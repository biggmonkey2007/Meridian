"""Meridian FEED SERVER — build the world map ONCE for everyone.

Run this on one small always-on box (a $5-10/mo VPS is plenty). It rebuilds the geolocated world feed
every few minutes and writes it to a directory that any web server / CDN serves as static JSON. Every
Meridian client (desktop, iOS, Android, web) then fetches ONE small file instead of doing the work
itself — which is exactly what lets a single backend hold MILLIONS of users without every copy
independently hammering GDELT / Wikidata / Telegram (and getting rate-limited).

    python build_feed.py               # build once
    python build_feed.py --loop 180    # rebuild every 180s, forever (run under systemd/pm2/docker)

Then point clients at it by setting MERIDIAN_FEED_BASE (or dropping feed_base.txt, or baking
FEED_BASE_DEFAULT in app.py) to the URL that serves FEED_OUT — e.g. https://feed.example.com/  so a
client GETs https://feed.example.com/world_24h.json.

Serve FEED_OUT behind a CDN with:  Access-Control-Allow-Origin: *   and   Cache-Control: public, max-age=60
(60s lines up with the client cache; the CDN then absorbs ~all of the load — origin builds, edge serves).
"""
import os
import sys
import json
import time
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

OUT = os.environ.get("FEED_OUT", "feed_out")
WINDOWS = (6, 12, 24, 48)


def _write_atomic(path, data):
    """Write via a temp file + rename so a client (or CDN) never reads a half-written JSON."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def build_once():
    os.makedirs(OUT, exist_ok=True)
    api = app.Api()
    for h in WINDOWS:
        t = time.time()
        try:
            data = api._build_world_events(h)            # the SAME builder the desktop app uses
            data = dict(data)
            data["generated"] = int(time.time())
            _write_atomic(os.path.join(OUT, "world_%dh.json" % h), data)
            print("  world_%dh.json  %d events  %.1fs" % (h, len(data.get("events", [])), time.time() - t),
                  flush=True)
        except Exception as e:
            print("  world_%dh.json FAILED: %s" % (h, e), flush=True)


def main():
    loop = 0
    if "--loop" in sys.argv:
        try:
            loop = int(sys.argv[sys.argv.index("--loop") + 1])
        except Exception:
            loop = 180
    while True:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), "building feed ->", OUT, flush=True)
        build_once()
        if not loop:
            break
        time.sleep(max(30, loop))


if __name__ == "__main__":
    main()
