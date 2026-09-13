#!/usr/bin/env python3
"""
measure.py -- the measurement layer of cyclometer.

Reads the wheel sensor and turns its pulses into speed, distance and
acceleration. Run it directly for a console monitor, useful when
setting up magnets or diagnosing the sensor:
    python3 measure.py              # on the Pi, reading the sensor
    python3 measure.py --simulate   # anywhere, no GPIO needed

gauge.py imports this module for the analogue display; importing it
does not start the console monitor.

Hardware: Raspberry Pi Zero 2 W + NJK-5002C (NPN open collector,
active LOW). The console output fits 48 columns x 12 rows, so it is
readable on the 480x320 panel as well as over SSH.

CSV logs are written next to this file, so the whole project stays in
one place; override with --log-dir.
"""

from __future__ import annotations

import argparse
import csv
import math
import random
import signal
import socket
import subprocess
import sys
import time
from collections import deque
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path

# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

GPIO_CHIP = "/dev/gpiochip0"
PULSE_PIN = 16           # BCM16 = physical pin 36

MAGNETS = 3              # magnets on the wheel (120 degrees apart)
# TIRE 20x1.75 (47-406). The 1515 mm from the usual cycle-computer
# tables is too small: 406 + 47*2 = 500 mm outer diameter gives about
# 1571 mm unloaded, and comparing straight sections of a GPS track
# against the wheel count put the real figure near 1560 mm. Confirm
# with a roll-out under load when convenient.
CIRCUMFERENCE_M = 1.560

# Noise rejection is expressed as a speed, not a time. A fixed minimum
# interval would silently depend on the magnet count: with 12 magnets
# on a 1.56 m wheel, 20 ms is only 23 km/h, so real pulses would start
# being thrown away. Deriving it from the arc keeps the meaning fixed.
MAX_PLAUSIBLE_KMH = 90.0  # faster than this is noise, not a bicycle
STOP_TIMEOUT_S = 3.0      # no pulse for this long means we have stopped

# Logs land next to this script, so moving the project moves the logs.
LOG_DIR = Path(__file__).resolve().parent
REFRESH_HZ = 20


# --------------------------------------------------------------------------
# RideState / RideModel : no hardware, no clock. Testable on a Mac.
# --------------------------------------------------------------------------

@dataclass
class RideState:
    level: int = 1              # raw GPIO level (1 = HIGH = no magnet)
    pulses: int = 0             # cumulative pulse count
    revolutions: float = 0.0    # cumulative wheel revolutions
    interval_s: float = 0.0     # latest pulse interval
    speed_kmh: float = 0.0      # from a 1-revolution window (robust)
    speed_inst_kmh: float = 0.0 # from the latest pulse only (responsive)
    distance_m: float = 0.0
    max_kmh: float = 0.0
    moving_s: float = 0.0
    stopped: bool = True
    rejected: int = 0           # pulses discarded as noise
    width_s: float = 0.0        # LOW duration of the last pulse
    width_min_s: float = 0.0    # narrowest pulse seen while riding
    speed_kf_kmh: float = 0.0   # Kalman estimate, updates between pulses
    accel: float = 0.0          # m/s^2, positive when speeding up
    accel_max: float = 0.0      # strongest acceleration seen
    accel_min: float = 0.0      # strongest deceleration seen
    fault: str | None = None    # e.g. "MAGNET LOST", for the display


class RideModel:
    """Turns a pulse train into speed and distance."""

    def __init__(
        self,
        circumference_m: float = CIRCUMFERENCE_M,
        magnets: int = MAGNETS,
        max_plausible_kmh: float = MAX_PLAUSIBLE_KMH,
        stop_timeout_s: float = STOP_TIMEOUT_S,
    ) -> None:
        self.circumference_m = circumference_m
        self.magnets = magnets
        self.arc_m = circumference_m / magnets   # distance per pulse
        self.min_interval_s = self.arc_m / (max_plausible_kmh / 3.6)
        self.stop_timeout_s = stop_timeout_s

        # Last magnets+1 timestamps. First to last is exactly one full
        # revolution, so any error in magnet placement cancels out.
        self._ts: deque[int] = deque(maxlen=magnets + 1)
        # recent intervals, for the missing-magnet check
        self._recent: deque[float] = deque(maxlen=self.FAULT_WINDOW)
        self._fault_run = 0

        self.kf = SpeedKalman()
        self.state = RideState()
        self._last_tick_ns: int | None = None

    # --- missing-magnet detection ---------------------------------
    # A magnet that has fallen off does not look like random dropped
    # pulses: with three magnets, losing one leaves 240 and 120 degree
    # gaps, so the intervals alternate long-short-long in a strict 2:1
    # pattern. On a real ride this showed up as 99.9% alternation,
    # which acceleration alone never produces.
    FAULT_WINDOW = 25       # intervals kept for the check
    FAULT_RATIO = 1.6       # above this an interval counts as "long"
    FAULT_ALT = 0.90        # required alternation rate
    FAULT_RUNS = 8          # consecutive positive checks before warning

    def _check_magnets(self) -> str | None:
        """Only defined for three magnets; the 2:1 signature is specific
        to losing one of three. Other counts need their own test."""
        if self.magnets != 3:
            return None
        d = list(self._recent)
        if len(d) < 15:
            return None
        ratios = [d[i + 1] / d[i] for i in range(len(d) - 1) if d[i] > 0]
        if len(ratios) < 12:
            return None
        long_ = [r > self.FAULT_RATIO for r in ratios]
        alt = sum(long_[i] != long_[i + 1]
                  for i in range(len(long_) - 1)) / (len(long_) - 1)
        share = sum(long_) / len(long_)
        if alt >= self.FAULT_ALT and 0.35 < share < 0.65:
            return "MAGNET LOST"
        return None

    def on_pulse(self, t_ns: int) -> bool:
        """Feed in one pulse. Returns False if rejected as noise."""
        s = self.state

        if self._ts:
            dt = (t_ns - self._ts[-1]) / 1e9
            if dt < self.min_interval_s:
                s.rejected += 1
                return False
            s.interval_s = dt
            s.speed_inst_kmh = self.arc_m / dt * 3.6
            self._recent.append(dt)
            # Hysteresis: a few consecutive positives before warning,
            # and the same again before clearing it.
            if self._check_magnets():
                self._fault_run = min(self.FAULT_RUNS * 2,
                                      self._fault_run + 1)
            else:
                self._fault_run = max(0, self._fault_run - 1)
            if self._fault_run >= self.FAULT_RUNS:
                s.fault = "MAGNET LOST"
            elif self._fault_run == 0:
                s.fault = None
            if dt > self.stop_timeout_s:
                # This interval spans a stop, so its average speed says
                # nothing about how fast we are moving now. Drop it and
                # let the next pulse restart the filter.
                self.kf.reset()
            else:
                # Average over the interval == instantaneous speed at
                # its midpoint, under constant acceleration.
                self.kf.update(self.arc_m / dt, (t_ns + self._ts[-1]) // 2)

        self._ts.append(t_ns)
        s.pulses += 1
        s.revolutions = s.pulses / self.magnets
        s.distance_m = s.pulses * self.arc_m

        if len(self._ts) == self._ts.maxlen:
            dt_rev = (self._ts[-1] - self._ts[0]) / 1e9
            if dt_rev > 0:
                s.speed_kmh = self.circumference_m / dt_rev * 3.6
        else:
            s.speed_kmh = s.speed_inst_kmh

        s.stopped = False
        s.max_kmh = max(s.max_kmh, s.speed_kmh)
        return True

    def set_level(self, level: int) -> None:
        self.state.level = level

    def tick(self, now_ns: int) -> RideState:
        """Advance time. Call this regularly, pulses or not."""
        s = self.state

        if self._last_tick_ns is not None and not s.stopped:
            s.moving_s += (now_ns - self._last_tick_ns) / 1e9
        self._last_tick_ns = now_ns

        self.kf.predict_to(now_ns)

        if self._ts:
            elapsed = (now_ns - self._ts[-1]) / 1e9
            if elapsed > self.stop_timeout_s:
                s.stopped = True
                s.speed_kmh = 0.0
                s.speed_inst_kmh = 0.0
                self.kf.reset()
                self.kf.predict_to(now_ns)
                self._recent.clear()
                self._fault_run = 0
                s.fault = None
            elif elapsed > 0:
                self.kf.clamp_speed(self.arc_m / elapsed)
                # The pulse that has not arrived yet caps how fast we
                # can be going. Without this the reading sticks while
                # slowing down.
                cap = self.arc_m / elapsed * 3.6
                s.speed_kmh = min(s.speed_kmh, cap)
                s.speed_inst_kmh = min(s.speed_inst_kmh, cap)

        s.speed_kf_kmh = max(0.0, self.kf.v * 3.6)
        s.accel = self.kf.a
        if not s.stopped:
            s.accel_max = max(s.accel_max, s.accel)
            s.accel_min = min(s.accel_min, s.accel)
        return s


# --------------------------------------------------------------------------
# Kalman filter for speed and acceleration
# --------------------------------------------------------------------------

class SpeedKalman:
    """Constant-acceleration Kalman filter over irregular pulse timing.

    State is [v, a] in m/s and m/s^2. Between pulses we only predict,
    so the estimate keeps moving while the wheel is between magnets --
    that is what a plain interval calculation cannot do, and it is why
    acceleration comes out usable at low speed and during launches.

    One subtlety matters for accuracy: a pulse tells us the AVERAGE
    speed over the interval, not the speed at its end. Under constant
    acceleration the average over an interval equals the instantaneous
    value at its midpoint, so we apply each measurement at the midpoint
    time. Without that, every estimate lags by half an interval.
    """

    def __init__(self, sigma_jerk: float = 0.6,
                 rel_meas_noise: float = 0.05,
                 gate: float = 3.0) -> None:
        # sigma_jerk: how fast acceleration itself is allowed to change
        #   (m/s^3). Larger = trusts new measurements more, noisier.
        # rel_meas_noise: 1-sigma error of one pulse measurement as a
        #   fraction. Dominated by magnet placement, measured at ~4%.
        self.q = sigma_jerk ** 2
        self.rel = rel_meas_noise
        self.gate = gate
        self.reset()

    # Physical bounds. A bicycle cannot exceed these, so anything past
    # them is a numerical artefact rather than a measurement.
    A_LIMIT = 5.0          # m/s^2
    COAST_LIMIT_S = 1.0    # how far ahead we trust the acceleration state

    def reset(self) -> None:
        self.ready = False
        self.v = 0.0
        self.a = 0.0
        # covariance, [[vv, va],[av, aa]]
        self.p00, self.p01, self.p11 = 1.0, 0.0, 1.0
        self.t_ns: int | None = None
        self.outliers = 0

    def predict_to(self, t_ns: int) -> None:
        if self.t_ns is None:
            self.t_ns = t_ns
            return
        dt = (t_ns - self.t_ns) / 1e9
        if dt <= 0:
            return
        self.t_ns = t_ns

        # x = F x. Extrapolating acceleration over a long gap diverges,
        # so velocity coasts on the acceleration state for at most a
        # second while the covariance still grows over the full gap.
        self.v += self.a * min(dt, self.COAST_LIMIT_S)
        self.v = max(0.0, self.v)
        # P = F P F' + Q
        p00 = self.p00 + 2 * dt * self.p01 + dt * dt * self.p11
        p01 = self.p01 + dt * self.p11
        p11 = self.p11
        d2, d3, d4 = dt * dt, dt ** 3, dt ** 4
        self.p00 = p00 + self.q * d4 / 4.0
        self.p01 = p01 + self.q * d3 / 2.0
        self.p11 = p11 + self.q * d2

    def update(self, z: float, t_ns: int) -> None:
        """z: average speed in m/s, valid at the interval midpoint t_ns."""
        if not self.ready:
            self.bootstrap(z, t_ns)
            return
        self.predict_to(t_ns)
        r = (self.rel * max(z, 0.1)) ** 2
        y = z - self.v              # innovation
        sden = self.p00 + r
        if sden <= 0:
            return
        # Soft outlier handling: a missed pulse halves z, which would
        # otherwise drag the estimate. Inflate R instead of rejecting,
        # so genuine hard braking still gets through, just slower.
        if abs(y) > self.gate * math.sqrt(sden):
            self.outliers += 1
            r *= 25.0
            sden = self.p00 + r
        k0 = self.p00 / sden
        k1 = self.p01 / sden
        self.v += k0 * y
        self.a += k1 * y
        p00, p01, p11 = self.p00, self.p01, self.p11
        self.p00 = (1 - k0) * p00
        self.p01 = (1 - k0) * p01
        self.p11 = p11 - k1 * p01
        self.v = max(0.0, self.v)
        self.a = max(-self.A_LIMIT, min(self.A_LIMIT, self.a))

    def bootstrap(self, z: float, t_ns: int) -> None:
        """Start (or restart) the filter from a single measurement."""
        self.reset()
        self.v = z
        self.p00 = (self.rel * max(z, 0.1)) ** 2 * 4.0
        self.p11 = 1.0
        self.t_ns = t_ns
        self.ready = True

    def clamp_speed(self, v_max: float) -> None:
        """A pulse that has not arrived yet bounds how fast we can be."""
        if self.v > v_max:
            self.v = v_max
            if self.a > 0:
                self.a = 0.0


# --------------------------------------------------------------------------
# Pulse input backends
# --------------------------------------------------------------------------

class GpiodSource:
    """libgpiod v2. The kernel timestamps each edge in its interrupt
    handler, so short pulses are caught and jitter is microseconds.

    Do NOT set debounce_period here. BCM2835 has no hardware debounce,
    so the kernel emulates it by re-reading the line from a delayed
    work item. That rounds up to a jiffy (1 ms at HZ=1000), timestamps
    the event at the re-read instead of the edge, and silently drops
    any pulse shorter than the delay. A Hall sensor has a Schmitt
    trigger on its output and does not bounce, so the speed limit in
    software is the only filtering we need.
    """

    name = "gpiod (kernel ts)"

    def __init__(self, chip: str, pin: int) -> None:
        import gpiod
        from gpiod.line import Bias, Edge

        self._req = gpiod.request_lines(
            chip,
            consumer="cyclometer",
            config={
                pin: gpiod.LineSettings(
                    edge_detection=Edge.BOTH,
                    bias=Bias.PULL_UP,
                )
            },
        )

    def poll(self, timeout_s: float):
        """Returns a list of (t_ns, level); level 1 = HIGH."""
        events = []
        if self._req.wait_edge_events(timedelta(seconds=timeout_s)):
            for e in self._req.read_edge_events():
                level = 0 if e.event_type == e.Type.FALLING_EDGE else 1
                events.append((e.timestamp_ns, level))
        return events

    def close(self) -> None:
        self._req.release()


class GpiozeroSource:
    """Fallback when gpiod is unavailable. Timestamps taken in userspace."""

    name = "gpiozero (user ts)"

    def __init__(self, chip: str, pin: int) -> None:
        from gpiozero import DigitalInputDevice

        self._q: deque = deque()
        self._dev = DigitalInputDevice(pin, pull_up=True, bounce_time=None)
        # pull_up=True, so "active" means the pin is LOW: a magnet passed.
        self._dev.when_activated = lambda: self._q.append(
            (time.monotonic_ns(), 0)
        )
        self._dev.when_deactivated = lambda: self._q.append(
            (time.monotonic_ns(), 1)
        )

    def poll(self, timeout_s: float):
        time.sleep(timeout_s)
        out = []
        while self._q:
            out.append(self._q.popleft())
        return out

    def close(self) -> None:
        self._dev.close()


class SimulatedSource:
    """Synthetic pulses so the program runs with no hardware at all.

    Distance is integrated in small steps and a pulse is emitted every
    time the wheel has covered one arc. Predicting the next pulse from
    the speed at the previous one looks simpler, but breaks during a
    launch: at 0.1 m/s it schedules the next pulse four seconds ahead,
    by which time the bike is doing 4 m/s, so the whole acceleration is
    skipped and the model sees a standstill followed by a jump.
    """

    name = "simulated"

    # Roughly what the bike does: the magnet sweeps past the sensor,
    # so the LOW pulse is (detection window) / (magnet tip speed).
    DETECT_MM = 4.0      # width of the region where the sensor trips
    MAGNET_RADIUS_M = 0.20
    STEP_NS = 5_000_000  # integration step, 5 ms

    def __init__(self, chip: str, pin: int, magnets: int = MAGNETS,
                 circumference_m: float = CIRCUMFERENCE_M) -> None:
        self._circ = circumference_m
        self._arc = circumference_m / magnets
        self._t0_ns = time.monotonic_ns()
        self._last_ns = self._t0_ns
        self._dist = 0.0        # distance since the last pulse

    def _width_ns(self, v_mps: float) -> int:
        """Pulse width at rim speed v."""
        rev_per_s = v_mps / self._circ
        tip_mps = rev_per_s * 2 * math.pi * self.MAGNET_RADIUS_M
        if tip_mps <= 0:
            return 10 ** 6
        return int(self.DETECT_MM / 1000.0 / tip_mps * 1e9)

    def _speed_mps(self, t: float) -> float:
        """Repeating profile: launch, cruise, slow down, stop.

        The cruise amplitude grows so that each swing sets a new peak,
        which is what exercises the peak marker on the dial.
        """
        t = t % 25.0
        if t < 6:
            v = 6.5 * (t / 6) ** 1.6
        elif t < 15:
            grow = 0.5 + (2.0 - 0.5) / (15 - 6) * (t - 6)
            v = 6.5 + 0.6 * math.sin(t * 1.5) * grow
        elif t < 21:
            v = max(0.0, 6.5 * (21 - t) / 6)
        else:
            v = 0.0
        return max(0.0, v + random.uniform(-0.05, 0.05))

    def poll(self, timeout_s: float):
        time.sleep(timeout_s)
        return self.advance(time.monotonic_ns())

    def advance(self, now: int):
        """Integrate up to `now` and return the pulses in between.

        Split out from poll() so a recorder can drive it from a virtual
        clock and get exactly the same pulse train every run.
        """
        out = []
        t = self._last_ns
        while t < now:
            nxt = min(t + self.STEP_NS, now)
            dt = (nxt - t) / 1e9
            v = self._speed_mps((t - self._t0_ns) / 1e9)
            d = v * dt
            while d > 0 and self._dist + d >= self._arc:
                need = self._arc - self._dist
                at = int(t + (nxt - t) * (need / d))
                w = self._width_ns(max(v, 0.02))
                out.append((at, 0))              # falling: magnet arrives
                out.append((at + w, 1))          # rising: magnet leaves
                d -= need
                self._dist = 0.0
            self._dist += d
            t = nxt
        self._last_ns = now
        return out

    def close(self) -> None:
        pass


def make_source(kind: str, pin: int, magnets: int = MAGNETS):
    if kind == "simulate":
        return SimulatedSource(GPIO_CHIP, pin, magnets)
    if kind == "gpiozero":
        return GpiozeroSource(GPIO_CHIP, pin)
    try:
        return GpiodSource(GPIO_CHIP, pin)
    except Exception as exc:
        print(f"gpiod unavailable, falling back to gpiozero: {exc}",
              file=sys.stderr)
        return GpiozeroSource(GPIO_CHIP, pin)


# --------------------------------------------------------------------------
# Display
# --------------------------------------------------------------------------

class ConsoleView:
    """Redraws in place with ANSI escapes. Fits 48 columns x 12 rows."""

    BAR_W = 40
    BAR_MAX_KMH = 40.0

    def __init__(self, model: RideModel, pin: int, tag: str = "") -> None:
        self._model = model
        self._pin = pin
        self._tag = tag
        self._history: deque[float] = deque(maxlen=5)
        self._first = True

    def note_pulse(self, interval_s: float) -> None:
        if interval_s > 0:
            self._history.append(interval_s)

    def render(self, s: RideState, backend: str) -> None:
        m = self._model
        if self._first:
            print("\033[2J", end="")
            self._first = False
        print("\033[H", end="")

        lv = ("\033[7m LOW  magnet \033[0m" if s.level == 0
              else " HIGH no magnet ")
        n = max(0, min(self.BAR_W,
                       int(s.speed_kf_kmh / self.BAR_MAX_KMH * self.BAR_W)))
        bar = "#" * n + "." * (self.BAR_W - n)
        hist = " ".join(f"{v*1000:.0f}" for v in self._history) or "(none)"
        # Pulse width scales with 1/speed, so extrapolate the margin
        # we would have at the 24 km/h assist limit.
        w24 = (s.width_s * s.speed_inst_kmh / 24.0
               if s.speed_inst_kmh > 0.5 else 0.0)

        state_txt = "STOPPED" if s.stopped else "RIDING"
        lines = [
            f"\033[1mCYCLOMETER\033[0m  {self._tag}",
            f"{backend}  BCM{self._pin}  mag{m.magnets}"
            f"  circ {m.circumference_m:.3f}m",
            f"Level {lv} State {state_txt}",
            f"Pulses  {s.pulses:7d}  {s.revolutions:8.2f} rev"
            f"  noise {s.rejected}",
            f"Interval {s.interval_s*1000:7.1f} ms"
            f"   Dist {s.distance_m/1000:7.3f} km",
            f"Width {s.width_s*1000:6.2f} min {s.width_min_s*1000:5.2f}"
            f"  @24km/h {w24*1000:5.2f} ms",
            f"\033[1mSPEED {s.speed_kf_kmh:8.2f} km/h\033[0m"
            f"  win {s.speed_kmh:6.2f} inst {s.speed_inst_kmh:6.2f}",
            f"[{bar}]",
            " 0        10        20        30        40",
            f"Max {s.max_kmh:6.2f} km/h   Moving {s.moving_s:7.1f} s",
            (f"\033[1;7m {s.fault} \033[0m  "
             f"Accel {s.accel:+6.2f} m/s2" if s.fault else
             f"Accel {s.accel:+6.2f} m/s2  peak {s.accel_max:+5.2f}"
             f" /{s.accel_min:+6.2f}"),
            (" \033[1;7m WARNING: not gpiod - short pulses lost \033[0m"
             if not backend.startswith("gpiod") and backend != "simulated"
             else "Ctrl-C to quit (log saved automatically)"),
        ]
        for ln in lines:
            print(f"\033[K{ln}")


# --------------------------------------------------------------------------
# Logging
# --------------------------------------------------------------------------

def clock_is_synced() -> bool:
    """True when NTP has actually set the clock this boot.

    The Zero 2 W has no battery-backed RTC. Without a network the time
    is only whatever fake-hwclock restored at boot, so wall-clock
    timestamps can be badly wrong. Monotonic timing is unaffected.

    systemd-timesyncd drops a stamp file when it first syncs, but the
    name changed between versions ("synchronized" now, "synced"
    before), and chrony or ntpsec write neither. timedatectl is the
    authority, so fall back to asking it.
    """
    d = Path("/run/systemd/timesync")
    if (d / "synchronized").exists() or (d / "synced").exists():
        return True
    try:
        out = subprocess.run(
            ["timedatectl", "show", "-p", "NTPSynchronized", "--value"],
            capture_output=True, text=True, timeout=2)
        return out.stdout.strip() == "yes"
    except Exception:
        return False


def short_hostname() -> str:
    """A hostname safe to put in a filename.

    Macs report things like "name.local" and the case varies, so trim
    to the first label and normalise. Anything unexpected is replaced
    rather than left in a path.
    """
    name = socket.gethostname().split(".")[0].lower()
    safe = "".join(c if c.isalnum() or c == "-" else "-" for c in name)
    return safe.strip("-") or "host"


def next_serial(directory: Path, prefix: str) -> int:
    """One past the highest serial already using this prefix.

    Numbering is per host, and separately per real/simulated, on
    purpose. Logs from the Pi and from a laptop end up in the same
    folder sooner or later, and a shared counter would either collide
    or skip numbers depending on which machine wrote last. Keeping the
    series apart means each stays intact however they are merged.
    """
    highest = 0
    for f in directory.glob(f"{prefix}*.csv"):
        stem = f.stem[len(prefix):]
        if stem.isdigit():
            highest = max(highest, int(stem))
    return highest + 1


class CsvLogger:
    """Writes the ride log.

    Simulated runs are marked twice over: in the filename, so a folder
    listing tells you what you have, and in a column, so it survives
    being renamed or merged into something else. Mistaking a demo run
    for real data is the kind of error that is hard to notice later.
    """

    def __init__(self, directory: Path, simulated: bool = False) -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.host = short_hostname()
        self.simulated = simulated
        prefix = f"ride-{self.host}-" + ("sim-" if simulated else "")
        self.serial = next_serial(directory, prefix)
        self.synced = clock_is_synced()
        self.path = directory / f"{prefix}{self.serial:04d}.csv"
        self._fp = self.path.open("w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fp)
        # wall_iso is only trustworthy when clock_synced is True.
        self._w.writerow(
            ["t_ns", "wall_iso", "clock_synced", "simulated",
             "pulse_index", "interval_s", "width_s", "speed_kmh",
             "speed_inst_kmh", "speed_kf_kmh", "accel_mps2", "distance_m"]
        )
        self._n = 0

    def log(self, t_ns: int, s: RideState) -> None:
        self._w.writerow([
            t_ns,
            time.strftime("%Y-%m-%dT%H:%M:%S"),
            int(self.synced),
            int(self.simulated),
            s.pulses,
            f"{s.interval_s:.6f}",
            f"{s.width_s:.6f}",
            f"{s.speed_kmh:.3f}",
            f"{s.speed_inst_kmh:.3f}",
            f"{s.speed_kf_kmh:.3f}",
            f"{s.accel:.4f}",
            f"{s.distance_m:.3f}",
        ])
        self._n += 1
        # Flush now and then: a sudden power cut should cost only the
        # last few rows, but flushing every row wears out the SD card.
        if self._n % 20 == 0:
            self._fp.flush()

    def close(self) -> None:
        self._fp.flush()
        self._fp.close()


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="cyclometer console monitor")
    ap.add_argument("--simulate", action="store_true",
                    help="synthetic pulses, no GPIO (for the Mac)")
    ap.add_argument("--gpiozero", action="store_true",
                    help="force the gpiozero backend")
    ap.add_argument("--pin", type=int, default=PULSE_PIN,
                    help=f"BCM pin number (default {PULSE_PIN})")
    ap.add_argument("--circumference", type=float, default=CIRCUMFERENCE_M,
                    help="wheel circumference in metres")
    ap.add_argument("--magnets", type=int, default=MAGNETS,
                    help="number of magnets on the wheel")
    ap.add_argument("--log-dir", type=Path, default=LOG_DIR,
                    help=f"where to write CSV logs (default {LOG_DIR})")
    ap.add_argument("--no-log", action="store_true", help="do not write CSV")
    args = ap.parse_args()

    kind = "simulate" if args.simulate else ("gpiozero" if args.gpiozero
                                             else "auto")
    source = make_source(kind, args.pin, args.magnets)
    model = RideModel(circumference_m=args.circumference,
                      magnets=args.magnets)
    logger = (None if args.no_log else
              CsvLogger(args.log_dir, simulated=(kind == "simulate")))
    if logger and not logger.synced:
        print("warning: clock not NTP-synced, wall-clock times are unreliable",
              file=sys.stderr)
    tag = f"log {logger.path.name}" if logger else "no log"
    view = ConsoleView(model, args.pin, tag)

    running = True

    def stop(_sig, _frm):
        nonlocal running
        running = False

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    period = 1.0 / REFRESH_HZ
    pending: int | None = None    # timestamp of an unmatched falling edge
    try:
        while running:
            for t_ns, level in source.poll(period):
                model.set_level(level)
                if level == 0:                     # falling: magnet arrives
                    pending = t_ns if model.on_pulse(t_ns) else None
                elif pending is not None:          # rising: magnet leaves
                    st = model.state
                    st.width_s = (t_ns - pending) / 1e9
                    if st.width_min_s == 0.0 or st.width_s < st.width_min_s:
                        st.width_min_s = st.width_s
                    view.note_pulse(st.interval_s)
                    if logger:
                        logger.log(pending, st)
                    pending = None

            state = model.tick(time.monotonic_ns())
            view.render(state, source.name)
    finally:
        source.close()
        if logger:
            logger.close()
            print(f"\nlog saved: {logger.path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
