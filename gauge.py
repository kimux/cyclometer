#!/usr/bin/env python3
"""
gauge.py -- the analogue dial display for cyclometer.

Draws a 0-40 km/h dial with a needle, an assist-limit mark at 24 km/h,
a red zone, and a rolling odometer. Sized for a 480x320 SPI display.

    python3 gauge.py --simulate      # works on a Mac too
    python3 gauge.py                 # on the Pi, reading the sensor

Performance note: the dial face is rendered once at 3x and scaled down
(which is where the anti-aliasing comes from), then kept as a surface.
Every frame only restores the background under the needle and redraws
it, so the number of pixels sent over SPI stays small.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import time

import pygame
import pygame.gfxdraw

import measure as cc

# --------------------------------------------------------------------------
# Layout and look
# --------------------------------------------------------------------------

W, H = 480, 320
SS = 3                      # supersampling factor for the dial face

# The dial is deliberately larger than the screen: the top of the arc
# sits on the top edge and the bottom of the circle runs off below, so
# the face fills the width instead of leaving margins either side.
CX, CY, R = 240, 186, 185   # dial centre and radius
V_MAX = 40.0                # full-scale speed
ANG_0, ANG_MAX = 200.0, -20.0   # degrees, maths convention (y flipped)
ASSIST_LIMIT = 24.0         # e-bike assist cuts out here

# Coloured band, in the manner of a 1970s bicycle speedometer: green
# while the motor is helping, amber just past the cut-off, red beyond.
BAND_IN, BAND_OUT = 0.795, 0.960

BG        = (12, 12, 14)
FACE      = (24, 24, 28)
INK       = (238, 234, 224)
DIM       = (150, 148, 142)
GREEN     = (46, 150, 62)
AMBER     = (226, 138, 28)
RED       = (198, 40, 36)
NEEDLE    = (246, 244, 238)
NEEDLE_EDGE = (0, 0, 0)     # outline so the needle lifts off the band
HUB       = (232, 228, 220)
ODO_BG    = (8, 8, 10)
ODO_FONT_PT = 29            # shared by the odometer and the km/h label


def angle_for(v: float) -> float:
    """Degrees for a speed, clamped to the dial."""
    v = max(0.0, min(V_MAX, v))
    return ANG_0 + (ANG_MAX - ANG_0) * (v / V_MAX)


def polar(cx: float, cy: float, r: float, deg: float) -> tuple[float, float]:
    a = math.radians(deg)
    return cx + r * math.cos(a), cy - r * math.sin(a)


# --------------------------------------------------------------------------
# Dial face, drawn once
# --------------------------------------------------------------------------

def build_face(font_big, font_small, show_20: bool = True) -> pygame.Surface:
    s = pygame.Surface((W * SS, H * SS))
    s.fill(BG)
    cx, cy, r = CX * SS, CY * SS, R * SS

    pygame.draw.circle(s, FACE, (cx, cy), r)

    # coloured band: one filled ring segment per speed range
    for v0, v1, col in ((0.0, ASSIST_LIMIT, GREEN),
                        (ASSIST_LIMIT, 30.0, AMBER),
                        (30.0, V_MAX, RED)):
        pts = []
        steps = max(8, int((v1 - v0) * 3))
        for i in range(steps + 1):
            v = v0 + (v1 - v0) * i / steps
            pts.append(polar(cx, cy, r * BAND_OUT, angle_for(v)))
        for i in range(steps, -1, -1):
            v = v0 + (v1 - v0) * i / steps
            pts.append(polar(cx, cy, r * BAND_IN, angle_for(v)))
        pygame.draw.polygon(s, col, pts)

    # ticks: major every 10, mid every 5, minor every 2
    v = 0.0
    while v <= V_MAX + 1e-6:
        a = angle_for(v)
        # Ticks sit on top of the band and reach a little past it.
        if abs(v % 10) < 1e-6:
            r0, wd = 0.775, 5
        elif abs(v % 5) < 1e-6:
            r0, wd = 0.845, 4
        else:
            r0, wd = 0.885, 3
        pygame.draw.line(s, INK, polar(cx, cy, r * r0, a),
                         polar(cx, cy, r * 0.985, a), wd * SS)
        if abs(v % 10) < 1e-6:
            # Everything except 20 goes outside the arc, using the
            # corners that would otherwise be wasted. 20 stays inside
            # because there is no room above it.
            mid = abs(v - V_MAX / 2) < 1e-6
            if mid and not show_20:
                # the clock lives here instead
                v += 2.0
                continue
            txt = font_big.render(f"{int(v)}", True, INK)
            if not mid:
                # Push out far enough that no corner of the glyph box
                # can touch the rim, whatever the angle or font size.
                half = math.hypot(txt.get_width(), txt.get_height()) / 2
                rad = r + half + 5 * SS
            else:
                # 20 is the only one inside; pull it in far enough that
                # it cannot touch the coloured band above it.
                rad = r * BAND_IN - half - 5 * SS
            s.blit(txt, txt.get_rect(center=polar(cx, cy, rad, a)))
        v += 2.0

    # unit, in the open space between the hub and the odometer,
    # matching the "km" on the odometer so the two read as a pair
    t = font_small.render("km/h", True, DIM)
    s.blit(t, t.get_rect(center=(cx, cy + int(r * 0.225))))
    return pygame.transform.smoothscale(s, (W, H))


# --------------------------------------------------------------------------
# Needle and odometer
# --------------------------------------------------------------------------

HUB_R = 22          # radius of the white boss at the pivot
NEEDLE_HALF = 18    # half-width of the needle where it leaves the boss
NEEDLE_TAIL = 44    # counterweight length behind the pivot
TIP_R = 5           # tip is a rounded cap of this radius, not a point
PEAK_R = 6          # the peak-speed dot
PEAK_RING = 3       # white ring around it, so it shows on any band colour
PEAK_RADIUS = 0.877 # where the dot sits: mid-band, near the needle tip
PEAK_MIN = 1.0      # do not show a peak below this
EDGE = 3            # thickness of the black outline around the needle


def tip_centre(deg: float, grow: float = 0.0) -> tuple[float, float]:
    return polar(CX, CY, R * 0.945 + grow - (TIP_R + grow), deg)


def needle_points(deg: float, grow: float = 0.0) -> list[tuple[float, float]]:
    """Body of the needle, ending in a flat that the cap rounds off.

    grow inflates the whole shape, which is how the black outline is
    made: the same needle drawn slightly larger, underneath.
    """
    cap = TIP_R + grow
    tx, ty = tip_centre(deg, grow)
    ux, uy = math.cos(math.radians(deg + 90)), -math.sin(math.radians(deg + 90))
    tl = (tx + ux * cap, ty + uy * cap)
    tr = (tx - ux * cap, ty - uy * cap)
    l = polar(CX, CY, NEEDLE_HALF + grow, deg + 90)
    rt = polar(CX, CY, NEEDLE_HALF + grow, deg - 90)
    tail = polar(CX, CY, NEEDLE_TAIL + grow, deg + 180)
    return [tl, l, tail, rt, tr]


def draw_needle(dst: pygame.Surface, deg: float) -> pygame.Rect:
    for grow, col in ((EDGE, NEEDLE_EDGE), (0.0, NEEDLE)):
        pts = needle_points(deg, grow)
        ipts = [(int(x), int(y)) for x, y in pts]
        pygame.gfxdraw.filled_polygon(dst, ipts, col)
        pygame.gfxdraw.aapolygon(dst, ipts, col)
        tx, ty = tip_centre(deg, grow)
        rad = int(TIP_R + grow)
        pygame.gfxdraw.filled_circle(dst, int(tx), int(ty), rad, col)
        pygame.gfxdraw.aacircle(dst, int(tx), int(ty), rad, col)
    pygame.gfxdraw.filled_circle(dst, CX, CY, HUB_R + EDGE, NEEDLE_EDGE)
    pygame.gfxdraw.aacircle(dst, CX, CY, HUB_R + EDGE, NEEDLE_EDGE)
    pygame.gfxdraw.filled_circle(dst, CX, CY, HUB_R, HUB)
    pygame.gfxdraw.aacircle(dst, CX, CY, HUB_R, HUB)
    pygame.gfxdraw.aacircle(dst, CX, CY, HUB_R - 4, (120, 116, 110))
    pts = needle_points(deg, EDGE)
    xs = [p[0] for p in pts] + [CX - HUB_R - EDGE, CX + HUB_R + EDGE]
    ys = [p[1] for p in pts] + [CY - HUB_R - EDGE, CY + HUB_R + EDGE]
    pad = TIP_R + EDGE + 3
    return pygame.Rect(min(xs) - pad, min(ys) - pad,
                       max(xs) - min(xs) + pad * 2,
                       max(ys) - min(ys) + pad * 2)


# --------------------------------------------------------------------------
# Boxed digit panel, shared by the odometer and the clock
# --------------------------------------------------------------------------

BOX_BG   = (30, 30, 34)
BOX_INV  = (236, 232, 224)
BOX_FG_INV = (20, 20, 22)
PANEL_EDGE = (70, 68, 64)
PEAK_COL = (222, 34, 30)
FAULT_BG = (176, 24, 20)


def panel_rect(n: int, dw: int, dh: int, pad: int, sep_w: int,
               center: tuple[int, int]) -> pygame.Rect:
    r = pygame.Rect(0, 0, dw * n + sep_w + pad * 2, dh + pad * 2)
    r.center = center
    return r


def draw_panel(dst, rect, font, chars, dw, dh, pad,
               invert_from=None, roll_frac=None,
               sep_after=None, sep_w=0) -> None:
    """One frame around a row of individually boxed characters.

    invert_from: index from which boxes are reversed out in white,
        which is how the odometer marks its fractional digits.
    roll_frac:   if given, the last box scrolls between its digit and
        the next, the way a mechanical drum sits between numbers.
    """
    pygame.draw.rect(dst, ODO_BG, rect, border_radius=4)
    pygame.draw.rect(dst, PANEL_EDGE, rect, 2, border_radius=4)
    x, y = rect.x + pad, rect.y + pad
    for i, ch in enumerate(chars):
        box = pygame.Rect(x, y, dw - 3, dh)
        inv = invert_from is not None and i >= invert_from
        pygame.draw.rect(dst, BOX_INV if inv else BOX_BG, box)
        fg = BOX_FG_INV if inv else INK
        clip = dst.get_clip()
        dst.set_clip(box)
        if roll_frac is not None and i == len(chars) - 1:
            off = int(roll_frac * dh)
            for k, d in enumerate((int(ch), (int(ch) + 1) % 10)):
                t = font.render(str(d), True, fg)
                dst.blit(t, t.get_rect(
                    center=(box.centerx, box.centery - off + k * dh)))
        else:
            t = font.render(ch, True, fg)
            dst.blit(t, t.get_rect(center=box.center))
        dst.set_clip(clip)
        pygame.draw.rect(dst, PANEL_EDGE, box, 1)
        x += dw
        if sep_after is not None and i == sep_after:
            cx = x + sep_w // 2
            pygame.draw.circle(dst, INK, (cx, box.y + dh // 3), 3)
            pygame.draw.circle(dst, INK, (cx, box.bottom - dh // 3), 3)
            x += sep_w


def peak_bounds(v: float) -> pygame.Rect:
    x, y = polar(CX, CY, R * PEAK_RADIUS, angle_for(v))
    e = PEAK_R + PEAK_RING + 2
    return pygame.Rect(int(x) - e, int(y) - e, e * 2, e * 2)


def draw_peak(dst: pygame.Surface, v: float) -> pygame.Rect:
    """A dot left behind at the fastest the needle has reached."""
    x, y = polar(CX, CY, R * PEAK_RADIUS, angle_for(v))
    x, y = int(x), int(y)
    for rad, col in ((PEAK_R + PEAK_RING, (240, 238, 232)), (PEAK_R, PEAK_COL)):
        pygame.gfxdraw.filled_circle(dst, x, y, rad, col)
        pygame.gfxdraw.aacircle(dst, x, y, rad, col)
    return peak_bounds(v)


class Odometer:
    """The distance readout, in the style of a mechanical odometer.

    The frame holds only the digits and is centred on the screen, so
    the reading sits on the vertical axis of the dial. The unit is set
    outside the frame, to its right.

    There is no printed decimal point: the fractional digits are marked
    by being reversed out in white, which is also where the movement is.
    """

    N_DIGITS = 5        # 99.999 km, so the last digit is one metre
    INT_DIGITS = 2      # digits before the (implied) decimal point
    DW, DH, PAD = 36, 42, 6

    def __init__(self, font) -> None:
        self.font = font
        self.rect = panel_rect(self.N_DIGITS, self.DW, self.DH, self.PAD, 0,
                               (W // 2, 0))
        self.rect.bottom = H - 6
        unit = font.render("km", True, DIM)
        self.unit_rect = unit.get_rect(
            midleft=(self.rect.right + 10, self.rect.centery))
        self.dirty = self.rect.union(self.unit_rect)

    def draw(self, dst: pygame.Surface, km: float) -> pygame.Rect:
        metres = km * 1000.0
        digits = f"{int(metres) % 100_000:0{self.N_DIGITS}d}"
        draw_panel(dst, self.rect, self.font, digits,
                   self.DW, self.DH, self.PAD,
                   invert_from=self.INT_DIGITS,
                   roll_frac=metres - math.floor(metres))
        t = self.font.render("km", True, DIM)
        dst.blit(t, self.unit_rect)
        return self.dirty


class FaultBanner:
    """A small warning tucked into the empty bottom-left corner.

    Losing one of three magnets is not fatal -- speed and distance are
    still measured from the remaining two -- so this must not displace
    anything the rider actually reads. It sits below the 0 label, clear
    of the needle, the dial and the odometer.
    """

    PAD_X, PAD_Y = 5, 3

    def __init__(self, font, center: tuple[int, int]) -> None:
        self.font = font
        self.center = center
        self.rect = pygame.Rect(0, 0, 0, 0)

    def draw(self, dst: pygame.Surface, text: str) -> pygame.Rect:
        t = self.font.render(text, True, (255, 246, 242))
        r = t.get_rect()
        r.inflate_ip(self.PAD_X * 2, self.PAD_Y * 2)
        r.center = self.center
        self.rect = r
        pygame.draw.rect(dst, FAULT_BG, r, border_radius=3)
        dst.blit(t, t.get_rect(center=r.center))
        return r


class Clock:
    """Hours and minutes, boxed the same way as the odometer."""

    DW, DH, PAD, SEP_W = 28, 34, 5, 12

    def __init__(self, font, center: tuple[int, int]) -> None:
        self.font = font
        self.rect = panel_rect(4, self.DW, self.DH, self.PAD,
                               self.SEP_W, center)

    def draw(self, dst: pygame.Surface) -> pygame.Rect:
        draw_panel(dst, self.rect, self.font, time.strftime("%H%M"),
                   self.DW, self.DH, self.PAD,
                   sep_after=1, sep_w=self.SEP_W)
        return self.rect


# --------------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser(description="cyclometer analogue display")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--gpiozero", action="store_true")
    ap.add_argument("--pin", type=int, default=cc.PULSE_PIN)
    ap.add_argument("--circumference", type=float, default=cc.CIRCUMFERENCE_M)
    ap.add_argument("--magnets", type=int, default=cc.MAGNETS)
    ap.add_argument("--windowed", action="store_true", help="no fullscreen")
    ap.add_argument("--fault", type=str, default=None,
                    help="draw this fault banner (preview only)")
    ap.add_argument("--clock", choices=("auto", "on", "off"), default="auto",
                    help="auto shows the clock only when NTP has set the "
                         "time; use on to preview it on a Mac")
    ap.add_argument("--record", type=float, default=None, metavar="SECONDS",
                    help="render this many seconds of the simulation to "
                         "PNG frames in ./frames, then exit")
    ap.add_argument("--fps", type=int, default=20,
                    help="frame rate for --record (default 20)")
    ap.add_argument("--shot", type=str, default=None,
                    help="render one frame at this speed:km and exit, "
                         "e.g. --shot 27.3:123.45")
    args = ap.parse_args()

    if args.shot or args.record:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    pygame.init()
    pygame.mouse.set_visible(False)
    flags = (0 if (args.windowed or args.shot or args.record)
             else pygame.FULLSCREEN)
    screen = pygame.display.set_mode((W, H), flags)
    pygame.display.set_caption("CYCLOMETER")

    def mono(sz, bold=False):
        for name in ("dejavusansmono", "liberationmono", "couriernew"):
            f = pygame.font.match_font(name, bold=bold)
            if f:
                return pygame.font.Font(f, sz)
        return pygame.font.Font(None, sz)

    # The clock takes the place of the "20" label, but only when the
    # time is actually trustworthy: the Pi has no RTC, so without an
    # NTP sync the clock would be quietly wrong.
    synced = (cc.clock_is_synced() if args.clock == "auto"
              else args.clock == "on")
    face = build_face(mono(38 * SS, True), mono(ODO_FONT_PT * SS, True),
                      show_20=not synced)
    odo = Odometer(mono(ODO_FONT_PT, True))
    clock = Clock(mono(ODO_FONT_PT - 5, True), (CX, CY - 88))
    fault_banner = FaultBanner(mono(ODO_FONT_PT - 13, True), (68, 296))

    if args.shot:
        parts = args.shot.split(":")
        v, km = float(parts[0]), float(parts[1])
        pk = float(parts[2]) if len(parts) > 2 else v
        screen.blit(face, (0, 0))
        if pk >= PEAK_MIN:
            draw_peak(screen, pk)
        if synced:
            clock.draw(screen)
        if args.fault:
            fault_banner.draw(screen, args.fault)
        draw_needle(screen, angle_for(v))
        odo.draw(screen, km)
        pygame.display.flip()
        pygame.image.save(screen, "/tmp/gauge.png")
        return 0

    if args.record:
        import random as _random
        _random.seed(0)                    # same demo every time
        out_dir = "frames"
        os.makedirs(out_dir, exist_ok=True)
        sim = cc.SimulatedSource("", args.pin, args.magnets,
                                 args.circumference)
        sim._t0_ns = 0
        sim._last_ns = 0
        model = cc.RideModel(circumference_m=args.circumference,
                             magnets=args.magnets)
        shown = peak = 0.0
        pending = None
        n = int(args.record * args.fps)
        for i in range(n):
            now = int(i / args.fps * 1e9)
            for t_ns, level in sim.advance(now):
                model.set_level(level)
                if level == 0:
                    pending = t_ns if model.on_pulse(t_ns) else None
                elif pending is not None:
                    model.state.width_s = (t_ns - pending) / 1e9
                    pending = None
            st = model.tick(now)
            shown += (st.speed_kf_kmh - shown) * 0.30
            peak = max(peak, shown)
            screen.blit(face, (0, 0))
            if synced:
                clock.draw(screen)
            if st.fault:
                fault_banner.draw(screen, st.fault)
            if peak >= PEAK_MIN:
                draw_peak(screen, peak)
            draw_needle(screen, angle_for(shown))
            odo.draw(screen, st.distance_m / 1000.0)
            pygame.image.save(screen, f"{out_dir}/{i:04d}.png")
        print(f"{n} frames written to {out_dir}/ at {args.fps} fps")
        return 0

    kind = ("simulate" if args.simulate
            else "gpiozero" if args.gpiozero else "auto")
    source = cc.make_source(kind, args.pin)
    model = cc.RideModel(circumference_m=args.circumference,
                         magnets=args.magnets)
    logger = cc.CsvLogger(cc.LOG_DIR)

    screen.blit(face, (0, 0))
    pygame.display.flip()

    shown = 0.0                 # needle position, lightly smoothed
    peak = 0.0                  # furthest the needle has reached
    prev_rect = pygame.Rect(0, 0, 0, 0)
    prev_peak = pygame.Rect(0, 0, 0, 0)
    prev_km = -1.0
    fps = pygame.time.Clock()
    pending: int | None = None
    running = True

    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN and e.key in (
                    pygame.K_ESCAPE, pygame.K_q):
                running = False
            elif e.type == pygame.MOUSEBUTTONDOWN:
                running = False          # a tap anywhere quits

        for t_ns, level in source.poll(0.0):
            model.set_level(level)
            if level == 0:
                pending = t_ns if model.on_pulse(t_ns) else None
            elif pending is not None:
                st = model.state
                st.width_s = (t_ns - pending) / 1e9
                if st.width_min_s == 0.0 or st.width_s < st.width_min_s:
                    st.width_min_s = st.width_s
                logger.log(pending, st)
                pending = None

        st = model.tick(time.monotonic_ns())
        shown += (st.speed_kf_kmh - shown) * 0.30

        dirty = []
        cur = needle_points(angle_for(shown))
        xs = [p[0] for p in cur] + [CX - 15, CX + 15]
        ys = [p[1] for p in cur] + [CY - 15, CY + 15]
        pad = TIP_R + 3
        cur_rect = pygame.Rect(min(xs) - pad, min(ys) - pad,
                               max(xs) - min(xs) + pad * 2,
                               max(ys) - min(ys) + pad * 2)
        restore = cur_rect.union(prev_rect)
        screen.blit(face, restore, restore)
        dirty.append(restore)

        if synced:
            # Drawn before the needle so the needle passes in front,
            # and refreshed every frame because the needle sweeps
            # straight through this area at 20 km/h.
            screen.blit(face, clock.rect, clock.rect)
            dirty.append(clock.draw(screen))

        if st.fault:
            screen.blit(face, fault_banner.rect, fault_banner.rect)
            dirty.append(fault_banner.draw(screen, st.fault))
        elif fault_banner.rect.width:
            screen.blit(face, fault_banner.rect, fault_banner.rect)
            dirty.append(fault_banner.rect)
            fault_banner.rect = pygame.Rect(0, 0, 0, 0)

        # The peak marker tracks the needle itself rather than the raw
        # estimate, so the dot always sits where the needle actually got to.
        peak = max(peak, shown)
        if peak >= PEAK_MIN:
            cur_peak = peak_bounds(peak)
            if cur_peak != prev_peak:
                screen.blit(face, prev_peak, prev_peak)
                dirty.append(prev_peak)
            screen.blit(face, cur_peak, cur_peak)
            dirty.append(draw_peak(screen, peak))
            prev_peak = cur_peak

        dirty.append(draw_needle(screen, angle_for(shown)))
        prev_rect = cur_rect

        km = st.distance_m / 1000.0
        if abs(km - prev_km) > 0.0005:
            dirty.append(odo.draw(screen, km))
            prev_km = km

        pygame.display.update(dirty)
        fps.tick(30)

    source.close()
    logger.close()
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
