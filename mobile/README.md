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
Already wired. `python make_mobile_icons.py` renders the Meridian globe into `resources/icon.png` (1024²,
opaque — iOS forbids alpha) and `resources/splash.png` (2732²); `npm run icons` then runs
`@capacitor/assets` to fan them out to every platform size. This runs automatically inside `npm run add:ios`
/ `add:android`. Re-run `npm run icons` after changing the mark or brand colors.

## Version
`APP_VERSION` in `../app.py` is the single source of truth. `python set_version.py` (aliased `npm run
version`, and part of `sync` / `add:*`) copies it into `package.json`, `android/app/build.gradle`
(`versionName` + a derived integer `versionCode`), and `ios/.../Info.plist` (`CFBundleShortVersionString` +
`CFBundleVersion`). Bump `APP_VERSION`, run it, and every surface stays in lock-step.

## Ship to the App Store / Play Store
Store copy, keywords, screenshot list, age rating, and the privacy answers both stores demand live in
[`store/listing.md`](store/listing.md) and [`store/privacy.md`](store/privacy.md).

**Android (Play):**
```bash
npm run add:android          # once: creates android/, icons, version
npm run sync                 # after any UI/feed change
npm run open:android         # Android Studio -> Build > Generate Signed App Bundle (.aab)
```
Upload the `.aab` in Play Console, fill Data safety from `store/privacy.md`, paste the listing, submit.

**iOS (App Store — needs a Mac + Xcode + Apple Developer account):**
```bash
npm run add:ios
npm run sync
npm run open:ios             # Xcode -> set your Team/signing -> Product > Archive -> Distribute
```
In App Store Connect, answer App Privacy = "Data Not Collected", paste the listing, add screenshots, submit.

Both `ios/` and `android/` are generated (gitignored); `resources/`, the scripts, config, and `store/` are
the committed source, so the projects regenerate identically on any machine.

[Capacitor]: https://capacitorjs.com
[`../DEPLOY.md`]: ../DEPLOY.md
