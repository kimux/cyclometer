#!/usr/bin/env python3
"""
spinner.py -- a Blade Runner spinner flight-monitor skin for cyclometer.

Six CRT panels in a 2x3 grid. A magenta corridor rushes out from a
vanishing point at the centre: rings are born small, expand, and sweep
past the edges, so the flow rate is the speed. Lit windows stream by
on either side, the towers of a city you are flying between. Above
the panels sits a 16-segment readout.

    python3 spinner.py                      # on the Pi
    python3 spinner.py --simulate --windowed
    python3 spinner.py --shot 27.3:12.345
    python3 spinner.py --record 25          # PNG frames for a demo

Like gauge.py this is display only; all measurement lives in measure.py.
"""

from __future__ import annotations

import argparse
import math
import os
import random
import sys
import time

import pygame
import pygame.gfxdraw

from pathlib import Path

import measure as cc

# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

MICHROMA = "Michroma-Regular.ttf"

W, H = 480, 320

STRIP_H = 44                 # 16-segment readout across the top
STRIP_CELLS = 16             # cells in the tube
GUTTER = 7                   # black gap between panels
MARGIN_X, MARGIN_B = 6, 4

COLS, ROWS = 3, 2
GRID_X = MARGIN_X
GRID_Y = STRIP_H
GRID_W = W - MARGIN_X * 2
GRID_H = H - STRIP_H - MARGIN_B
PANEL_W = (GRID_W - GUTTER * (COLS - 1)) // COLS
PANEL_H = (GRID_H - GUTTER * (ROWS - 1)) // ROWS

VP_X = GRID_X + GRID_W // 2  # vanishing point
VP_Y = GRID_Y + GRID_H // 2

BG        = (2, 3, 6)
PANEL     = (26, 52, 122)    # the blue of the CRT
PANEL_EDGE = (8, 14, 34)
SCAN      = (0, 0, 0, 46)    # scanline overlay
MAGENTA   = (236, 84, 190)
MAGENTA_D = (150, 46, 122)

# The speed plate takes the colour of the band it is in, matching the
# dial skin: green while the motor assists, amber past the cut-off,
# red beyond. Filling the plate rather than the digits puts a large
# patch of colour in the corner, which registers before you have read
# anything -- and it keeps the numerals dark on bright, which stays
# legible in sunlight where red-on-black would be the first to go.
ASSIST_LIMIT = 24.0          # km/h, where the motor stops helping
BAND_GREEN = (38, 156, 66)
BAND_AMBER = (176, 106, 12)
BAND_RED   = (204, 46, 38)
# Dark enough in every band that white numerals stay legible, so the
# ink never has to change with the colour behind it.
BAND_INK = (252, 250, 246)


def speed_band(kmh: float):
    if kmh <= ASSIST_LIMIT:
        return BAND_GREEN
    if kmh <= 30.0:
        return BAND_AMBER
    return BAND_RED
CYAN      = (150, 214, 255)
CYAN_D    = (74, 130, 190)
AMBER     = (255, 168, 44)
AMBER_D   = (16, 9, 2)       # unlit segments, only just visible

# --------------------------------------------------------------------------
# Corridor
# --------------------------------------------------------------------------

RINGS = 6                # rings in the cycle
RING_SKIP = 2            # deepest ones not drawn: the far end reads
                         # better empty than crowded with tiny boxes
RING_MIN, RING_MAX = 0.10, 2.8    # scale at birth and at the far edge
RING_W, RING_H = GRID_W * 0.92, GRID_H * 0.62
RING_SPACING_M = 4.0     # metres of travel between one ring and the next

# Skyscrapers, as a proper perspective projection rather than boxes
# that merely grow. Windows sit on vertical walls either side; the
# depth of each is reduced by the distance travelled and wrapped at
# Z_PERIOD, which is what makes the city endless without storing one.
FOCAL = 250.0            # pixels; sets how fast things widen out
Z_NEAR = 2.2             # closer than this and a window is behind us
Z_PERIOD = 46.0          # metres before the pattern repeats
Z_FAR = 30.0             # beyond this a window is not drawn
CENTRE_CLEAR = 15        # px around the vanishing point kept empty
WIN_W, WIN_H = 0.34, 0.52   # window size in metres
WIN_MAX_W, WIN_MAX_H = 5, 8  # px, so a close one does not fill the panel
WALL_MIN, WALL_MAX = 4.0, 9.5    # metres from the flight path
BUILDINGS = 22


def ring_scale(u: float) -> float:
    """Scale for a ring at normalised depth u in [0,1).

    Exponential, because in a perspective view an object's size grows
    geometrically as it approaches, not linearly.
    """
    return RING_MIN * (RING_MAX / RING_MIN) ** u


# The notch is described by how deep it is and how steep its sides
# are, rather than by a width and a depth separately. Width and depth
# were scaled by the ring's half-width and half-height, which differ,
# so changing one silently changed the angle as well.
NOTCH_SIZE = 0.36   # depth, as a fraction of the half-height
NOTCH_SLOPE = 4.0   # depth over half-width in pixels: the angle


def rounded_rect_points(cx, cy, hw, hh, r, steps=6):
    """Outline of a rounded rectangle whose top and bottom edges dip
    toward the centre.

    That notch is the shape that reads as a "V" in the upper middle
    panel and an inverted one below, and it is what makes the frame
    look like it is rushing at you rather than just growing.
    """
    r = min(r, hw, hh)
    nd = hh * NOTCH_SIZE
    nw = nd / NOTCH_SLOPE
    pts = []

    def arc(cx0, cy0, a0):
        for k in range(steps + 1):
            a = math.radians(a0 + 90.0 * k / steps)
            pts.append((cx0 + r * math.cos(a), cy0 - r * math.sin(a)))

    arc(cx + hw - r, cy - hh + r, 0.0)          # top-right corner
    pts += [(cx + nw, cy - hh), (cx, cy - hh + nd), (cx - nw, cy - hh)]
    arc(cx - hw + r, cy - hh + r, 90.0)         # top-left corner
    arc(cx - hw + r, cy + hh - r, 180.0)        # bottom-left corner
    pts += [(cx - nw, cy + hh), (cx, cy + hh - nd), (cx + nw, cy + hh)]
    arc(cx + hw - r, cy + hh - r, 270.0)        # bottom-right corner
    return pts


class Skyline:
    """Towers either side, seen as their lit windows.

    Drawing the buildings as outlines never reads at this size; what
    says "flying between skyscrapers" is the windows streaming past.
    Each window is a point in space, projected properly, so a column
    of them at one depth becomes a vertical row and successive depths
    fan out exactly as they should.
    """

    def __init__(self, seed: int = 11) -> None:
        rnd = random.Random(seed)
        self.travel = 0.0
        self.windows = []    # (x, y, z)
        for b in range(BUILDINGS):
            side = -1 if b % 2 == 0 else 1
            wx = side * rnd.uniform(WALL_MIN, WALL_MAX)
            z0 = rnd.uniform(0.0, Z_PERIOD)
            top = rnd.uniform(-22.0, -9.0)
            bot = rnd.uniform(9.0, 22.0)
            depth = rnd.uniform(7.0, 14.0)
            cols = max(3, int(depth / 2.2))
            for ci in range(cols):
                z = z0 + depth * (ci + 0.5) / cols
                y = top + 1.6
                while y < bot - 1.2:
                    if rnd.random() < 0.68:
                        self.windows.append((wx, y, z))
                    y += 2.1          # a regular grid reads as a facade

    def advance(self, metres: float) -> None:
        self.travel += metres

    def _z(self, z0: float) -> float:
        return (z0 - self.travel) % Z_PERIOD + Z_NEAR

    def _project(self, x, y, z):
        return VP_X + FOCAL * x / z, VP_Y + FOCAL * y / z

    def draw(self, dst) -> None:
        # Only the windows: the building corners drew as full-height
        # verticals that read as a fence across the panels rather than
        # as structure, and the lit windows carry the shape anyway.
        top_y, bot_y = GRID_Y, GRID_Y + GRID_H
        for x, y, z0 in self.windows:
            z = self._z(z0)
            if z > Z_FAR:
                continue
            sx, sy = self._project(x, y, z)
            # Everything far enough away lands on the vanishing point,
            # which turns into a speckle of noise over the corridor.
            # Keeping a hole there puts the city where it belongs:
            # out to either side, the way you would see it go past.
            if abs(sx - VP_X) < CENTRE_CLEAR:
                continue
            if not (GRID_X - 6 < sx < GRID_X + GRID_W + 6):
                continue
            if not (top_y < sy < bot_y):
                continue
            # Sized by perspective rather than in steps, and taller
            # than wide: a square pixel reads as a star, a small
            # upright rectangle reads as a lit window.
            w = min(WIN_MAX_W, max(2, int(FOCAL * WIN_W / z)))
            h = min(WIN_MAX_H, max(2, int(FOCAL * WIN_H / z)))
            col = CYAN_D if z > 20.0 else CYAN
            pygame.draw.rect(dst, col, (int(sx), int(sy), w, h))


class Corridor:
    """The magenta perspective tunnel."""

    def __init__(self, seed: int = 7) -> None:
        self.phase = 0.0          # ring phase, 0..1

    def advance(self, metres: float) -> None:
        self.phase = (self.phase + metres / (RING_SPACING_M * RINGS)) % 1.0

    def draw(self, dst: pygame.Surface) -> None:
        self._draw_rings(dst)

    def _draw_rings(self, dst) -> None:
        for i in range(RINGS):
            u = (self.phase + i / RINGS) % 1.0
            if u < RING_SKIP / RINGS:
                continue
            s = ring_scale(u)
            hw, hh = RING_W * 0.5 * s, RING_H * 0.5 * s
            if hw < 3 or hh < 3:
                continue
            # Three widths rather than a smooth ramp: a hard step reads
            # as depth far better than a gradual one at this size.
            if u > 0.78:
                col, wd = MAGENTA, 4
            elif u > 0.52:
                col, wd = MAGENTA, 2
            else:
                col, wd = MAGENTA_D, 1
            pts = rounded_rect_points(VP_X, VP_Y, hw, hh, min(hw, hh) * 0.55)
            pygame.draw.lines(dst, col, True,
                              [(int(x), int(y)) for x, y in pts], wd)


# --------------------------------------------------------------------------
# 16-segment display
# --------------------------------------------------------------------------

SEG_FONT = {
    # 16-segment patterns. a1/a2 top, b/c right, d1/d2 bottom, e/f left,
    # g1/g2 middle, h/i/j and k/l/m the upper and lower diagonals.
    "0": "a1 a2 b c d1 d2 e f",
    "1": "b c",
    "2": "a1 a2 b g1 g2 e d1 d2",
    "3": "a1 a2 b c d1 d2 g1 g2",   # full crossbar: clearer than g2 alone
    "4": "f g1 g2 b c",
    "5": "a1 a2 f g1 g2 c d1 d2",
    "6": "a1 a2 f e d1 d2 c g1 g2",
    "7": "a1 a2 b c",
    "8": "a1 a2 b c d1 d2 e f g1 g2",
    "9": "a1 a2 b c d1 d2 f g1 g2",
    "A": "a1 a2 b c e f g1 g2",
    "C": "a1 a2 f e d1 d2",
    "D": "a1 a2 b c d1 d2 i l",
    "E": "a1 a2 f e d1 d2 g1 g2",
    "H": "b c e f g1 g2",
    "I": "a1 a2 d1 d2 i l",
    "K": "e f g1 j m",
    "M": "b c e f h j",
    "N": "b c e f h m",
    "P": "a1 a2 b e f g1 g2",
    "S": "a1 a2 f g1 g2 c d1 d2",
    "T": "a1 a2 i l",
    "U": "b c d1 d2 e f",
    "-": "g1 g2",
    "/": "j k",
    " ": "",
}


class SegDisplay:
    """A 16-segment vacuum-fluorescent readout, drawn as real segments.

    Each cell is an upright rectangle. Inside it the segments are
    shaped bars whose ends are cut at 45 degrees, so they meet at the
    corners the way the physical part does; the italic look comes from
    shearing those bars, not from slanting the cell. A round decimal
    point sits outside the body at bottom right, and is part of the
    cell whether it is lit or not.
    """

    # Drawing real segment shapes needs room: below about 16 px of
    # body width the 45-degree cuts eat the bars and the glyph breaks
    # into blobs. That sets the cell size, and the cell size sets how
    # many characters fit across 480 px.
    CH = 32                  # body height
    BODY_W = 16              # body width at the baseline, before shear
    SLANT = 0.15             # horizontal shift per unit of height
    THICK = 3.4              # segment thickness
    THIN = 0.80              # diagonals and centre bars, relative
    INSET = 0.22             # how far each end is pulled back, x THICK
    DP_R = 2.2               # decimal point radius
    DP_GAP = 2.5             # from the body's right edge to the dot
    PITCH = 29               # cell to cell
    SS = 3                   # supersampling; see render()

    def __init__(self, lit, unlit=None) -> None:
        self.lit = lit
        self.unlit = unlit
        self._cache: dict[tuple[str, bool], pygame.Surface] = {}
        self._geom = self._build()

    # -- geometry ---------------------------------------------------

    def _sheared(self, x, y):
        return x + (self.CH - y) * self.SLANT, y

    def _build(self):
        """Segment polygons, in cell coordinates."""
        w, h = self.BODY_W, self.CH
        x0, x1, x2 = 0.0, w / 2, w
        y0, y1, y2 = 0.0, h / 2, h
        spec = {
            "a1": ((x0, y0), (x1, y0), 1.0), "a2": ((x1, y0), (x2, y0), 1.0),
            "b":  ((x2, y0), (x2, y1), 1.0), "c":  ((x2, y1), (x2, y2), 1.0),
            "d1": ((x0, y2), (x1, y2), 1.0), "d2": ((x1, y2), (x2, y2), 1.0),
            "e":  ((x0, y1), (x0, y2), 1.0), "f":  ((x0, y0), (x0, y1), 1.0),
            "g1": ((x0, y1), (x1, y1), 1.0), "g2": ((x1, y1), (x2, y1), 1.0),
            "h":  ((x0, y0), (x1, y1), self.THIN),
            "i":  ((x1, y0), (x1, y1), self.THIN),
            "j":  ((x2, y0), (x1, y1), self.THIN),
            "k":  ((x0, y2), (x1, y1), self.THIN),
            "l":  ((x1, y2), (x1, y1), self.THIN),
            "m":  ((x2, y2), (x1, y1), self.THIN),
        }
        out = {}
        for name, (p0, p1, scale) in spec.items():
            out[name] = self._bar(p0, p1, self.THICK * scale)
        return out

    def _bar(self, a, b, thick):
        """A bar with both ends cut back at 45 degrees.

        The cut is what lets two segments meet at a corner without
        overlapping, and it is the shape that reads as a real display
        rather than as a stroke font.
        """
        half = thick / 2.0
        ax, ay = a
        bx, by = b
        dx, dy = bx - ax, by - ay
        length = math.hypot(dx, dy)
        ux, uy = dx / length, dy / length
        nx, ny = -uy, ux
        s = self.THICK * self.INSET
        ax, ay = ax + ux * s, ay + uy * s
        bx, by = bx - ux * s, by - uy * s
        e, tip = half * 0.70, half * 0.30
        raw = [
            (ax + ux * e + nx * half, ay + uy * e + ny * half),
            (bx - ux * e + nx * half, by - uy * e + ny * half),
            (bx + nx * tip, by + ny * tip),
            (bx - nx * tip, by - ny * tip),
            (bx - ux * e - nx * half, by - uy * e - ny * half),
            (ax + ux * e - nx * half, ay + uy * e - ny * half),
            (ax - nx * tip, ay - ny * tip),
            (ax + nx * tip, ay + ny * tip),
        ]
        return [self._sheared(x, y) for x, y in raw]

    @property
    def span(self):
        return self.BODY_W + self.CH * self.SLANT

    # -- text -------------------------------------------------------

    @staticmethod
    def to_cells(text: str) -> list[tuple[str, bool]]:
        """Split text into physical cells.

        A "." does not take a cell of its own: it lights the decimal
        point of the character before it, which is how the real part is
        laid out and why "12.345" needs five cells rather than six.
        """
        cells: list[list] = []
        for ch in text:
            if ch == "." and cells and not cells[-1][1]:
                cells[-1][1] = True
            else:
                cells.append([ch, False])
        return [(c, d) for c, d in cells]

    def cell_count(self, text: str) -> int:
        return len(self.to_cells(text))

    def _glyph(self, ch: str, dp: bool) -> pygame.Surface:
        """One cell, cached.

        Caching per glyph rather than per string matters on the Pi: the
        speed changes every frame, so a whole-string cache would never
        hit and we would redraw sixteen cells at SS scale twenty times
        a second. There are only a few dozen distinct cells.
        """
        key = (ch, dp)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        w, h = self.PITCH, self.CH + 6
        k = self.SS
        # Draw at SS times the size and scale down. Filling and then
        # stroking an anti-aliased outline over the same polygon blends
        # the edge twice and leaves it looking chewed; supersampling
        # avoids that and hides the rounding of vertices to whole pixels.
        big = pygame.Surface((w * k, h * k), pygame.SRCALPHA)
        dcol = self.lit if dp else self.unlit
        if dcol is not None:
            dpx = self.span + self.DP_GAP + self.DP_R
            pygame.draw.circle(
                big, dcol, (int(dpx * k), int((self.CH - self.DP_R + 3) * k)),
                int(self.DP_R * k))
        if ch == ":":
            for yy in (self.CH * 0.34, self.CH * 0.70):
                x, y = self._sheared(self.BODY_W / 2, yy)
                pygame.draw.circle(big, self.lit,
                                   (int(x * k), int((y + 3) * k)),
                                   int(self.DP_R * k))
        else:
            on = set(SEG_FONT.get(ch.upper(), "").split())
            for name, poly in self._geom.items():
                col = self.lit if name in on else self.unlit
                if col is None:
                    continue
                pygame.draw.polygon(
                    big, col, [(x * k, (y + 3) * k) for x, y in poly])
        surf = pygame.transform.smoothscale(big, (w, h))
        self._cache[key] = surf
        return surf

    def render(self, text: str) -> pygame.Surface:
        cells = self.to_cells(text)
        w, h = self.PITCH * len(cells) + 6, self.CH + 6
        surf = pygame.Surface((w, h), pygame.SRCALPHA)
        for idx, (ch, dp) in enumerate(cells):
            surf.blit(self._glyph(ch, dp), (idx * self.PITCH, 0))
        return surf

    def blit(self, dst, text, center=None, midleft=None, midright=None):
        s = self.render(text)
        r = s.get_rect()
        if center:
            r.center = center
        elif midleft:
            r.midleft = midleft
        else:
            r.midright = midright
        dst.blit(s, r)
        return r


# --------------------------------------------------------------------------
# Static background: panels, gutters, scanlines
# --------------------------------------------------------------------------

def build_background() -> pygame.Surface:
    s = pygame.Surface((W, H))
    s.fill(BG)
    for r in range(ROWS):
        for c in range(COLS):
            x = GRID_X + c * (PANEL_W + GUTTER)
            y = GRID_Y + r * (PANEL_H + GUTTER)
            pygame.draw.rect(s, PANEL, (x, y, PANEL_W, PANEL_H))
            pygame.draw.rect(s, PANEL_EDGE, (x, y, PANEL_W, PANEL_H), 1)
    return s


def panel_mask() -> pygame.Surface:
    """Black over everything that is not a panel, so the corridor is
    clipped to the six windows instead of bleeding into the gutters."""
    m = pygame.Surface((W, H), pygame.SRCALPHA)
    m.fill(BG)
    for r in range(ROWS):
        for c in range(COLS):
            x = GRID_X + c * (PANEL_W + GUTTER)
            y = GRID_Y + r * (PANEL_H + GUTTER)
            pygame.draw.rect(m, (0, 0, 0, 0), (x, y, PANEL_W, PANEL_H))
    return m


# --------------------------------------------------------------------------

BIG_PAD = 7              # padding inside the plate behind the big number


def draw_big_speed(screen, font, unit_font, kmh: float) -> None:
    """The speed as a whole number, bottom right, over everything.

    Drawn last so the mask and the scanlines do not touch it, and on a
    dark plate because the corridor sweeps through that corner and a
    bare glyph would disappear against a magenta ring.
    """
    col = speed_band(kmh)
    num = font.render(f"{min(99, int(round(kmh))):d}", True, BAND_INK)
    unit = unit_font.render("km/h", True, BAND_INK)
    gap = 5
    # The slot is always two digits wide, so the plate does not change
    # size when the speed crosses ten, and the units digit stays put.
    num_w = font.size("88")[0]
    cw = num_w + gap + unit.get_width()
    ch = num.get_height()
    plate = pygame.Rect(0, 0, cw + BIG_PAD * 2, ch + BIG_PAD * 2)
    plate.bottomright = (GRID_X + GRID_W - 6, GRID_Y + GRID_H - 6)
    shade = pygame.Surface(plate.size, pygame.SRCALPHA)
    shade.fill((*col, 232))
    screen.blit(shade, plate)
    pygame.draw.rect(screen, [int(c * 0.55) for c in col], plate, 2)
    nx = plate.x + BIG_PAD
    ny = plate.y + BIG_PAD
    screen.blit(num, (nx + num_w - num.get_width(), ny))
    # the unit sits on the number's baseline, so it reads as one label
    screen.blit(unit, (nx + num_w + gap,
                       ny + ch - unit.get_height() - 6))


def draw_frame(screen, bg, mask, scanlay, dm, big_font, unit_font,
               corridor, sky, st, shown, peak):
    screen.blit(bg, (0, 0))
    sky.draw(screen)
    corridor.draw(screen)
    screen.blit(mask, (0, 0))       # clip the corridor to the panels
    screen.blit(scanlay, (0, 0))    # scanlines over the graphics too

    # top strip
    pygame.draw.rect(screen, (6, 6, 9), (0, 0, W, STRIP_H))
    pygame.draw.line(screen, (30, 22, 8), (0, STRIP_H - 1), (W, STRIP_H - 1))
    mid = STRIP_H // 2
    # The tube carries distance and time only; the speed lives in the
    # corner where it can be read at a glance. Spelling out KM is what
    # the extra segments are for.
    dm.blit(screen, f"{st.distance_m / 1000.0:6.3f}KM", midleft=(6, mid))
    right = st.fault[:5] if st.fault else time.strftime("%H:%M")
    dm.blit(screen, right, midright=(W - 6, mid))
    draw_big_speed(screen, big_font, unit_font, shown)


def main() -> int:
    ap = argparse.ArgumentParser(description="cyclometer spinner display")
    ap.add_argument("--simulate", action="store_true")
    ap.add_argument("--gpiozero", action="store_true")
    ap.add_argument("--pin", type=int, default=cc.PULSE_PIN)
    ap.add_argument("--circumference", type=float, default=cc.CIRCUMFERENCE_M)
    ap.add_argument("--magnets", type=int, default=cc.MAGNETS)
    ap.add_argument("--windowed", action="store_true")
    ap.add_argument("--record", type=float, default=None, metavar="SECONDS")
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--shot", type=str, default=None,
                    help="speed:km, render one frame and exit")
    args = ap.parse_args()

    if args.shot or args.record:
        os.environ.setdefault("SDL_VIDEODRIVER", "dummy")

    pygame.init()
    pygame.mouse.set_visible(False)
    flags = (0 if (args.windowed or args.shot or args.record)
             else pygame.FULLSCREEN)
    screen = pygame.display.set_mode((W, H), flags)
    pygame.display.set_caption("CYCLOMETER")

    def display_font(sz, fallback_sz):
        """Michroma if it is bundled, otherwise the system monospace.

        Michroma is the closest free stand-in for Eurostile Bold
        Extended, the wide squared-off face the film uses on its
        readouts. Synthetic bold gives it the weight it needs at this
        size; the real thing has no bold cut.
        """
        path = Path(__file__).resolve().parent / "fonts" / MICHROMA
        if path.exists():
            f = pygame.font.Font(str(path), sz)
            f.set_bold(True)
            return f
        return mono(fallback_sz)

    def mono(sz):
        for name in ("dejavusansmono", "liberationmono", "couriernew"):
            f = pygame.font.match_font(name, bold=True)
            if f:
                return pygame.font.Font(f, sz)
        return pygame.font.Font(None, sz)

    bg = build_background()
    mask = panel_mask()
    scanlay = pygame.Surface((W, H), pygame.SRCALPHA)
    for y in range(GRID_Y, GRID_Y + GRID_H, 2):
        pygame.draw.line(scanlay, SCAN, (0, y), (W, y))
    dm = SegDisplay(AMBER, AMBER_D)
    big_font = display_font(40, 58)
    unit_font = display_font(12, 17)
    corridor = Corridor()
    sky = Skyline()

    if args.shot:
        v, km = (float(x) for x in args.shot.split(":"))
        st = cc.RideState(distance_m=km * 1000.0)
        corridor.advance(7.3)
        sky.advance(7.3)
        draw_frame(screen, bg, mask, scanlay, dm, big_font, unit_font,
                   corridor, sky, st, v, v)
        pygame.display.flip()
        pygame.image.save(screen, "/tmp/spinner.png")
        return 0

    if args.record:
        random.seed(0)
        os.makedirs("frames", exist_ok=True)
        sim = cc.SimulatedSource("", args.pin, args.magnets,
                                 args.circumference)
        sim._t0_ns = sim._last_ns = 0
        model = cc.RideModel(circumference_m=args.circumference,
                             magnets=args.magnets)
        shown = peak = 0.0
        last_d = 0.0
        n = int(args.record * args.fps)
        for i in range(n):
            now = int(i / args.fps * 1e9)
            for t_ns, level in sim.advance(now):
                if level == 0:
                    model.on_pulse(t_ns)
            st = model.tick(now)
            shown += (st.speed_kf_kmh - shown) * 0.30
            peak = max(peak, shown)
            corridor.advance(st.distance_m - last_d)
            sky.advance(st.distance_m - last_d)
            last_d = st.distance_m
            draw_frame(screen, bg, mask, scanlay, dm, big_font, unit_font,
                       corridor, sky, st, shown, peak)
            pygame.image.save(screen, f"frames/{i:04d}.png")
        print(f"{n} frames written to frames/ at {args.fps} fps")
        return 0

    kind = ("simulate" if args.simulate
            else "gpiozero" if args.gpiozero else "auto")
    source = cc.make_source(kind, args.pin, args.magnets)
    model = cc.RideModel(circumference_m=args.circumference,
                         magnets=args.magnets)
    logger = cc.CsvLogger(cc.LOG_DIR)

    shown = peak = 0.0
    last_d = 0.0
    pending = None
    fps = pygame.time.Clock()
    running = True

    while running:
        for e in pygame.event.get():
            if e.type == pygame.QUIT:
                running = False
            elif e.type == pygame.KEYDOWN and e.key in (pygame.K_ESCAPE,
                                                        pygame.K_q):
                running = False

        for t_ns, level in source.poll(0.0):
            model.set_level(level)
            if level == 0:
                pending = t_ns if model.on_pulse(t_ns) else None
            elif pending is not None:
                model.state.width_s = (t_ns - pending) / 1e9
                if (model.state.width_min_s == 0.0
                        or model.state.width_s < model.state.width_min_s):
                    model.state.width_min_s = model.state.width_s
                logger.log(pending, model.state)
                pending = None

        st = model.tick(time.monotonic_ns())
        shown += (st.speed_kf_kmh - shown) * 0.30
        peak = max(peak, shown)
        corridor.advance(st.distance_m - last_d)
        sky.advance(st.distance_m - last_d)
        last_d = st.distance_m

        draw_frame(screen, bg, mask, scanlay, dm, big_font, unit_font,
                   corridor, sky, st, shown, peak)
        pygame.display.flip()
        fps.tick(20)

    source.close()
    logger.close()
    pygame.quit()
    return 0


if __name__ == "__main__":
    sys.exit(main())
