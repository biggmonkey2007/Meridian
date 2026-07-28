# Meridian FEED SERVER — one self-contained container that (1) rebuilds the geolocated world feed every few
# minutes and (2) serves it with CORS + cache headers. Pure Python stdlib, so the image is tiny and needs no
# pip install. Put Cloudflare (or any CDN/reverse proxy) in front for HTTPS + global caching.
#
#   docker build -t meridian-feed .
#   docker run -d --name meridian-feed -p 80:8080 \
#       -e SUMMARY_API_KEY=gsk_your_groq_key \
#       -v meridian_data:/data meridian-feed
#
# The feed is then at  http://<host>/world_24h.json  and share cards at  http://<host>/s/<sid>.html
FROM python:3.12-slim

WORKDIR /app
COPY . /app

# Feed + caches live on a volume so they persist across restarts (and the summary cache isn't rebuilt).
ENV FEED_OUT=/data/feed_out \
    FEED_PORT=8080 \
    MERIDIAN_DATA=/data \
    PYTHONUNBUFFERED=1
VOLUME /data
EXPOSE 8080

# Rebuild loop in the background; static server in the foreground (PID 1).
CMD ["/bin/sh", "-c", "python build_feed.py --loop 180 & exec python serve_feed.py"]
