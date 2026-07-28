# Scaling Meridian to millions — and to iOS / Android

## The problem with today's design
Today every installed copy is a **fat client**: it independently calls GDELT, scrapes RSS + news
articles, pulls Telegram, and queries Wikidata — then geolocates and dedupes it all *locally*. That's
fine for one user, but it does **not** scale:

- **Rate limits.** GDELT, Wikidata and Telegram throttle by IP/volume. At a million users those services
  see a flood and start returning `429` — you've already seen the Wikidata 429s on a single machine.
- **Massive redundancy.** A million machines computing the *identical* world feed every few minutes is
  a million times more work than needed.
- **No mobile.** The Python/pywebview backend can't run on iOS or Android.

No amount of client-side preloading fixes this — it's structural.

## The architecture that scales: thin client + hosted feed
Do the heavy work **once, on a server**, and let every client just **fetch the result**:

```
   GDELT / RSS / Telegram / Wikidata
                 │
        ┌────────▼─────────┐        rebuilds every ~3 min
        │  FEED SERVER      │        (build_feed.py, one small VPS)
        │  _build_world_*   │
        └────────┬─────────┘
                 │ writes world_{6,12,24,48}h.json
        ┌────────▼─────────┐
        │   CDN / edge     │  ← absorbs ~all traffic; origin builds, edge serves
        └────────┬─────────┘
      ┌──────────┼───────────┬─────────────┐
   Desktop     iOS app     Android      Web  ← each does ONE small cached GET
```

One origin build serves **everyone**. Ten million users hitting a CDN for a ~100 KB JSON is trivial and
cheap; the origin still only builds a handful of times per hour.

## What's built now (in this repo)
- **Client thin-mode** (`app.py`): if `MERIDIAN_FEED_BASE` (env) / `feed_base.txt` (drop-in) /
  `FEED_BASE_DEFAULT` is set, `world_events()` fetches `<base>/world_<h>h.json` — one CDN-cached GET, 60s
  client cache — instead of building locally. If the server is unreachable it **falls back to the local
  build**, so the desktop app always works, even offline.
- **Feed server** (`build_feed.py`): rebuilds the feed for every window and writes atomic JSON to
  `FEED_OUT`. Run `python build_feed.py --loop 180`.

### Deploy it (≈ 20 min, ~$10/mo)
1. On a small VPS: `pip install -r requirements.txt`, then run `build_feed.py --loop 180` under
   systemd / pm2 / a Docker restart policy.
2. Serve `feed_out/` behind a CDN (Cloudflare in front of Caddy/nginx) with
   `Access-Control-Allow-Origin: *` and `Cache-Control: public, max-age=60`.
3. Point clients at it: set `FEED_BASE_DEFAULT = "https://feed.yourdomain.com"` in `app.py` and cut a
   release — or, for already-installed copies without a rebuild, drop the URL into
   `%LOCALAPPDATA%\Meridian\feed_base.txt`.

## Next phase — the per-click enrichments (finish the scale story)
The feed is the big, constant load; it's done. The per-*click* calls (`article_detail`, `event_media`,
`event_people`, `story_photo`, `perspectives`, `country_leaders`, `country_news`) still hit external
sites from the client. At millions of users those need the same treatment: a thin API on the feed server
(`GET <base>/api/article_detail?url=…`) that runs the existing `Api` method, **caches the result
server-side**, and returns JSON — so a popular story is scraped once for everybody, not once per user.
This is a small Flask/FastAPI wrapper around the methods that already exist; wire the client's
`aiBridge()` calls to fall back to `fetch(<base>/api/…)` when there's no desktop bridge.

## iOS / Android
The UI (`meridian-relief.html`) is already a self-contained web app — that's the key. Once the hosted
feed + enrichment API above exist:
1. Add a **web mode** to the UI: when there's no pywebview bridge, call the hosted API via `fetch()`
   (the UI already detects "no bridge"; point that path at `<base>/…`).
2. Wrap the same HTML/JS with **Capacitor** → one codebase compiles to a real iOS *and* Android app,
   pointing at the same feed. Ship UI changes **over-the-air** so most updates skip App-Store review.

Result: desktop, iOS, Android and web are all thin clients of one backend — the same data, updated in one
place, scaling on the CDN.

## Speed wins already in place (client side)
Parallel source fetch under one deadline · memoised geolocation · gazetteer scan pre-filter · hover +
idle **preloading** of a story's article/clips/photos/people so a click opens with no wait · blank-until-
fresh cold open. With the hosted feed on top, the map itself loads in one cached request — effectively no
load time.
