"""Meridian FEED SERVER — build the world map (and the branded share pages) ONCE for everyone.

Run this on one small always-on box (a $5-10/mo VPS is plenty). It rebuilds the geolocated world feed
every few minutes and writes it to a directory that any web server / CDN serves as static JSON. Every
Meridian client (desktop, iOS, Android, web) then fetches ONE small file instead of doing the work
itself — which is exactly what lets a single backend hold MILLIONS of users without every copy
independently hammering GDELT / Wikidata / Telegram.

It ALSO writes a branded Open-Graph share page per story to FEED_OUT/s/<sid>.html — so when a user shares
a story, the pasted link renders a rich MERIDIAN card (photo + headline + "via Meridian"), which is the
app's main organic-growth loop.

    python build_feed.py               # build once
    python build_feed.py --loop 180    # rebuild every 180s, forever (run under systemd/pm2/docker)

Set MERIDIAN_FEED_BASE to this server's public URL (e.g. https://feed.example.com) so the share pages'
og:url / "Get Meridian" links are absolute. Serve FEED_OUT behind a CDN with
Access-Control-Allow-Origin: *  and  Cache-Control: public, max-age=60.
"""
import os
import sys
import json
import time
import tempfile
import concurrent.futures
import html as _html

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app

OUT = os.environ.get("FEED_OUT", "feed_out")
BASE = (os.environ.get("MERIDIAN_FEED_BASE") or app._feed_base() or "").rstrip("/")
WINDOWS = (6, 12, 24, 48)


def _write_atomic(path, text):
    """Write text via a temp file + rename so a client/CDN never reads a half-written file."""
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


_CSS = ("*{box-sizing:border-box}body{margin:0;font:16px/1.5 -apple-system,BlinkMacSystemFont,'Segoe UI',"
        "Roboto,sans-serif;background:#0b1220;color:#e8edf6;display:flex;justify-content:center;padding:24px}"
        ".card{max-width:560px;width:100%;background:#121a2b;border:1px solid #1e2942;border-radius:16px;"
        "overflow:hidden}.brand{padding:14px 20px;font-weight:800;letter-spacing:.14em;font-size:13px;"
        "color:#9fb3d0;border-bottom:1px solid #1e2942}.hero{width:100%;aspect-ratio:1200/630;"
        "object-fit:cover;display:block;background:#1a2540}.body{padding:20px}h1{font-family:Georgia,serif;"
        "font-size:24px;line-height:1.22;margin:0 0 10px}.meta{color:#8fa3c0;font-size:13px;margin:0 0 14px}"
        ".sum{color:#cdd8ea;margin:0 0 20px}a.cta{display:inline-block;background:#2b6cff;color:#fff;"
        "text-decoration:none;padding:11px 16px;border-radius:10px;font-weight:600}a.get{display:block;"
        "margin-top:16px;color:#9fb3d0;text-decoration:none;font-size:14px}")


def _story_summary(api, ev):
    """Our copyright-free summary for one story, via the SAME code path a click uses (so the 30-day cache is
    shared): a real article URL scrapes+summarizes the article; a substantial pure-Telegram post summarizes
    its own text. Returns "" when there's nothing summarize-worthy or no summarizer is configured."""
    u = (ev.get("url") or "").strip()
    if u.startswith("http") and "t.me/" not in u:
        return (api.summarize_event(ev.get("title") or "", u) or {}).get("summary") or ""
    if ev.get("tg") and len((ev.get("sum") or "")) >= 180:
        return app._summarize(ev.get("title") or "", ev.get("sum") or "")
    return ""


def enrich_summaries(api, events):
    """Pre-generate Meridian's OWN copyright-free summary for every story, ONCE, here on the server — so
    every user who opens that story gets the SAME summary with zero client work (it ships baked into the
    feed JSON as ev["summary"]). Skips entirely when no summarizer is configured. Both the article scrape
    and the summary are cached, so only the first build pays the cost; later builds reuse the cache."""
    if not (app._summary_cfg()[0] or app._local_llm()):
        return 0

    def work(ev):
        try:
            s = _story_summary(api, ev)
            if s:
                ev["summary"] = s
                return 1
        except Exception:
            pass
        return 0

    n = 0
    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        for r in ex.map(work, events):
            n += r
    return n


def _share_page(ev):
    e = lambda s: _html.escape(str(s or ""), quote=True)
    title = e(ev.get("title"))
    desc = e(ev.get("summary") or ev.get("sum"))   # our own copyright-free summary on the share card when we have it
    img = e(ev.get("image") or "")
    art = e(ev.get("url") or "#")
    meta = e(" · ".join(x for x in [ev.get("place") or ev.get("country") or "", ev.get("source") or ""] if x))
    surl = e(BASE + "/s/" + (ev.get("sid") or "")) if BASE else art
    get = e(BASE or "#")
    og_img = ('<meta property="og:image" content="%s"><meta name="twitter:image" content="%s">' % (img, img)) if img else ""
    hero = ('<img class="hero" src="%s" alt="">' % img) if img else ""
    card = "summary_large_image" if img else "summary"
    return (
        "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
        "<title>" + title + " · Meridian</title>"
        "<meta property=\"og:type\" content=\"article\">"
        "<meta property=\"og:site_name\" content=\"Meridian\">"
        "<meta property=\"og:title\" content=\"" + title + "\">"
        "<meta property=\"og:description\" content=\"" + desc + "\">"
        "<meta property=\"og:url\" content=\"" + surl + "\">"
        "<meta name=\"twitter:card\" content=\"" + card + "\">"
        "<meta name=\"twitter:title\" content=\"" + title + "\">"
        "<meta name=\"twitter:description\" content=\"" + desc + "\">"
        + og_img +
        "<style>" + _CSS + "</style></head><body><main class=\"card\">"
        "<div class=\"brand\">◐ MERIDIAN</div>" + hero +
        "<div class=\"body\"><h1>" + title + "</h1><p class=\"meta\">" + meta + "</p>"
        "<p class=\"sum\">" + desc + "</p>"
        "<a class=\"cta\" href=\"" + art + "\" rel=\"noopener\">Read the original →</a>"
        "<a class=\"get\" href=\"" + get + "\">The world’s news, mapped — get Meridian ↗</a>"
        "</div></main></body></html>")


def write_share_pages(data):
    sd = os.path.join(OUT, "s")
    os.makedirs(sd, exist_ok=True)
    n = 0
    for ev in data.get("events", []):
        sid = ev.get("sid")
        if not sid:
            continue
        try:
            _write_atomic(os.path.join(sd, sid + ".html"), _share_page(ev))
            n += 1
        except Exception:
            pass
    return n


def build_once():
    os.makedirs(OUT, exist_ok=True)
    api = app.Api()
    pages = 0
    for h in WINDOWS:
        t = time.time()
        try:
            data = dict(api._build_world_events(h))
            data["generated"] = int(time.time())
            ns = enrich_summaries(api, data.get("events", []))   # bake OUR summary into every story, once, for all users
            _write_atomic(os.path.join(OUT, "world_%dh.json" % h),
                          json.dumps(data, ensure_ascii=False, separators=(",", ":")))
            if h == 24:                              # share pages from the widest, most-shared window
                pages = write_share_pages(data)
            print("  world_%dh.json  %d events  %d summaries  %.1fs" % (
                h, len(data.get("events", [])), ns, time.time() - t), flush=True)
        except Exception as ex:
            print("  world_%dh.json FAILED: %s" % (h, ex), flush=True)
    print("  wrote %d share pages -> %s/s/" % (pages, OUT), flush=True)
    # LIVE TV — resolve each channel's current live video id once here (server-side; a browser can't, CORS
    # blocks youtube.com) and hand it to the website as static JSON. The loop refresh keeps the ids fresh, so
    # the web UI's webApi.live_tv() just fetches this file. (Desktop uses the pywebview Api.live_tv directly.)
    try:
        tv = api.live_tv()
        _write_atomic(os.path.join(OUT, "live_tv.json"),
                      json.dumps({"channels": tv, "generated": int(time.time())}, ensure_ascii=False, separators=(",", ":")))
        print("  live_tv.json  %d/%d live" % (sum(1 for c in tv if c.get("live")), len(tv)), flush=True)
    except Exception as ex:
        print("  live_tv.json FAILED: %s" % ex, flush=True)


def main():
    loop = 0
    if "--loop" in sys.argv:
        try:
            loop = int(sys.argv[sys.argv.index("--loop") + 1])
        except Exception:
            loop = 180
    while True:
        print(time.strftime("%Y-%m-%d %H:%M:%S"), "building feed ->", OUT, "(base:", BASE or "unset", ")", flush=True)
        build_once()
        if not loop:
            break
        time.sleep(max(30, loop))


if __name__ == "__main__":
    main()
