# Meridian for iOS & Android (Capacitor)

The mobile apps are the **same** `meridian-relief.html` UI, wrapped by [Capacitor] and pointed at the hosted
feed. There's no second codebase: `build_www.py` copies the shared HTML into `www/index.html` and injects
your feed URL. On mobile there's no Python desktop bridge, so the UI's `aiBridge()` automatically switches to
**web-fetch mode** — it downloads `world_<h>h.json` from the feed and renders the map + headlines + the
pre-baked **copyright-free Groq summaries** (each generated once on the server, identical for every user).

## Prerequisites
- **Deploy the feed server first** (see [`../DEPLOY.md`]) and note its URL, e.g. `https://feed.example.com`.
- **Node.js 18+** and npm.
- **iOS:** a Mac with **Xcode**. **Android:** **Android Studio** (+ JDK 17).
- Python 3 (for `build_www.py`).

## One-time setup
```bash
cd mobile
echo "https://feed.example.com" > feed_base.txt      # your deployed feed URL
npm install
npm run add:android      # creates android/ (needs Android Studio)
npm run add:ios          # creates ios/     (needs a Mac + Xcode)
```

## Build / run
```bash
npm run sync             # rebuild www from the latest UI + copy into the native projects
npm run open:android     # opens Android Studio -> Run on device/emulator
npm run open:ios         # opens Xcode -> Run on device/simulator
```

Whenever you change `../meridian-relief.html`, run `npm run sync` again. Because the UI is downloaded logic +
a hosted feed, most **content and even UI updates ship over-the-air** (rebuild the feed / re-host the HTML);
you only rebuild the native app for Capacitor/native-plugin changes or store releases.

## What works in this v1
- The live map, dots, geolocation, headlines, and the **"In brief · Meridian"** summary (baked into the feed).
- Share links / branded cards (served by the feed server).

## Deferred to the enrichment-API phase
Per-click extras that today use the desktop bridge — people faces, article photos, video clips, country
leaders — **gracefully no-op** on mobile (their `if(!api || !api.x)` guards skip them). To light them up,
stand up the hosted enrichment API described in `../SCALING.md` and add matching methods to the `webApi()`
shim in `meridian-relief.html`; every call site already routes through `aiBridge()`, so nothing else changes.

## App icons / splash
Drop a 1024×1024 icon and a splash image in and run `npx @capacitor/assets generate` (add
`@capacitor/assets` as a dev dep) to populate both platforms.

[Capacitor]: https://capacitorjs.com
[`../DEPLOY.md`]: ../DEPLOY.md
