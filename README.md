# Meridian

**A live, map-first world-news reader for someone who wants to actually understand what is
happening right now — faster and more honestly than the standard press.**

A desktop app (pywebview / WebView2). Backend + launcher: `app.py`. UI (single file):
`meridian-relief.html`.

Everything (gazetteer, channels, key, cache) is resolved relative to `app.py`, so the folder
is self-contained — move or copy it anywhere and it still runs.

Run it: `pythonw app.py`

### Auto-open after edits (Claude Code)
When Claude Code finishes an update, the app **relaunches itself** so you see the change immediately.
This is a `Stop` hook in [`.claude/settings.json`](.claude/settings.json) that runs
[`open_meridian.ps1`](open_meridian.ps1). The script only reopens when `meridian-relief.html` or
`app.py` actually changed (plain chat turns don't relaunch it). On each relaunch it **closes every
Meridian window still open** — matched by window title (`Meridian`), so manually- or previously-opened
ones are caught too, not just the last one it launched — leaving you exactly one fresh window instead of
a pile. Its bookkeeping (`.meridian_opened`, `.meridian.pid`) lives in `cache/`. To turn it off, delete
the `Stop` hook (or run `/hooks` in Claude Code); to open the app by hand, just run the script or
`pythonw app.py`.

---

## 1. What this product is trying to be

Read this before changing anything. Every design decision below exists to serve it.

> ### The standard: professional, or it does not ship
> **We are competing with million-dollar newsrooms, and we cannot afford mediocrity.** Every time
> anyone touches this bot, the bar is the utmost professional standard — the standard of a paid,
> world-class product, not a hobby project. Concretely, that means:
> - **Every article reads like a professional wrote it.** Clean, sharp, crisp paragraphs; no truncation
>   (`[...]`, trailing "…"), no stray quotation marks, no raw source stamps ("via Truth Social:",
>   "Axios reports", "Disclose.tv"), no bullets that just restate the headline. If it isn't publishable
>   in a real outlet, it isn't done.
> - **Every fact per country is correct and current.** Heads of state and government, the cabinet
>   (VP, foreign & defence ministers…), and the overview must be accurate and refreshed daily — never
>   a stale or wrong name. When a name can't be verified, self-heal from another source; never guess.
> - **Every dot is in the right place, and every picture loads and never lies.** A wrong location, a
>   broken image, or a mislabelled photo is a P0, not a cosmetic nit.
> - **"Good enough" is a regression.** Ship the polished version or keep working. If a free, scalable
>   source can make it more accurate, use it before settling for less.

- **A map of what is happening in the world, right now.** Every real event is a dot, placed
  **where it actually happened** — not on the capital, not on the publisher's country.
- **Fast and plentiful, like the OSINT Telegram channels the user actually reads** (Rerum
  Novarum, Tabz, NOELREPORTS…), which are quicker and more detailed than wire copy. The
  ⚡ Live Wire is the firehose; the map is the curated layer.
- **Deeply informative per event.** Click a dot → a real headline, clean paragraphs, a
  relevant photo, and the actual **clips/footage** of *that* event pulled from the wire.
- **Every dot must mean something.** No think-pieces, no op-eds, no "week in pictures".
- **Every card has a picture, and no picture lies.** These are not in tension — the difference is
  *labelling*. The hero image is, strictly in order:
  1. the article's **own** share image;
  2. a real photo of **this event** from the wire (a matched Telegram clip);
  3. a **file photo of the place**, and it *says so* — a `FILE PHOTO · <place>` chip you cannot miss.

  A stock photo is only a lie when it is **passed off as evidence**. What was removed (correctly) was
  a Wikipedia picture of the *topic*, presented with a credit line as if it were a picture of the
  story: any headline with the word "drone" got a stock *"Unmanned aerial vehicle"*, a refinery strike
  got a stock *"Oil refinery"*, a shooting got a photo of *"Police"*. A labelled file photo of the
  place is honest — it shows you where in the world this is — and it keeps every card colourful
  instead of leaving a bare colour block. **Never remove the chip.**
- **Correctness is the product.** A dot in the wrong country, a clip under the wrong story,
  or a war story filed under "Science & Tech" is an "entry-level mistake" — treat them as P0.

### Voice and tone — neutral, factual, non-partisan

The writing has **one job: report what happened, accurately and plainly.** The tone is professional and
neutral — the voice of a wire desk, not a columnist. This is deliberate and it is a feature, not a gap:

- **No editorial slant is written into the news** — not left, not right, not for or against any nation,
  party, religion, or group. A contested claim or loaded label is **attributed to whoever said it**
  ("a Russian senator said", "according to Israel's government"); our own voice states only uncontested
  facts. This is what makes the app trustworthy to *every* reader and keeps the summaries fair-use rather
  than derivative.
- **The credibility is the product.** The moment the writing tilts to flatter one side, it stops being
  news and becomes advocacy — and a reader who senses the tilt stops trusting every other dot on the map.
  A million-dollar newsroom's reputation dies the same way. So the summarizer is instructed to be neutral
  (see `_summarize` in `app.py`), and that instruction is not to be replaced with a partisan, ideological,
  or identity-based ("pro/anti a race, religion, or nation") point of view. Keep the facts; drop the spin.

### The target is ALL the news, ALL of it correctly placed

**Both. Not a trade between them.** An earlier draft of this file said a wrong dot is "worse
than no dot at all" — that is *wrong*, and it quietly licenses the laziest possible fix:
delete the story. **Never drop a story to dodge a hard location.** The answer to a dot in the
wrong place is a dot in the *right* place.

So when geolocation is uncertain: **keep the dot, and go fix the reasoning.** Every location bug
in this app has turned out to be a missing *rule* (actors sink, targets sink, ministries act from
their own capital), never a reason to bin the news. Find the rule, add the case to
`test_meridian.py`, and the dot gets more correct for every future story of that shape.

Aspiration (not yet reached): a strike on an oil refinery should be pinned to *that
refinery*, within a mile — because there are only so many refineries in a region.

### Where this is going: a real app

Meridian is a desktop app today, but the destination is a **phone app — iOS and Android**, and
anywhere else that makes sense. **Assume it will be used by millions of people** — every design and
coding decision has to hold at that scale:

- **Built for millions — no per-user setup, no per-user secrets.** A feature must work for *anyone* the
  moment they open the app: never make a user register an API key, log in to a third party, or run a
  setup script, and **never ship a personal credential or session** (e.g. a Telegram login) — that would
  make every user act as *you*. Prefer sources that scale for free and need no account (public feeds,
  embeddable players like YouTube) over ones needing the operator's private access; when a real service
  is unavoidable it lives on **one shared backend**, never baked into each client.
- **The backend is the product.** `app.py` is the pipeline (fetch → classify → geolocate → dedup)
  and it must stay a clean, transport-agnostic core. It talks to the UI over one narrow surface
  (`Api.world_events`, `Api.live_feed`, `Api.event_media`…), so the same calls can be re-exposed
  over **HTTP/JSON** to a phone client. **Do not entangle news logic with pywebview.**
- **Do not fork the intelligence into the client.** Classification, geolocation, dedup and the
  people/photo gating belong in the backend, so iOS, Android and desktop all get the *same*
  answer. The map is a renderer.
- **The clients differ only in projection and touch targets** — the same rule that already binds
  the flat map and the globe (RULE 2) extends to phone.

---

## 2. THE RULES (do not break these)

### RULE 1 — Run the tests. Always.
```
python test_meridian.py        # 133 cases, all must pass
```
**Every case is a bug that actually shipped**, and each one is annotated with why it exists.
The runner asserts `ran == total`: for months `HEADLINE_CASES` and `DATELINE_CASES` were declared,
counted in the printed total, reported as "PASSED" — and **never executed**, because nothing looped
over them. A test that does not run is worse than no test: it reports safety it is not providing.
This file is the memory of this project. Fixing one thing has repeatedly broken another
(correctly reclassifying a story pushed it into a full category cap and it silently vanished).
If you change classification, geolocation, dedup, or clip matching — run it, and **add a case**.

### RULE 2 — Flat map and globe are the same product.
Every map/news/label/styling change to one **must** be applied to the other. The *only*
difference is the projection.

| Concern       | Flat (Leaflet)                        | Globe (MapLibre GL)                          |
|---------------|---------------------------------------|----------------------------------------------|
| Event dots    | `drawDots()` / `visibleEvents()`      | `globeEventsGeo()` / `updateGlobeData()`     |
| Borders       | app GeoJSON `#1f2630` @0.3            | `g-borders` — same colour/opacity            |
| Ports         | ⚓ anchor, **no name**, `minZ 5`       | `g-ports-pt` ⚓ icon, **no label**, `minzoom 4` |
| Straits/seas  | icon + name, `minZ 5`                 | `g-straits-*` (keep name), `minzoom 4`       |
| Dot colours   | `CAT[c].hex`                          | `catColorExpr()` reads the same `CAT[c].hex` |
| Filters       | `visibleEvents()` (shared)            | `visibleEvents()` (shared)                   |

### RULE 3 — Never first-match-wins. Score, then pick the best.
This single anti-pattern caused most of the worst bugs. An ordered keyword list means one
incidental word decides everything ("**satellite** imagery confirms tanks burned after the
overnight **strike**" → filed under Science & Tech, because security's list lacked bare
"strike" and tech's had "satellite"). Categories, place candidates, and clips are all scored.

### RULE 4 — Never drop a story. Place it correctly instead.
Keep every real event, and put every dot in the right place. These are **not** in tension, and
you may not buy one with the other:
- **Do not delete news to avoid a hard location.** If a story's place is uncertain, that is a
  missing *rule* in `_geolocate` — go find it. Deleting the story hides the bug and loses the news.
- **Do not let a filter eat a real event.** An "event verb" requirement once silently deleted
  *"missiles have **impacted** the port"* and *"the president **nominates** a PM"*. Prefer precision
  filters (the URL section) over clever ones.

Every location bug so far was a missing rule, never a reason to bin the story: ACTORS SINK,
TARGETS SINK, a national ministry acts from its OWN capital. Add the rule, add the test case.

*Exception — the importance gate.* The WORLD map is for country/region/world-changing news, so a
true-but-minor **local** story (a beach eroding) or a broad analysis with no place to pin is hidden
from it (`_map_worthy` / the AI `SCOPE`). That is NOT dropping the news: the **starred-country feed
keeps everything**, and `_hard_news` (casualties, a top official on the record) can never be hidden.

### RULE 5 — Every update does a complete fresh resweep. Bump `_DATA_VER`.
A fix is worthless if the running app keeps serving stale cached dots built by the old code. So on
**every shipped change to the news/AI pipeline, bump the single `_DATA_VER` constant** (top of
`app.py`). It is folded into the feed-cache stamp and the summary + location/scope cache keys, so the
next launch **throws away all stale data and re-does everything from scratch**: the feed is rebuilt
live (re-fetch the wire → re-geolocate with the new rules → re-apply the importance gate) and every AI
product — summary, location (`WHERE`), importance (`SCOPE`) — is regenerated. The fix is then visible
on the next launch instead of self-healing over a later cycle. (The per-feature vers — `_SUM_PROMPT_VER`,
`_AIWHERE_VER` — still exist for targeted invalidation; `_DATA_VER` is the big hammer that resweeps all
of it.) AI geolocation is what deciphers the traps the rules can't — a program *name* like
`'Golden Fleet'` is not the town of Golden, CO — so a resweep is what lets that judgment reach the map.

---

### RULE 6 — Verify from the user's seat before you ship.
A change is not done when the code runs — it is done when it **actually works and makes the app better
for the person using it.** After every change, check both:
1. **Does it work?** Run `test_meridian.py`, and exercise the actual path — build the feed, open the
   affected card/panel/dot, and confirm the change does what it claims (not just that nothing errored).
2. **Is the user's experience better?** Look at the result the way someone reading the app would: is the
   headline clean and complete, the dot in the right place, the photo real, the text publishable? A change
   that passes tests but leaves the card worse to read has not improved anything. If you cannot see it
   improve from the reader's perspective, it is not finished.

---

## 3. Architecture

### The news pipeline — `Api.world_events(24)`
Merges **GDELT** + **45 outlet RSS feeds** (`WORLD_FEEDS`, incl. state press: TASS, RT,
Ukrinform, Global Times, Anadolu…) + **geolocatable Telegram posts**. Then:

1. `_is_fluff(title, url)` — drop non-events. The **URL section** is the reliable signal
   (`/opinion/`, `/features/`, `/featured-documentaries/`, `/podcast/`…). `/video/` alone is
   *not* fluff — France24 files real news there.
2. `_classify(title, desc)` — scored categories (below).
3. `_geolocate(...)` — the big one (below).
4. **Dedup on DISTINCTIVE words only** (`_sigwords - _GENERIC_WORDS`). Comparing raw words
   merged *different* events, because every Russia-security story shares
   {drone, strike, oil, refinery}. The 2-word rule requires the **same place**.
   Then **`_collapse_colocated`** — a final map pass: several dots on the *same specific place*
   within ~6h are one unfolding situation, even when the classifier split them across categories
   (the Odesa barrage arrived as "Kh-22 impacts" → security and "2 on course for Odesa" → politics,
   sharing only the word *Odesa*, so word-overlap never merged them). It keeps the **severest** dot
   per place+window. A bare **country** never collapses (two "Russia" stories are different events);
   nothing is lost from the app — the ⚡ Live Wire still carries every post (the map is the curated
   layer).
5. Caps: sorted **newest-first** *before* capping. **Deliberately set high** (security 70, others
   45, sports 5; ≤30/country) — RULE 4 says never drop a story, and the old **≤7/country** silently
   binned the 8th Russia story of the day, which a full-scale war blows past before breakfast.
   **Dedup** is what protects the map from repetition; a cap is a blunt instrument that throws away
   news we correctly fetched, classified and placed. They remain only as a runaway guard.

### Geolocation — the deepest system. `_geolocate(title, sourcecountry, desc, url)`
**Measured fact: no single technique works. They fail in opposite directions.**
- **spaCy NER alone** misses places it doesn't know (*Kyiv*, *Toretsk* → nothing), mislabels
  (*"Omsk oil refinery"* → ORG; *Zaporizhzhia*, *Valencia* → PERSON), and has **no coordinates**.
- **The gazetteer alone** knows all of those precisely — but thinks a surname is a city
  (*"Bellingham scores"*, *"Lindsey Graham dies"*).

So: **the gazetteer FINDS the place; NER only VETOES.** Order of evidence:

1. **Wire dateline** (`_dateline_place`) — `LUGANSK, July 12. /TASS/.` is the single most
   authoritative location a story carries. Only trusted when it **agrees** with the story
   (same country, or a country the story names) — else a Reuters piece *about Russia* filed
   from **LONDON** would dot London.
2. **Headline scan** (`_scan_places`) — greedy longest n-gram (4→1) over `CITY_CANDS`.
   Candidates are ranked by **prior**: `_COUNTRY_PRIOR` > `_FACILITY_PRIOR` (9M, e.g. *Syzran
   Oil Refinery*, *Shuwaikh Port*) > `_REGION_PRIOR` (5M, states/oblasts/waters) > city population.
   Same-name collisions are resolved by **context** (`_context_mentions`): *Odessa* → Ukraine
   when the story mentions Ukraine, else it used to land in **Texas**.
3. **`_pick_place`** — places with locational context (`in/at/near/over`, or an attack verb)
   win. **ACTORS SINK**: a demonym (*"**Ukrainian** drones strike Azov"*) and a **possessive**
   (*"injured in **Russia's** attack on **Zaporizhzhia**"*) name the actor, not the scene.
   An actor may only sink below a **genuine scene** (a located place, or a city/facility) — never
   below a bare country the story merely *mentions*, or the dot lands on whatever country wandered
   into the sentence last (*"**Iran's** IRGC … launches towards **U.S.** bases"* dotted the **US**).
   **When nothing is a scene, the ACTOR is the scene** — a state acts at home unless told otherwise.
   **TARGETS SINK** for the same reason: a country named only as what a sanction/tariff/bill is
   *aimed at* (*"Senate … **Russia** sanctions"*, *"tariffs **on China**"*) is the instrument's
   object, and **nothing has happened there**. The event is where the body sits, so `_SEATS` /
   `_CAPITAL_SEAT` dot it on the Senate, the Commons, Brussels — never on the target. The subject
   is safe: *"**Russia** says it will respond to new sanctions"* really is about Russia.
   Then leftmost, then city > country.
4. **Containment** — if the winner is a broad area (`_AREA_NAMES`) and a town inside it is
   named, the town wins: *"over occupied **Crimea** … above the **Sovetsky** district"* → Sovetsky.
   Facilities and waters are never upgraded away.
5. **A national body acts from its OWN capital** (`_national_body_actor`) — *"**Türkiye's Foreign
   Ministry** commemorates **Srebrenica** genocide"* was dotted on **Bosnia**; the ceremony was in
   **Ankara**, and Srebrenica is what it was *about*. Needs all three: a country subject, one of its
   state organs (`_STATE_BODIES` — `ministry`/`government`/`embassy`…), and a speech/ceremony verb.
   Only consulted when nothing is "located", so a ministry *reporting* a real event elsewhere
   (*"Russia's Defense Ministry says its forces **captured Toretsk**"*) still dots **Toretsk**.
   `officials` and `authorities` are deliberately **not** state organs — *"Israeli officials say
   Gaza strikes will continue"* is news about **Gaza**.
6. **NER veto** (`_ner_vetoes`) — a full-name span always vetoes (*Lindsey Graham*). A **lone**
   PERSON guess **cannot** veto a place the sentence explicitly points at (*"attack **on** X"*)
   or one the context supports — that was deleting Zaporizhzhia and Valencia.
7. Fall back to the story's **own summary**, and only then the outlet's country (that fallback
   is what once dotted *"Lindsey Graham dies"* on **France**).

**Everything is folded to ASCII first (`_fold`).** Every tokenizer here is `[A-Za-z0-9]+`, which
*shreds* accents: **Türkiye** came out as `["t","rkiye"]` and the country did not exist to the
geolocator at all. The fold is **length-preserving** (one char in, one out) on purpose — spaCy's
NER spans are character offsets into the same string, and `æ → ae` would misalign every veto.

**Data (loaded at startup, must ship next to `app.py`):**
- `cities_gaz.json` — ~32k GeoNames cities. Minor towns need a Capitalised, context-supported
  mention (`_WEAK_CITIES`, `_BAD_CITY_NAMES`) or *"in **defiance** of"* becomes Defiance, Ohio.
- In-code tables: `_WATERS` (88 seas/gulfs/straits/canals — ships cannot burn on land),
  `_FACILITIES`, `_REGIONS` (US states, UK nations, provinces), `_PLACE_ALIASES`
  (Russian transliterations — TASS writes *Zaporozhye*, *Kharkov*, *Odessa*).

### Flags on the card — who is a PARTY to the event (`_involved_countries`)
The country it **happened in always leads**. A **person's nationality is not a party**: *"ICE fatally
shoots 26-year-old **Colombian** man in Maine"* flew the **Colombian flag** over a US story.
A demonym in front of a private individual (`_PERSON_NOUNS` — man/woman/migrant/victim/student…)
is skipped. State actors are deliberately **not** in that list: an *"**Israeli** soldier"* or
*"**Ukrainian** forces"* really do make their country a party.

### Who is this? — faces for the people a story names (`Api.event_people`)
A face beside a name tells the reader instantly who is being talked about — and is the easiest way
to ship a humiliating error, because **names are common**. The wrong John Smith is far worse than no
photo, so a face must survive **all four** gates (`_person_card`):
1. a real **full name** (2–4 tokens, honorifics stripped) — or a **curated** head of state, so a bare
   *"Putin"* / *"Zelenskyy"* still resolves (`_OFFICIAL_WIKI`);
2. Wikipedia has that **exact page** — the title must *equal* the name, so a redirect to a different
   person or a disambiguation page is refused;
3. Wikidata says the subject is a **human** (`P31 = Q5`);
4. …who **holds or held public office** (`P39`), or is a politician/diplomat/officer by occupation.

Gate 4 is what keeps this to "government officials and people like that". **Returning nobody is the
normal, correct outcome** — an ICE shooting victim gets no photo, and *"Michael Brown scores twice"*
gets no photo. The gating lives in the **backend**, not the UI, so phone clients get the same answer.

### Country panel — live leadership & profile (`loadCountryProfile`)
Click a country → overview (Wikipedia / Factbook), **leadership**, and a demographics/government grid.
Two rules matter here and both are coded to self-update, never hardcoded:

- **Leadership is the *current* officeholder, refreshed daily.** `fetchLeadersWikidata` reads Wikidata
  at the **statement level** — `p:P35` / `p:P6` — and keeps only the holder whose term has **no end
  date** (`P582`). Truthy `wdt:P35` returns *every past* holder too, which is exactly how Sudan once
  showed a chairman who left in **2019** (Ibn Auf) instead of **al-Burhan**. Ties break on preferred
  rank, then on "actually has a photo". A daily URL cache-buster (`&_d=<YYYYMMDD>`) plus a day-stamp on
  each cached profile forces a fresh pull **once a day**, so leadership tracks reality within a day of
  Wikidata — with **no hardcoded names** to rot. (Wikidata itself can lag, e.g. a PM's end date not yet
  entered; the app shows "current per Wikidata" and auto-corrects the day the source does.)
- **Photos work for every country, no blanks, no long waits.** The leader's Commons photo (`P18`) comes
  back in the *same* query — no second round-trip — and loads eagerly. If it's missing, `fillLeaderPhotos`
  falls back to the Wikipedia thumbnail; if an image URL still fails at load time, `leadImgFallback`
  swaps it for an initial-letter avatar so the panel never shows a broken-image icon or an empty box.
- **Fast:** stale-while-revalidate. The panel paints the last-seen profile **instantly** (held in
  `localStorage`, so it survives relaunches) and only re-fetches in the background when the cache is from
  a previous day. Bump `_PROF_LS` (`meridian_profiles_vN`) when the profile shape/selection changes so
  stale copies are discarded, not flashed.

### Categories — scored, never first-match
`CAT_STRONG` (3 pts) + `CAT_WEAK` (1 pt), minus `CAT_MASK` (context stripped *before* scoring:
"satellite **imagery**" is how a war is *reported*, not a tech story; a "workers' **strike**"
is not a military one). Highest wins; ties → `CAT_ORDER`. If a headline scores **zero** it is
re-scored over the story text (a bare damage report used to fall to the "politics" default).

### Telegram — `channels.txt` (user-editable)
Public channel previews scraped from `https://t.me/s/<handle>` — **no API key, no login**.
- `Api.live_feed()` → ⚡ **Live Wire** drawer (the firehose, incl. minor/domestic news).
- `Api.event_media(title)` → **clips/photos under the matching dot**.
- `Api.clips_feed()` → ▶ Clips tab, grouped by official.
- Posts are filtered by `_tg_reliable` (drops speculative/threat/unverified — these channels
  do put out false alarms), headlines built by `_tg_headline` (cut at a **sentence**, never
  mid-word; always Capitalised — thread continuations start lowercase).
- `_thumb_ok(url)` uses **Pillow** to reject black/blank video posters (a night drone clip
  scored mean 2.5 / contrast 4.3 — it was being used as the story's hero image).

### Clips → the right story (`event_media`)
Word overlap is **worthless** here — every war story shares {drone, strike, attack}. Matching
requires **distinctive** shared words (names/places, `- _GENERIC_WORDS`) **and** place
agreement. This is why a Bangkok pub fire stopped appearing under a Burkina Faso story.

---

## 4. Dependencies

```
pip install pywebview spacy pillow yt-dlp
python -m spacy download en_core_web_sm
```
spaCy and Pillow are **optional** — the app degrades gracefully if they're missing (NER veto
and thumbnail checks are skipped), it just gets less accurate.

`gemini_key.txt` must be a real `AIza…` AI Studio key. **The current value is the wrong
format** (an OAuth token), so all AI features (Axios-style synthesis, AI-extracted quotes)
are correctly disabled.

### Frame-only clips → YouTube (in-app, and it scales)
A few wire clips are videos Telegram refuses to serve to **any** web client — the `t.me/s/` preview, the
post embed, and yt-dlp all get "media too big / view in Telegram". Those bytes only exist over Telegram's
MTProto API, which needs a logged-in **account** — so pulling them per-user does not scale (see "Built for
millions") and shipping one shared account would be a security hole. **A frame-only clip is therefore not
played from Telegram at all.** Instead the app finds the *same event's footage on YouTube*
(`Api.find_clip` → an embeddable video id, no API key) and embeds that in-app — universal, free, works for
every user. If nothing matches, the real still frame is shown. Ordinary clips still stream natively from
Telegram's CDN through the `/clip` proxy; only the un-fetchable `it.big` ones take the YouTube path.

---

## 5. Known limitations / what's next

- **No AI synthesis.** The "one neutral story from both sides" idea is blocked on a valid
  Gemini key. Perspective country-tabs were built, judged useless, and **removed**.
- **Placeless stories** still fall back to the outlet's country (rare now, but it happens).
- **Same-name cities** resolve by context, then population — so an unqualified "Valencia"
  picks the biggest one.
- **Facility-level precision** now works: a curated facility **outranks a city that merely got the
  preposition** (*"hits the **Afipsky refinery** in Krasnodar region"* → the refinery, not Krasnodar),
  it is **never NER-vetoed** (spaCy calls every refinery an ORG), and it beats *"Zelensky **said**…"*.
  Extending `_FACILITIES` is now purely a data job — every name added is a dot pinned to the metre.
- The globe's basemap is raster, so its city labels come from CARTO, not from our gazetteer.