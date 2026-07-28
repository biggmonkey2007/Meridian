# Deploying the Meridian feed server

One server builds the world feed (geolocation, dedup, Telegram, **and the Groq summaries**) **once**, and
every client — desktop, iOS, Android, web — fetches the finished JSON. That's what makes a story summarized
once and viewable by all, and what lets one backend hold millions of users. See `SCALING.md` for the why.

The server is **pure Python stdlib** (no pip install, no GUI) plus your Groq key. Two moving parts:
`build_feed.py` (rebuild loop) writes JSON to `feed_out/`, and something serves that directory with CORS.

---

## Option A — Docker, one container (simplest)

```bash
docker build -t meridian-feed .
docker run -d --name meridian-feed -p 80:8080 \
    -e SUMMARY_API_KEY=gsk_your_groq_key \
    -v meridian_data:/data \
    meridian-feed
```

That's it. The container runs the rebuild loop **and** the static server. Feed is now at:

- `http://<host>/world_24h.json`  (also `world_6h`, `world_12h`, `world_48h`)
- `http://<host>/s/<sid>.html`    (branded share cards)

Put **Cloudflare** (free) in front of `<host>` for HTTPS + a global CDN — done. The `-v meridian_data:/data`
volume persists the caches (including the 30-day summary cache) across restarts, so you never re-pay for a
summary you already generated.

## Option B — Docker Compose with automatic HTTPS (production)

For HTTPS terminated at your own box (Caddy) instead of Cloudflare:

```bash
# .env  (next to docker-compose.yml)
echo "FEED_DOMAIN=feed.example.com"        >  .env
echo "SUMMARY_API_KEY=gsk_your_groq_key"   >> .env

# point a DNS A record for feed.example.com at this box, then:
docker compose up -d
```

Caddy fetches a certificate automatically and serves `https://feed.example.com/world_24h.json` with CORS +
cache headers. A CDN can still sit in front.

## Option C — No Docker (any box with Python 3)

```bash
# terminal 1 — the builder
SUMMARY_API_KEY=gsk_your_groq_key MERIDIAN_DATA=./srvdata python build_feed.py --loop 180

# terminal 2 — the server
FEED_OUT=./feed_out FEED_PORT=8080 python serve_feed.py
```

Front it with a Cloudflare Tunnel (`cloudflared tunnel --url http://localhost:8080`) for instant HTTPS with
no open ports. Use `systemd`/`pm2` to keep both running.

---

## Point the clients at it

Once the feed is live, tell clients its base URL. Any ONE of:

- **Rebuild the app** with `FEED_BASE_DEFAULT = "https://feed.example.com"` in `app.py` (all new installs use it).
- **Existing desktop installs:** drop the URL into `%LOCALAPPDATA%\Meridian\feed_base.txt`.
- **Env:** `MERIDIAN_FEED_BASE=https://feed.example.com`.

The client does one small cached GET of `world_<h>.json` (60 s client cache) and **falls back to a local
build if the server is ever unreachable**, so it always works. The pre-baked `summary` on each event renders
with zero extra calls — including on web/mobile builds that have no desktop bridge.

## Cost & scale
A $5–10/mo VPS is plenty: the origin builds a handful of times per hour; the CDN absorbs all user traffic
(a ~100 KB JSON). Groq summaries are generated once per article and cached 30 days, so request volume tracks
*new articles*, not users — the free tier covers it regardless of audience size.
