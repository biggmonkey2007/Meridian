"""Build the Capacitor web root (www/index.html) from the ONE shared UI file (../meridian-relief.html),
injecting the hosted-feed URL so the app runs in web-fetch mode (aiBridge() -> fetch the feed, no desktop
bridge). The UI is self-contained (inline CSS/JS), so index.html is all Capacitor needs.

    MERIDIAN_FEED_BASE=https://feed.example.com python build_www.py
    #  ...or put the URL in mobile/feed_base.txt

Run via `npm run build` (package.json). Re-run whenever the UI changes, then `npx cap sync`.
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "meridian-relief.html")
WWW = os.path.join(HERE, "www")

feed = (os.environ.get("MERIDIAN_FEED_BASE") or "").strip().rstrip("/")
if not feed:
    p = os.path.join(HERE, "feed_base.txt")
    if os.path.exists(p):
        feed = open(p, encoding="utf-8").read().strip().rstrip("/")
if not feed:
    sys.exit("ERROR: set MERIDIAN_FEED_BASE=https://feed.example.com (or create mobile/feed_base.txt). "
             "This is the URL of your deployed feed server — see ../DEPLOY.md.")

html = open(SRC, encoding="utf-8").read()
meta = '<meta name="meridian-feed-base" content="%s">' % feed
if 'name="meridian-feed-base"' in html:                       # idempotent: replace an existing tag
    html = re.sub(r'<meta name="meridian-feed-base"[^>]*>', meta, html, count=1)
else:
    html = re.sub(r"(<head[^>]*>)", r"\1\n" + meta, html, count=1)

os.makedirs(WWW, exist_ok=True)
with open(os.path.join(WWW, "index.html"), "w", encoding="utf-8") as f:
    f.write(html)
print("wrote %s  (feed: %s)" % (os.path.join("www", "index.html"), feed))
