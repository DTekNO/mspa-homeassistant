"""Generate docs/prediction-models.svg — the bucket chords against Newton's curve.

Deliberately exaggerated. With this spa's real parameters (tau ~38 h, asymptote ~70 C)
a heat-up from 28 to 40 is very nearly a straight line: the rate falls only about 14%
across the whole run, and a figure drawn to those numbers shows two lines on top of one
another and teaches nothing. The asymptote here is pulled down to 45 C and tau to 10 h,
which makes the rate fall four-fold across the run and the geometry legible. The
*relationship* drawn is exact — each chord meets the curve at both its band edges,
because that is what a bucket rate is.

Run: python analysis/prediction_figure.py
"""
import math
from pathlib import Path

ASYMPTOTE, TAU = 45.0, 10.0          # exaggerated, see above
EDGES = [20.0, 30.0, 37.0, 39.0]
T_MIN, T_MAX = 18.0, 40.5

def hours(w0, w1):
    return TAU * math.log((ASYMPTOTE - w0) / (ASYMPTOTE - w1))

TOTAL = hours(EDGES[0], EDGES[-1])
W, H = 760, 430
L, R, TOP, BOT = 66, 22, 24, 54
PW, PH = W - L - R, H - TOP - BOT
X_MAX = TOTAL * 1.04

def px(h): return L + PW * h / X_MAX
def py(t): return TOP + PH * (T_MAX - t) / (T_MAX - T_MIN)

AXIS, CURVE, CHORD, NODE = "#8b949e", "#3b82c4", "#d98032", "#d98032"
o = []
add = o.append
add(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" '
    f'height="{H}" font-family="system-ui,-apple-system,Segoe UI,sans-serif" '
    f'role="img" aria-label="Bucket chords against the Newton heating curve">')
add('<title>Three learned rate buckets drawn as chords of the Newton heating curve'
    '</title>')

# band shading + edge rules
for i, (lo, hi) in enumerate(zip(EDGES, EDGES[1:])):
    if i % 2 == 0:
        add(f'<rect x="{px(hours(EDGES[0], lo)):.1f}" y="{py(T_MAX):.1f}" '
            f'width="{px(hours(EDGES[0], hi)) - px(hours(EDGES[0], lo)):.1f}" '
            f'height="{PH:.1f}" fill="{AXIS}" opacity="0.07"/>')
for e in EDGES:
    x = px(hours(EDGES[0], e))
    add(f'<line x1="{x:.1f}" y1="{py(e):.1f}" x2="{x:.1f}" y2="{py(T_MIN):.1f}" '
        f'stroke="{AXIS}" stroke-width="1" stroke-dasharray="2 3" opacity="0.55"/>')
    add(f'<line x1="{L}" y1="{py(e):.1f}" x2="{x:.1f}" y2="{py(e):.1f}" '
        f'stroke="{AXIS}" stroke-width="1" stroke-dasharray="2 3" opacity="0.55"/>')

# axes
add(f'<line x1="{L}" y1="{py(T_MIN):.1f}" x2="{L + PW}" y2="{py(T_MIN):.1f}" '
    f'stroke="{AXIS}" stroke-width="1.4"/>')
add(f'<line x1="{L}" y1="{TOP}" x2="{L}" y2="{py(T_MIN):.1f}" '
    f'stroke="{AXIS}" stroke-width="1.4"/>')
for t in (20, 25, 30, 35, 40):
    add(f'<text x="{L - 10}" y="{py(t) + 4:.1f}" fill="{AXIS}" font-size="12" '
        f'text-anchor="end">{t}</text>')
for h in range(0, int(X_MAX) + 1, 2):
    add(f'<text x="{px(h):.1f}" y="{py(T_MIN) + 18:.1f}" fill="{AXIS}" font-size="12" '
        f'text-anchor="middle">{h}</text>')
add(f'<text x="{L + PW / 2:.1f}" y="{H - 14}" fill="{AXIS}" font-size="13" '
    f'text-anchor="middle">hours of heating</text>')
add(f'<text x="16" y="{TOP + PH / 2:.1f}" fill="{AXIS}" font-size="13" '
    f'text-anchor="middle" transform="rotate(-90 16 {TOP + PH / 2:.1f})">'
    f'water temperature (°C)</text>')

# Newton curve
pts = []
steps = 240
for i in range(steps + 1):
    h = TOTAL * i / steps
    pts.append(f"{px(h):.2f},{py(ASYMPTOTE - (ASYMPTOTE - EDGES[0]) * math.exp(-h / TAU)):.2f}")
add(f'<polyline points="{" ".join(pts)}" fill="none" stroke="{CURVE}" '
    f'stroke-width="2.6" stroke-linecap="round"/>')

# chords: straight lines at the constant rate each bucket learned
for lo, hi in zip(EDGES, EDGES[1:]):
    x1, x2 = px(hours(EDGES[0], lo)), px(hours(EDGES[0], hi))
    add(f'<line x1="{x1:.1f}" y1="{py(lo):.1f}" x2="{x2:.1f}" y2="{py(hi):.1f}" '
        f'stroke="{CHORD}" stroke-width="2.4" stroke-dasharray="7 4"/>')
    rate = (hi - lo) / (hours(lo, hi))
    add(f'<text x="{(x1 + x2) / 2:.1f}" y="{(py(lo) + py(hi)) / 2 - 9:.1f}" '
        f'fill="{CHORD}" font-size="12" text-anchor="middle">{rate:.2f} °C/h</text>')
for e in EDGES:
    add(f'<circle cx="{px(hours(EDGES[0], e)):.1f}" cy="{py(e):.1f}" r="4.4" '
        f'fill="{NODE}" stroke="none"/>')

# legend
lx, ly = L + 22, TOP + 16
add(f'<line x1="{lx}" y1="{ly}" x2="{lx + 30}" y2="{ly}" stroke="{CURVE}" '
    f'stroke-width="2.6"/>')
add(f'<text x="{lx + 38}" y="{ly + 4}" fill="{AXIS}" font-size="13">'
    f'Newton — one curve, two parameters</text>')
add(f'<line x1="{lx}" y1="{ly + 22}" x2="{lx + 30}" y2="{ly + 22}" stroke="{CHORD}" '
    f'stroke-width="2.4" stroke-dasharray="7 4"/>')
add(f'<text x="{lx + 38}" y="{ly + 26}" fill="{AXIS}" font-size="13">'
    f'buckets — three constant rates, meeting the curve at each edge</text>')
add('</svg>')

out = Path(__file__).resolve().parent.parent / "docs" / "prediction-models.svg"
out.write_text("\n".join(o) + "\n")
print(f"{out}  ({out.stat().st_size} bytes)")
for lo, hi in zip(EDGES, EDGES[1:]):
    print(f"  band {lo:>4}-{hi:<4} chord {(hi - lo) / hours(lo, hi):.3f} °C/h  "
          f"over {hours(lo, hi):.2f} h")
