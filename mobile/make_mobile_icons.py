"""Generate the app-store icon + splash from Meridian's globe mark:

    mobile/resources/icon.png    1024x1024, OPAQUE  (iOS/Android app icon — iOS forbids alpha)
    mobile/resources/splash.png  2732x2732, OPAQUE  (launch screen — logo centered on the brand canvas)

Then `npx @capacitor/assets generate` (see package.json `assets` script) fans these two out to every
icon/splash size iOS and Android need. Re-run only if the mark or brand colors change.

    python make_mobile_icons.py
"""
import os
import math
from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
RES = os.path.join(HERE, "resources")

BG = (13, 18, 25)        # #0d1219 — the app's dark canvas (icons must be opaque, so this is the fill)
DISC = (26, 34, 48)      # #1a2230 — globe body
LINE = (91, 149, 224)    # #5b95e0 — meridian/parallel lines, the app's highlight blue (reads on the dark bg)


def globe(size, mark=0.82):
    """A size x size opaque image: the globe mark centered on the brand background. `mark` is the fraction
    of the canvas the globe spans (smaller for the splash, so it has breathing room)."""
    S = 4
    n = size * S
    im = Image.new("RGB", (n, n), BG)
    d = ImageDraw.Draw(im)
    cx = cy = n / 2
    r = n * 0.5 * mark * 0.9
    lw = max(1, int(n * 0.014 * (0.82 / mark) ** 0.5))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=DISC)
    for f in (-0.6, -0.3, 0.0, 0.3, 0.6):                 # parallels
        y = cy + r * f
        half = math.sqrt(max(0.0, r * r - (r * f) ** 2))
        d.line([cx - half, y, cx + half, y], fill=LINE, width=lw)
    for f in (0.4, 0.75):                                 # meridians
        rx = r * f
        d.ellipse([cx - rx, cy - r, cx + rx, cy + r], outline=LINE, width=lw)
    d.line([cx, cy - r, cx, cy + r], fill=LINE, width=lw)  # central meridian
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=LINE, width=int(lw * 1.3))   # crisp rim
    return im.resize((size, size), Image.LANCZOS)


if __name__ == "__main__":
    os.makedirs(RES, exist_ok=True)
    globe(1024, mark=0.82).save(os.path.join(RES, "icon.png"))
    globe(2732, mark=0.42).save(os.path.join(RES, "splash.png"))
    print("wrote resources/icon.png (1024, opaque) and resources/splash.png (2732, opaque)")
