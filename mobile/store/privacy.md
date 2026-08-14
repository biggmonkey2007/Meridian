# Privacy — App Store "App Privacy" + Google Play "Data safety"

Meridian's mobile app is a **thin client**: it downloads a pre-built **public** feed (`world_<h>h.json`) over
HTTPS and renders it. There are **no accounts, no login, no ads, no analytics SDK, no tracking**, and it never
asks for location, contacts, camera, microphone, or photos.

## Data collected / shared
- **None.** The app collects no personal data and shares none with third parties.
- Whatever hosts the feed/CDN may keep standard request logs (IP + user-agent) — that is operator
  infrastructure, not app-collected data. Disclose per your host's policy if relevant.

## Third-party content
- Article links, photos, and embedded clips open their **original public sources** (Wikipedia, YouTube, news
  outlets). Those sites have their own policies; Meridian only links to them.

## Permissions
- **Internet only.** No runtime permissions are requested.

## Store form quick-answers
- **App Store → App Privacy:** "Data Not Collected."
- **Play → Data safety:** No data collected · No data shared · Encrypted in transit: **Yes** (HTTPS).
- **iOS Privacy Manifest (`PrivacyInfo.xcprivacy`):** no tracking, no collected data types, no
  required-reason APIs beyond the defaults. Capacitor's template is fine; add the file only if a plugin needs it.

## Required
Both stores require a public privacy-policy **URL**. Render this file (or equivalent) at
`https://<your-domain>/privacy` — the feed server (`../../serve_feed.py`) can host it as a static page.
