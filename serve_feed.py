"""Meridian FEED STATIC SERVER — serve the pre-built feed_out/ directory to every client (desktop, iOS,
Android, web) with the CORS + cache headers they need. Pure Python stdlib, zero dependencies.

    python serve_feed.py                 # serve ./feed_out on :8080
    FEED_OUT=/data/feed_out FEED_PORT=80 python serve_feed.py

This is the simple path (great behind a Cloudflare Tunnel or any reverse proxy, which add HTTPS + a CDN).
For a fuller production setup with auto-HTTPS at the edge, use docker-compose.yml (Caddy) instead — but the
headers here are already correct: Access-Control-Allow-Origin:* so any web/mobile origin can fetch, and
Cache-Control so a CDN and the 60s client cache both do their job.

Run it ALONGSIDE the builder:  python build_feed.py --loop 180   (writes the JSON this server hands out).
"""
import os
import sys
import http.server
import socketserver

OUT = os.environ.get("FEED_OUT", "feed_out")
PORT = int(os.environ.get("FEED_PORT", "8080"))


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=OUT, **k)

    def end_headers(self):
        # every client shares one feed on any origin -> permissive CORS, and cache so a CDN absorbs the load.
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, HEAD, OPTIONS")
        path = self.path.split("?", 1)[0]
        if path.endswith(".json"):
            self.send_header("Cache-Control", "public, max-age=60")      # the feed: fresh within a minute
        elif path.endswith(".html"):
            self.send_header("Cache-Control", "public, max-age=300")     # share cards: rich preview, changes rarely
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def log_message(self, fmt, *args):
        pass                                                             # quiet; the builder does the interesting logging


def main():
    if not os.path.isdir(OUT):
        os.makedirs(OUT, exist_ok=True)
    socketserver.ThreadingTCPServer.allow_reuse_address = True
    with socketserver.ThreadingTCPServer(("0.0.0.0", PORT), Handler) as httpd:
        print("serving %s on http://0.0.0.0:%d  (CORS: *)" % (os.path.abspath(OUT), PORT), flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
