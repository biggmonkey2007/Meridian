"""Generate meridian.ico — a simple globe with meridian/parallel lines, matching the app's ink-navy look.
Run once (or after tweaking) to (re)build the icon: python make_icon.py"""
import math
from PIL import Image, ImageDraw

NAVY = (26, 34, 48)      # #1a2230 — the app's headline ink
BLUE = (58, 92, 138)     # meridian lines
BG = (0, 0, 0, 0)        # transparent


def render(px):
    # supersample for smooth curves, then downscale
    S = 4
    n = px * S
    im = Image.new("RGBA", (n, n), BG)
    d = ImageDraw.Draw(im)
    cx = cy = n / 2
    r = n * 0.44
    lw = max(1, int(n * 0.028))
    # globe disc
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=NAVY)
    # parallels (latitude lines) — horizontal chords
    for f in (-0.6, -0.3, 0.0, 0.3, 0.6):
        y = cy + r * f
        half = math.sqrt(max(0.0, r * r - (r * f) ** 2))
        d.line([cx - half, y, cx + half, y], fill=BLUE, width=lw)
    # meridians (longitude lines) — ellipse arcs of varying width
    for f in (0.4, 0.75):
        rx = r * f
        d.ellipse([cx - rx, cy - r, cx + rx, cy + r], outline=BLUE, width=lw)
    # central meridian
    d.line([cx, cy - r, cx, cy + r], fill=BLUE, width=lw)
    # crisp rim
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=BLUE, width=int(lw * 1.3))
    return im.resize((px, px), Image.LANCZOS)


big = render(256)
# save the 256px master and let PIL embed every requested size into a single multi-res .ico
big.save("meridian.ico", format="ICO",
         sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
big.save("meridian.png")
print("wrote meridian.ico and meridian.png")
