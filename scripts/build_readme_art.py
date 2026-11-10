"""Build the README artwork in assets/readme/ (development only, never loaded by the plugin).

    python scripts/build_readme_art.py            # write the SVGs
    python scripts/build_readme_art.py --png      # also render PNGs with headless Chrome or Edge

Art direction "Herald": Nous blue is Hermes's own work, gold is only ever the moment you approve.
The wordmark is drawn as pixel blocks (like Hermes Agent's own banner) because GitHub shows SVGs
as images, where web fonts cannot load. Animations are CSS only and stop under reduced motion.
"""

from __future__ import annotations

import argparse
import math
import random
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets" / "readme"

INK = "#0E0E1F"
INDIGO = "#19192F"
NOUS = "#0000F2"
NOUS_LIT = "#4D4DFF"
SILVER = "#F2F2F2"
SILVER_2 = "#C9C9DD"
SILVER_3 = "#8C8CB0"
DIM = "#A6A6C4"
GOLD = "#FFBF00"
GOLD_HI = "#FFD700"
BRONZE = "#CD7F32"
APPS = (("WHATSAPP", "#1FA855"), ("INSTAGRAM", "#C13584"), ("LINKEDIN", "#0A66C2"))
MONO = "ui-monospace, 'SF Mono', 'Cascadia Mono', 'Segoe UI Mono', Menlo, Consolas, monospace"

# Official app marks, unmodified, on a 24 x 24 grid. WhatsApp and Instagram: Simple Icons (MIT),
# the copy Hermes Agent itself ships. LinkedIn: its "in" bug. Each mark keeps its own brand colour.
WHATSAPP_D = ("M17.472 14.382c-.297-.149-1.758-.867-2.03-.967-.273-.099-.471-.148-.67.15-.197.297-.767.966-.94 1.164-.173.199-.347.223-.644.075-.297-.15-1.255-.463-2.39-1.475-.883-.788-1.48-1.761-1.653-2.059-.173-.297-.018-.458.13-.606.134-.133.298-.347.446-.52.149-.174.198-.298.298-.497.099-.198.05-.371-.025-.52-.075-.149-.669-1.612-.916-2.207-.242-.579-.487-.5-.669-.51-.173-.008-.371-.01-.57-.01-.198 0-.52.074-.792.372-.272.297-1.04 1.016-1.04 2.479 0 1.462 1.065 2.875 1.213 3.074.149.198 2.096 3.2 5.077 4.487.709.306 1.262.489 1.694.625.712.227 1.36.195 1.871.118.571-.085 1.758-.719 2.006-1.413.248-.694.248-1.289.173-1.413-.074-.124-.272-.198-.57-.347m-5.421 7.403h-.004a9.87 9.87 0 01-5.031-1.378l-.361-.214-3.741.982.998-3.648-.235-.374a9.86 9.86 0 01-1.51-5.26c.001-5.45 4.436-9.884 9.888-9.884 2.64 0 5.122 1.03 6.988 2.898a9.825 9.825 0 012.893 6.994c-.003 5.45-4.437 9.884-9.885 9.884m8.413-18.297A11.815 11.815 0 0012.05 0C5.495 0 .16 5.335.157 11.892c0 2.096.547 4.142 1.588 5.945L.057 24l6.305-1.654a11.882 11.882 0 005.683 1.448h.005c6.554 0 11.89-5.335 11.893-11.893a11.821 11.821 0 00-3.48-8.413Z")
INSTAGRAM_D = ("M7.0301.084c-1.2768.0602-2.1487.264-2.911.5634-.7888.3075-1.4575.72-2.1228 1.3877-.6652.6677-1.075 1.3368-1.3802 2.127-.2954.7638-.4956 1.6365-.552 2.914-.0564 1.2775-.0689 1.6882-.0626 4.947.0062 3.2586.0206 3.6671.0825 4.9473.061 1.2765.264 2.1482.5635 2.9107.308.7889.72 1.4573 1.388 2.1228.6679.6655 1.3365 1.0743 2.1285 1.38.7632.295 1.6361.4961 2.9134.552 1.2773.056 1.6884.069 4.9462.0627 3.2578-.0062 3.668-.0207 4.9478-.0814 1.28-.0607 2.147-.2652 2.9098-.5633.7889-.3086 1.4578-.72 2.1228-1.3881.665-.6682 1.0745-1.3378 1.3795-2.1284.2957-.7632.4966-1.636.552-2.9124.056-1.2809.0692-1.6898.063-4.948-.0063-3.2583-.021-3.6668-.0817-4.9465-.0607-1.2797-.264-2.1487-.5633-2.9117-.3084-.7889-.72-1.4568-1.3876-2.1228C21.2982 1.33 20.628.9208 19.8378.6165 19.074.321 18.2017.1197 16.9244.0645 15.6471.0093 15.236-.005 11.977.0014 8.718.0076 8.31.0215 7.0301.0839m.1402 21.6932c-1.17-.0509-1.8053-.2453-2.2287-.408-.5606-.216-.96-.4771-1.3819-.895-.422-.4178-.6811-.8186-.9-1.378-.1644-.4234-.3624-1.058-.4171-2.228-.0595-1.2645-.072-1.6442-.079-4.848-.007-3.2037.0053-3.583.0607-4.848.05-1.169.2456-1.805.408-2.2282.216-.5613.4762-.96.895-1.3816.4188-.4217.8184-.6814 1.3783-.9003.423-.1651 1.0575-.3614 2.227-.4171 1.2655-.06 1.6447-.072 4.848-.079 3.2033-.007 3.5835.005 4.8495.0608 1.169.0508 1.8053.2445 2.228.408.5608.216.96.4754 1.3816.895.4217.4194.6816.8176.9005 1.3787.1653.4217.3617 1.056.4169 2.2263.0602 1.2655.0739 1.645.0796 4.848.0058 3.203-.0055 3.5834-.061 4.848-.051 1.17-.245 1.8055-.408 2.2294-.216.5604-.4763.96-.8954 1.3814-.419.4215-.8181.6811-1.3783.9-.4224.1649-1.0577.3617-2.2262.4174-1.2656.0595-1.6448.072-4.8493.079-3.2045.007-3.5825-.006-4.848-.0608M16.953 5.5864A1.44 1.44 0 1 0 18.39 4.144a1.44 1.44 0 0 0-1.437 1.4424M5.8385 12.012c.0067 3.4032 2.7706 6.1557 6.173 6.1493 3.4026-.0065 6.157-2.7701 6.1506-6.1733-.0065-3.4032-2.771-6.1565-6.174-6.1498-3.403.0067-6.156 2.771-6.1496 6.1738M8 12.0077a4 4 0 1 1 4.008 3.9921A3.9996 3.9996 0 0 1 8 12.0077")
LINKEDIN_D = ("M20.447 20.452h-3.554v-5.569c0-1.328-.027-3.037-1.852-3.037-1.853 0-2.136 1.445-2.136 2.939v5.667H9.351V9h3.414v1.561h.046c.477-.9 1.637-1.85 3.37-1.85 3.601 0 4.267 2.37 4.267 5.455v6.286zM5.337 7.433c-1.144 0-2.063-.926-2.063-2.065 0-1.138.92-2.063 2.063-2.063 1.14 0 2.064.925 2.064 2.063 0 1.139-.925 2.065-2.064 2.065zm1.782 13.019H3.555V9h3.564v11.452zM22.225 0H1.771C.792 0 0 .774 0 1.729v20.542C0 23.227.792 24 1.771 24h20.451C23.2 24 24 23.227 24 22.271V1.729C24 .774 23.2 0 22.222 0h.003z")
IG_GRADIENT = ('<radialGradient id="igGrad" cx="0.3" cy="1.07" r="1.5">'
               '<stop offset="0" stop-color="#FDF497"/><stop offset="0.05" stop-color="#FDF497"/>'
               '<stop offset="0.45" stop-color="#FD5949"/><stop offset="0.6" stop-color="#D6249F"/>'
               '<stop offset="0.9" stop-color="#285AEB"/></radialGradient>')


def logo(app: str, x: float, y: float, size: float) -> str:
    """One official app mark at (x, y), size px square. Needs IG_GRADIENT in <defs> for Instagram."""
    inner = {
        "WHATSAPP": f'<path d="{WHATSAPP_D}" fill="#25D366"/>',
        "INSTAGRAM": f'<path d="{INSTAGRAM_D}" fill="url(#igGrad)"/>',
        "LINKEDIN": f'<rect x="2" y="2" width="20" height="20" fill="#FFFFFF"/><path d="{LINKEDIN_D}" fill="#0A66C2"/>',
    }[app]
    return f'<svg x="{x}" y="{y}" width="{size}" height="{size}" viewBox="0 0 24 24" overflow="visible">{inner}</svg>'

# 7-row block glyphs in the spirit of Hermes's ANSI-shadow logo: two-cell stems, '#' = filled.
GLYPHS = {
    "U": ["##...##", "##...##", "##...##", "##...##", "##...##", "##...##", ".#####."],
    "N": ["##...##", "###..##", "####.##", "##.####", "##..###", "##...##", "##...##"],
    "I": ["######", "..##..", "..##..", "..##..", "..##..", "..##..", "######"],
    "F": ["#######", "##.....", "##.....", "######.", "##.....", "##.....", "##....."],
    "E": ["#######", "##.....", "##.....", "######.", "##.....", "##.....", "#######"],
    "D": ["######.", "##...##", "##...##", "##...##", "##...##", "##...##", "######."],
    "B": ["######.", "##...##", "##...##", "######.", "##...##", "##...##", "######."],
    "O": [".#####.", "##...##", "##...##", "##...##", "##...##", "##...##", ".#####."],
    "X": ["##...##", "##...##", ".##.##.", "..###..", ".##.##.", "##...##", "##...##"],
}


def word_cells(word: str, gap: int = 1) -> tuple[list[tuple[int, int]], int]:
    """Pixel cells (x, y) for a word and its width in cells."""
    cells, x = [], 0
    for ch in word:
        g = GLYPHS[ch]
        for row, line in enumerate(g):
            for col, c in enumerate(line):
                if c == "#":
                    cells.append((x + col, row))
        x += len(g[0]) + gap
    return cells, x - gap


def outline_path(cells: list[tuple[int, int]], px: int, ox: float, oy: float) -> str:
    """The outer edges of a union of pixel cells, as one path."""
    filled = set(cells)
    d = []
    for x, y in cells:
        X, Y = ox + x * px, oy + y * px
        if (x, y - 1) not in filled:
            d.append(f"M{X} {Y}h{px}")
        if (x, y + 1) not in filled:
            d.append(f"M{X} {Y + px}h{px}")
        if (x - 1, y) not in filled:
            d.append(f"M{X} {Y}v{px}")
        if (x + 1, y) not in filled:
            d.append(f"M{X + px} {Y}v{px}")
    return "".join(d)


def wordmark(lines: list[str], px: int, x: float, y: float, line_gap: int, bands: tuple[str, str, str],
             echo: tuple[str, str], gid: str) -> tuple[str, float, float]:
    """Pixel wordmark with three colour bands and two offset echo outlines. Returns svg, width, height."""
    parts_echo2, parts_echo1, parts_fill = [], [], []
    width = 0
    for li, word in enumerate(lines):
        cells, wcells = word_cells(word)
        oy = y + li * (7 * px + line_gap)
        width = max(width, wcells * px)
        parts_echo2.append(outline_path(cells, px, x + px * 1.4, oy + px * 1.4))
        parts_echo1.append(outline_path(cells, px, x + px * 0.7, oy + px * 0.7))
        for cx, cy in cells:
            band = bands[0] if cy < 3 else bands[1] if cy < 5 else bands[2]
            parts_fill.append(f'<rect x="{x + cx * px}" y="{oy + cy * px}" width="{px + 0.4}" height="{px + 0.4}" fill="{band}"/>')
    height = len(lines) * 7 * px + (len(lines) - 1) * line_gap
    svg = (
        f'<g id="{gid}">'
        f'<path class="e2" d="{"".join(parts_echo2)}" fill="none" stroke="{echo[1]}" stroke-width="1.2" stroke-opacity="0.9"/>'
        f'<path class="e1" d="{"".join(parts_echo1)}" fill="none" stroke="{echo[0]}" stroke-width="1.2"/>'
        f'<g shape-rendering="crispEdges">{"".join(parts_fill)}</g>'
        "</g>"
    )
    return svg, width, height


def caduceus(cx: float, cy: float, s: float = 1.0) -> str:
    """The herald's staff: a rod, two serpents that cross it three times, and a pair of wings."""
    top, bottom = cy - 64 * s, cy + 78 * s
    out = [f'<line x1="{cx}" y1="{top}" x2="{cx}" y2="{bottom}" stroke="{SILVER}" stroke-width="{3 * s}" stroke-linecap="round"/>',
           f'<circle cx="{cx}" cy="{top - 6 * s}" r="{6 * s}" fill="{SILVER}"/>']
    for sign in (1, -1):
        pts = []
        y0, y1 = bottom - 6 * s, top + 22 * s
        n = 60
        for i in range(n + 1):
            t = i / n
            yy = y0 + (y1 - y0) * t
            amp = (15 - 4 * t) * s
            xx = cx + sign * amp * math.sin(t * 3 * math.pi)
            pts.append(f"{xx:.1f},{yy:.1f}")
        # the head turns in towards the rod
        hx, hy = cx + sign * 9 * s, y1 - 8 * s
        out.append(f'<polyline points="{" ".join(pts)} {hx:.1f},{hy:.1f}" fill="none" stroke="{SILVER}" stroke-width="{2.4 * s}" stroke-linecap="round" stroke-linejoin="round"/>')
        out.append(f'<circle cx="{hx:.1f}" cy="{hy:.1f}" r="{2.6 * s}" fill="{SILVER}"/>')
        # wing: three feathers sweeping up and out
        for k, (ln, rise) in enumerate(((46, 20), (38, 10), (28, 2))):
            wy = top + (4 + k * 6) * s
            out.append(f'<path d="M{cx + sign * 4 * s} {wy} q{sign * ln * 0.5 * s} {-rise * s - 10 * s} {sign * ln * s} {-rise * s}" '
                       f'fill="none" stroke="{SILVER}" stroke-width="{2 * s}" stroke-linecap="round"/>')
    return "".join(out)


def halftone_disc(cx: float, cy: float, r: float, step: float, color: str) -> str:
    dots = []
    y = cy - r
    while y <= cy + r:
        x = cx - r + ((y - cy) / step % 2) * step / 2
        while x <= cx + r:
            d = math.hypot(x - cx, y - cy)
            if d < r:
                rad = 1.9 * (1 - d / r) ** 1.3 + 0.25
                dots.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{rad:.2f}"/>')
            x += step
        y += step
    return f'<g fill="{color}">{"".join(dots)}</g>'


def rays(cx: float, cy: float, n: int, rmin: float, rmax: float, seed: int, opacity: tuple[float, float],
         width: float, dashed: float) -> str:
    rnd = random.Random(seed)
    out = []
    for i in range(n):
        a = (i / n) * 2 * math.pi + rnd.uniform(-0.03, 0.03)
        r0 = rmin + rnd.uniform(0, 14)
        r1 = rnd.uniform(rmin + 40, rmax)
        x0, y0 = cx + r0 * math.cos(a), cy + r0 * math.sin(a)
        x1, y1 = cx + r1 * math.cos(a), cy + r1 * math.sin(a)
        op = rnd.uniform(*opacity)
        dash = ' stroke-dasharray="2 4"' if rnd.random() < dashed else ""
        out.append(f'<line x1="{x0:.1f}" y1="{y0:.1f}" x2="{x1:.1f}" y2="{y1:.1f}" stroke-opacity="{op:.2f}"{dash}/>')
    return f'<g stroke="{NOUS_LIT}" stroke-width="{width}">{"".join(out)}</g>'


# One 8-second story, told the same way every loop: three messages arrive, the herald takes them in,
# and only then does the gold line (your yes) light up. Behind it, the light turns slowly in two
# layers at different speeds, and a fine grain drifts, so the picture has depth even when still.
HERO_CSS = """<style>
.e1{{animation:echo 7s ease-in-out infinite alternate}}
@keyframes echo{{from{{transform:translate(0,0)}}to{{transform:translate(1.6px,1.2px)}}}}
.grain{{animation:drift 36s linear infinite}}
@keyframes drift{{from{{transform:translate(0,0)}}to{{transform:translate(-16px,-8px)}}}}
.rf,.rn,.halo,.ring{{transform-origin:{nx}px {cy}px}}
.rf{{animation:spin 260s linear infinite}}
.rn{{animation:spin 170s linear infinite reverse}}
@keyframes spin{{to{{transform:rotate(360deg)}}}}
.halo{{animation:breathe 8s ease-in-out infinite}}
@keyframes breathe{{0%,100%{{transform:scale(.97);opacity:.7}}40%{{transform:scale(1.03);opacity:1}}}}
.tail,.head{{opacity:0}}
.tail{{stroke-dasharray:16 200;animation:tail 8s cubic-bezier(.45,0,.25,1) infinite}}
.head{{stroke-dasharray:3 200;animation:head 8s cubic-bezier(.45,0,.25,1) infinite}}
.d1{{animation-delay:.3s}}.d2{{animation-delay:.6s}}
@keyframes tail{{0%{{stroke-dashoffset:16;opacity:0}}3%{{opacity:.45}}27%{{stroke-dashoffset:-100;opacity:.45}}29%,100%{{stroke-dashoffset:-100;opacity:0}}}}
@keyframes head{{0%{{stroke-dashoffset:3;opacity:0}}3%{{opacity:1}}27%{{stroke-dashoffset:-113;opacity:1}}29%,100%{{stroke-dashoffset:-113;opacity:0}}}}
.ring{{opacity:0;animation:ripple 8s cubic-bezier(.2,0,0,1) infinite}}
.ring2{{animation-delay:.25s}}
@keyframes ripple{{0%,33%{{transform:scale(1);opacity:0}}34%{{opacity:.75}}52%,100%{{transform:scale(2.1);opacity:0}}}}
.gold-draw{{stroke-dasharray:100 100;animation:ignite 8s cubic-bezier(.65,0,.35,1) infinite}}
@keyframes ignite{{0%,40%{{stroke-dashoffset:100;opacity:1}}58%{{stroke-dashoffset:0;opacity:1}}84%{{stroke-dashoffset:0;opacity:1}}95%,100%{{stroke-dashoffset:0;opacity:0}}}}
.lit{{animation:lit 8s ease-in-out infinite}}
@keyframes lit{{0%,52%{{opacity:.4}}60%,84%{{opacity:1}}95%,100%{{opacity:.4}}}}
.slice{{opacity:0}}
.s1{{animation:glitch 13s steps(1) infinite 5.2s}}
.s2{{animation:glitch 13s steps(1) infinite 5.32s}}
@keyframes glitch{{0%{{opacity:1;transform:translateX(9px)}}0.8%{{transform:translateX(-5px)}}1.6%{{opacity:1;transform:translateX(3px)}}2.2%,100%{{opacity:0;transform:none}}}}
@media (prefers-reduced-motion: reduce){{
.e1,.grain,.rf,.rn,.halo,.ring,.tail,.head,.gold-draw,.lit,.slice{{animation:none}}
.gold-draw,.lit{{opacity:1;stroke-dashoffset:0}}
}}
</style>"""


def hero(width: int = 1200, height: int = 480) -> str:
    cy = height / 2
    node_x = 930
    pad = 64
    mark, mw, mh = wordmark(["UNIFIED", "INBOX"], 9, pad, cy - 104, 22,
                            (SILVER, SILVER_2, SILVER_3), (NOUS_LIT, NOUS), "mark")
    text_y = cy - 104 + mh + 46

    streams, pulses = [], []
    ys = (cy - 96, cy, cy + 96)
    for i, ((name, _), y) in enumerate(zip(APPS, ys)):
        sx = 700
        path = f"M{sx + 34} {y} C {sx + 130} {y}, {node_x - 120} {cy}, {node_x - 54} {cy}"
        streams.append(f'<path d="{path}" fill="none" stroke="{SILVER_3}" stroke-width="1.1" stroke-opacity="0.6"/>')
        streams.append(logo(name, sx, y - 12, 24))
        streams.append(f'<text x="{sx - 14}" y="{y + 4.5}" text-anchor="end" font-family="{MONO}" font-size="12" letter-spacing="1.6" fill="{DIM}">{name}</text>')
        d = f" d{i}" if i else ""
        pulses.append(f'<path class="tail{d}" d="{path}" fill="none" stroke="{SILVER}" stroke-width="2" stroke-linecap="round" pathLength="100"/>')
        pulses.append(f'<path class="head{d}" d="{path}" fill="none" stroke="#FFFFFF" stroke-width="3" stroke-linecap="round" pathLength="100"/>')

    out_x0, out_x1 = node_x + 58, width - 56
    gold = (f'<line x1="{out_x0}" y1="{cy}" x2="{out_x1}" y2="{cy}" stroke="{GOLD}" stroke-width="1.6" stroke-opacity="0.3"/>'
            f'<line class="gold-draw" x1="{out_x0}" y1="{cy}" x2="{out_x1}" y2="{cy}" stroke="{GOLD}" stroke-width="2.2" pathLength="100"/>'
            f'<rect class="lit" x="{out_x1}" y="{cy - 6}" width="12" height="12" fill="{GOLD}"/>'
            f'<text class="lit" x="{out_x1 + 12}" y="{cy - 20}" text-anchor="end" font-family="{MONO}" font-size="12" letter-spacing="1.6" fill="{GOLD}">YOUR YES</text>'
            f'<text x="{out_x1 + 12}" y="{cy + 30}" text-anchor="end" font-family="{MONO}" font-size="12" letter-spacing="1.6" fill="{DIM}">THEN SENT</text>')

    band_a = (cy - 104 + 9 * 2, 16)
    band_b = (cy - 104 + 7 * 9 + 22 + 9 * 4, 12)
    facts = "22 TOOLS FOR HERMES  ·  ONE /inbox COMMAND  ·  LOCAL-FIRST  ·  NOTHING LEAVES WITHOUT YOUR YES"
    return f"""<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-labelledby="t d">
<title id="t">unified-inbox, a Hermes Agent plugin</title>
<desc id="d">WhatsApp, Instagram and LinkedIn messages flow into Hermes, drawn as the herald's staff. One gold line leaves it: a message goes out only after your yes.</desc>
{HERO_CSS.format(nx=node_x, cy=cy)}
<defs>
{IG_GRADIENT}
<pattern id="scan" width="4" height="3" patternUnits="userSpaceOnUse"><rect width="4" height="1" fill="#FFFFFF" fill-opacity="0.03"/></pattern>
<pattern id="dots" width="8" height="8" patternUnits="userSpaceOnUse"><circle cx="2" cy="2" r="0.7" fill="{NOUS_LIT}"/></pattern>
<radialGradient id="glow" cx="{node_x}" cy="{cy}" r="300" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="{NOUS}" stop-opacity="0.45"/><stop offset="0.55" stop-color="{NOUS}" stop-opacity="0.12"/><stop offset="1" stop-color="{NOUS}" stop-opacity="0"/></radialGradient>
<radialGradient id="fade" cx="{node_x}" cy="{cy}" r="330" gradientUnits="userSpaceOnUse"><stop offset="0.25" stop-color="#FFFFFF"/><stop offset="1" stop-color="#FFFFFF" stop-opacity="0"/></radialGradient>
<mask id="vignette"><rect x="640" width="{width - 640}" height="{height}" fill="url(#fade)"/></mask>
<clipPath id="right"><rect x="640" width="{width - 640}" height="{height}"/></clipPath>
<clipPath id="c1"><rect x="0" y="{band_a[0]}" width="{width}" height="{band_a[1]}"/></clipPath>
<clipPath id="c2"><rect x="0" y="{band_b[0]}" width="{width}" height="{band_b[1]}"/></clipPath>
</defs>
<rect width="{width}" height="{height}" fill="{INK}"/>
<g clip-path="url(#right)">
<g mask="url(#vignette)" opacity="0.5"><g class="grain"><rect x="600" y="-20" width="{width - 560}" height="{height + 40}" fill="url(#dots)"/></g></g>
<rect x="640" width="{width - 640}" height="{height}" fill="url(#glow)"/>
<g mask="url(#vignette)">
<g class="rf">{rays(node_x, cy, 150, 64, 380, 11, (0.06, 0.22), 0.7, 0.5)}</g>
<g class="rn">{rays(node_x, cy, 64, 72, 320, 7, (0.16, 0.42), 1.0, 0.2)}</g>
</g>
</g>
<g class="halo">{halftone_disc(node_x, cy, 100, 8, NOUS_LIT)}</g>
<circle class="ring" cx="{node_x}" cy="{cy}" r="52" fill="none" stroke="{NOUS_LIT}" stroke-width="1.2"/>
<circle class="ring ring2" cx="{node_x}" cy="{cy}" r="52" fill="none" stroke="{SILVER_2}" stroke-width="0.8"/>
<circle cx="{node_x}" cy="{cy}" r="52" fill="{INK}" stroke="{NOUS_LIT}" stroke-width="1"/>
{"".join(streams)}
{"".join(pulses)}
{gold}
{caduceus(node_x, cy + 4, 0.58)}
<g transform="translate({pad + 6} {cy - 145})">{caduceus(0, 0, 0.13)}</g>
<text x="{pad + 22}" y="{cy - 140}" font-family="{MONO}" font-size="12" letter-spacing="2" fill="{DIM}">A HERMES AGENT PLUGIN</text>
{mark}
<g class="slice s1" clip-path="url(#c1)"><use xlink:href="#mark" href="#mark"/></g>
<g class="slice s2" clip-path="url(#c2)"><use xlink:href="#mark" href="#mark"/></g>
<text x="{pad}" y="{text_y}" font-family="{MONO}" font-size="15" fill="{SILVER_2}">WhatsApp, Instagram and LinkedIn in one inbox.</text>
<text x="{pad}" y="{text_y + 26}" font-family="{MONO}" font-size="15" fill="{SILVER_2}">Hermes drafts. <tspan fill="{GOLD}">You send.</tspan></text>
<line x1="{pad}" y1="{height - 52}" x2="{width - pad}" y2="{height - 52}" stroke="{SILVER}" stroke-opacity="0.12"/>
<text x="{pad}" y="{height - 28}" font-family="{MONO}" font-size="11" letter-spacing="1.6" fill="{SILVER_3}">{facts}</text>
<rect width="{width}" height="{height}" fill="url(#scan)" pointer-events="none"/>
</svg>
"""


def box(x, y, w, h, title, lines, stroke, title_fill, dashed=False, num=None):
    dash = ' stroke-dasharray="5 4"' if dashed else ""
    out = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" fill="{INK}" stroke="{stroke}" stroke-width="1.2"{dash}/>']
    label = (f"#{num} " if num else "") + title
    out.append(f'<text x="{x + 14}" y="{y + 26}" font-family="{MONO}" font-size="12" letter-spacing="1.6" fill="{title_fill}">{label}</text>')
    for i, ln in enumerate(lines):
        out.append(f'<text x="{x + 14}" y="{y + 54 + i * 22}" font-family="{MONO}" font-size="13" fill="{SILVER_2}">{ln}</text>')
    return "".join(out)


def arrow(x1, y1, x2, y2, color, width=1.4):
    ang = math.atan2(y2 - y1, x2 - x1)
    a1, a2 = ang + 2.6, ang - 2.6
    head = (f"M{x2} {y2} L{x2 + 8 * math.cos(a1):.1f} {y2 + 8 * math.sin(a1):.1f} "
            f"M{x2} {y2} L{x2 + 8 * math.cos(a2):.1f} {y2 + 8 * math.sin(a2):.1f}")
    return (f'<line x1="{x1}" y1="{y1}" x2="{x2}" y2="{y2}" stroke="{color}" stroke-width="{width}"/>'
            f'<path d="{head}" stroke="{color}" stroke-width="{width}" fill="none" stroke-linecap="round"/>')


DIAGRAM_CSS = """<style>
.current{stroke-dasharray:2 14;animation:current 2.4s linear infinite}
@keyframes current{to{stroke-dashoffset:-16}}
.glint{stroke-dasharray:6 200;stroke-dashoffset:6;opacity:0;animation:glint 8s cubic-bezier(.65,0,.35,1) infinite}
@keyframes glint{0%,45%{stroke-dashoffset:6;opacity:0}46%{opacity:1}74%{stroke-dashoffset:-100;opacity:1}76%,100%{stroke-dashoffset:-100;opacity:0}}
.gate{animation:gate 8s ease-in-out infinite}
@keyframes gate{0%,40%{stroke-opacity:.55}55%,80%{stroke-opacity:1}100%{stroke-opacity:.55}}
@media (prefers-reduced-motion: reduce){.current,.glint,.gate{animation:none}.gate{stroke-opacity:1}}
</style>"""


def frame_svg(w, h, title, desc, body):
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" role="img" aria-labelledby="t d">\n'
            f'<title id="t">{title}</title>\n<desc id="d">{desc}</desc>\n{DIAGRAM_CSS}\n<defs>{IG_GRADIENT}</defs>\n'
            f'<rect width="{w}" height="{h}" fill="{INK}"/>\n{body}\n</svg>\n')


def approval_svg() -> str:
    W, H, y, h = 1200, 400, 92, 176
    b = [f'<text x="40" y="50" font-family="{MONO}" font-size="12" letter-spacing="2" fill="{DIM}">HOW A MESSAGE LEAVES · BLUE IS HERMES, GOLD IS YOU</text>']
    b.append(box(40, y, 250, h, "HERMES DRAFTS", ["in your own style", "saved as a draft", "nothing else happens"], NOUS_LIT, DIM, num=1))
    b.append(box(330, y, 250, h, "RULES CHECKED", ["daily, hourly, per chat", "new-chat cap, quiet hours", "scam and code guard", "45-90 s between sends"], NOUS_LIT, DIM, num=2))
    b.append(box(620, y, 250, h, "YOUR YES", ["Hermes's own prompt", "the person and the app", "the exact words", "the AI cannot skip it"], GOLD, GOLD, num=3))
    b.append(arrow(290, y + h / 2, 326, y + h / 2, NOUS_LIT))
    b.append(arrow(580, y + h / 2, 616, y + h / 2, NOUS_LIT))
    b.append(f'<line class="current" x1="290" y1="{y + h / 2}" x2="320" y2="{y + h / 2}" stroke="#FFFFFF" stroke-width="1.4"/>')
    b.append(f'<line class="current" x1="580" y1="{y + h / 2}" x2="610" y2="{y + h / 2}" stroke="#FFFFFF" stroke-width="1.4"/>')
    b.append(f'<rect class="gate" x="620" y="{y}" width="250" height="{h}" fill="none" stroke="{GOLD_HI}" stroke-width="2.4"/>')
    b.append(f'<line class="glint" x1="870" y1="{y + h / 2}" x2="906" y2="{y + 22}" stroke="{GOLD_HI}" stroke-width="2.4" stroke-linecap="round" pathLength="100"/>')
    outcomes = [("Yes", "sent once, exactly as shown", GOLD),
                ("No or no answer", "nothing sent, draft kept", SILVER_2),
                ("A rule fails", "nothing sent, reasons listed", SILVER_2),
                ("yolo mode or cron", "refused before it starts", SILVER_2)]
    for i, (k, v, col) in enumerate(outcomes):
        oy = y + 4 + i * 44
        b.append(f'<line x1="870" y1="{y + h / 2}" x2="906" y2="{oy + 18}" stroke="{GOLD if i == 0 else SILVER_3}" stroke-width="1.2"/>')
        b.append(f'<text x="916" y="{oy + 14}" font-family="{MONO}" font-size="13" fill="{col}">{k}</text>')
        b.append(f'<text x="916" y="{oy + 32}" font-family="{MONO}" font-size="12" fill="{SILVER_3}">{v}</text>')
    b.append(f'<line x1="40" y1="{H - 64}" x2="{W - 40}" y2="{H - 64}" stroke="{SILVER}" stroke-opacity="0.12"/>')
    b.append(f'<text x="40" y="{H - 34}" font-family="{MONO}" font-size="12" fill="{SILVER_3}">Scheduled messages are confirmed the same way when you schedule them, and every rule is checked again at send time.</text>')
    return frame_svg(W, H, "How a message leaves",
                     "Hermes drafts, the safety rules are checked, then Hermes's own approval prompt shows you the person, "
                     "the app and the exact words. Only a yes sends it, once.", "".join(b))


def architecture_svg() -> str:
    W, H = 1200, 600
    top, h = 96, 236
    b = [f'<text x="40" y="50" font-family="{MONO}" font-size="12" letter-spacing="2" fill="{DIM}">HOW IT WORKS · READING FLOWS RIGHT, A SEND COMES BACK ONLY THROUGH GOLD</text>']
    for i, (name, color) in enumerate(APPS):
        yy = top + 18 + i * 74
        b.append(f'<rect x="40" y="{yy}" width="150" height="52" fill="{INK}" stroke="{SILVER_3}" stroke-width="1.2"/>')
        b.append(logo(name, 52, yy + 15, 22))
        b.append(f'<text x="84" y="{yy + 31}" font-family="{MONO}" font-size="12" letter-spacing="1.4" fill="{SILVER_2}">{name}</text>')
        b.append(arrow(190, yy + 26, 222, yy + 26, SILVER_3, 1.1))
    cols = [(226, "UNIPILE API V2", ["your own key", "one call at a time", "2-5 s apart", "120 calls an hour", "webhooks, optional"], SILVER_3),
            (468, "LOCAL SQLITE", ["chats and messages", "full-text search", "memory, open items", "embeddings, optional", "on this machine"], SILVER_3),
            (710, "UNDERSTAND", ["triage, scams, plans", "radar (local rules)", "memory and drafts", "translation", "via Hermes's model"], NOUS_LIT),
            (952, "HERMES", ["22 tools and a skill", "the /inbox command", "the Inbox page", "cron blueprints", "voice in and out"], NOUS_LIT)]
    for i, (x, t, lines, st) in enumerate(cols):
        b.append(box(x, top, 208, h, t, lines, st, DIM, num=i + 1))
        if i < 3:
            b.append(arrow(x + 208, top + h / 2, x + 238, top + h / 2, NOUS_LIT if i >= 1 else SILVER_3))
    b.append(box(468, 372, 208, 74, "YOUR SUPABASE", ["optional, two-way sync"], SILVER_3, DIM, dashed=True))
    b.append(f'<line x1="572" y1="{top + h}" x2="572" y2="368" stroke="{SILVER_3}" stroke-width="1.1" stroke-dasharray="3 3"/>')
    gy = 486
    b.append(f'<rect x="710" y="{gy - 26}" width="450" height="64" fill="{INK}" stroke="{GOLD}" stroke-width="1.4"/>')
    b.append(f'<text x="726" y="{gy - 2}" font-family="{MONO}" font-size="12" letter-spacing="1.6" fill="{GOLD}">#5 YOUR YES</text>')
    b.append(f'<text x="726" y="{gy + 22}" font-family="{MONO}" font-size="13" fill="{SILVER_2}">approval prompt, exact words, limits re-checked</text>')
    b.append(arrow(1056, top + h, 1056, gy - 30, GOLD))
    b.append(f'<path d="M710 {gy + 6} H330 V{top + h + 30}" fill="none" stroke="{GOLD}" stroke-width="1.6"/>')
    b.append(f'<rect class="gate" x="710" y="{gy - 26}" width="450" height="64" fill="none" stroke="{GOLD_HI}" stroke-width="2.4"/>')
    b.append(f'<path class="glint" d="M1056 {top + h} V{gy - 26} M710 {gy + 6} H330 V{top + h + 4}" fill="none" stroke="{GOLD_HI}" stroke-width="2.6" stroke-linecap="round" pathLength="100"/>')
    for x in (434, 676, 918):
        b.append(f'<line class="current" x1="{x}" y1="{top + h / 2}" x2="{x + 26}" y2="{top + h / 2}" stroke="#FFFFFF" stroke-width="1.4"/>')
    b.append(arrow(330, top + h + 30, 330, top + h + 4, GOLD, 1.6))
    b.append(f'<text x="350" y="{gy - 4}" font-family="{MONO}" font-size="12" letter-spacing="1.4" fill="{GOLD}">SENT ONCE</text>')
    b.append(f'<line x1="40" y1="{H - 56}" x2="{W - 40}" y2="{H - 56}" stroke="{SILVER}" stroke-opacity="0.12"/>')
    b.append(f'<text x="40" y="{H - 28}" font-family="{MONO}" font-size="12" fill="{SILVER_3}">No telemetry. Text sent to a model or embedding provider has codes, passwords and card, bank and ID numbers removed first.</text>')
    return frame_svg(W, H, "How unified-inbox works",
                     "WhatsApp, Instagram and LinkedIn are read through your Unipile key at a careful pace into a local SQLite copy, "
                     "understood on your machine and with Hermes's model, and offered to Hermes as tools, a command and the Inbox page. "
                     "A message goes back out only through your approval.", "".join(b))


def badges_svg() -> str:
    """A strip of label/value badges in the Herald colours (kept local so the docs page shows them too)."""
    items = [("HERMES AGENT", "0.23+", NOUS_LIT), ("TOOLS", "22", NOUS_LIT), ("TESTS", "461, OFFLINE", NOUS_LIT),
             ("SENDS", "ONLY AFTER YOUR YES", GOLD), ("LICENSE", "MIT", SILVER_3)]
    cw, h, gap, x = 7.9, 28, 10, 0.5
    out = []
    for label, value, accent in items:
        lw, vw = len(label) * cw + 20, len(value) * cw + 20
        out.append(f'<rect x="{x}" y="0.5" width="{lw}" height="{h}" fill="{INDIGO}" stroke="{accent}" stroke-opacity="0.7"/>')
        out.append(f'<rect x="{x + lw}" y="0.5" width="{vw}" height="{h}" fill="{INK}" stroke="{accent}" stroke-opacity="0.7"/>')
        out.append(f'<text x="{x + 10}" y="19" font-family="{MONO}" font-size="11" letter-spacing="1.3" fill="{DIM}">{label}</text>')
        out.append(f'<text x="{x + lw + 10}" y="19" font-family="{MONO}" font-size="11" letter-spacing="1.3" '
                   f'fill="{GOLD if accent == GOLD else SILVER}">{value}</text>')
        x += lw + vw + gap
    w = math.ceil(x - gap + 1)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h + 1}" viewBox="0 0 {w} {h + 1}" role="img" '
            f'aria-label="Hermes Agent 0.23 or later; 22 tools; 461 tests, run offline; sends only after your yes; MIT license">'
            f'{"".join(out)}</svg>\n')


def gallery_png() -> None:
    """One picture of the Inbox page: four screens in a grid and the phone layout beside them."""
    from PIL import Image  # development only

    shots = [Image.open(OUT / "screens" / f"{n}.png").convert("RGB") for n in ("today", "chat", "radar", "setup-light")]
    phone = Image.open(OUT / "screens" / "phone.png").convert("RGB")
    cell_w, pad, gap = 1100, 56, 32
    cell_h = round(shots[0].height * cell_w / shots[0].width)
    grid_h = 2 * cell_h + gap
    ph_w = round(phone.width * grid_h / phone.height)
    canvas = Image.new("RGB", (pad * 2 + cell_w * 2 + gap * 2 + ph_w, pad * 2 + grid_h), INK)
    for i, im in enumerate(shots):
        canvas.paste(im.resize((cell_w, cell_h), Image.LANCZOS), (pad + (i % 2) * (cell_w + gap), pad + (i // 2) * (cell_h + gap)))
    canvas.paste(phone.resize((ph_w, grid_h), Image.LANCZOS), (pad + 2 * (cell_w + gap), pad))
    canvas.thumbnail((2400, 2400), Image.LANCZOS)
    canvas.save(OUT / "screens" / "gallery.png", optimize=True)


def browser() -> str | None:
    for p in (r"C:\Program Files\Google\Chrome\Application\chrome.exe",
              r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
              shutil.which("chromium"), shutil.which("google-chrome"), shutil.which("chrome")):
        if p and Path(p).exists():
            return p
    return None


STILL_CSS = ("<style>*{animation:none!important}.lit,.gate{opacity:1!important;stroke-opacity:1!important}"
             ".gold-draw{stroke-dashoffset:0!important;opacity:1!important}"
             ".tail,.head,.ring,.slice,.glint{opacity:0!important}</style>")


def render_png(svg_path: Path, png_path: Path, w: int, h: int, at_ms: int = 0) -> None:
    """Screenshot a still of the SVG: motion off, the gold "your yes" lit (the end of the story)."""
    still = svg_path.with_name(svg_path.stem + ".still.svg")
    text = svg_path.read_text(encoding="utf-8")
    still.write_text(text.replace("</desc>", "</desc>\n" + STILL_CSS, 1), encoding="utf-8")
    try:
        _shoot(still, png_path, w, h)
    finally:
        still.unlink()


def _shoot(svg_path: Path, png_path: Path, w: int, h: int, at_ms: int = 0) -> None:
    exe = browser()
    if not exe:
        sys.exit("No Chrome or Edge found for --png")
    subprocess.run([exe, "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=2",
                    f"--window-size={w},{h}", f"--screenshot={png_path}", svg_path.resolve().as_uri()],
                   check=True, capture_output=True, timeout=60)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--png", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "hero.svg").write_text(hero(1200, 480), encoding="utf-8")
    (OUT / "catalog-card.svg").write_text(hero(1200, 600), encoding="utf-8")
    (OUT / "approval.svg").write_text(approval_svg(), encoding="utf-8")
    (OUT / "architecture.svg").write_text(architecture_svg(), encoding="utf-8")
    (OUT / "badges.svg").write_text(badges_svg(), encoding="utf-8")
    if (OUT / "screens" / "today.png").exists():
        gallery_png()
    if args.png:
        render_png(OUT / "approval.svg", OUT / "approval.png", 1200, 400, 5600)
        render_png(OUT / "architecture.svg", OUT / "architecture.png", 1200, 600, 5600)
        render_png(OUT / "hero.svg", OUT / "hero.png", 1200, 480, 5200)
        render_png(OUT / "catalog-card.svg", OUT / "catalog-card.png", 1200, 600, 5200)
    print("wrote", ", ".join(sorted(p.name for p in OUT.iterdir())))


if __name__ == "__main__":
    main()
