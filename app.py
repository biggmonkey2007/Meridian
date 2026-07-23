"""
Meridian — desktop app launcher (news + clips backend).

Runs the Meridian web app inside a native window using the Edge/WebView2
engine (which, unlike your managed Chrome, allows WebGL — so the globe works),
AND exposes a small Python API to the page so it can fetch geolocated world
news and find + embed real YouTube/Telegram clips (no API key needed).

Run it with:   python app.py     (or the "Meridian" Desktop shortcut / Meridian.exe)
"""

import os
import sys
import re
import json
import time
import shutil
import subprocess
import math
import datetime
import hashlib
import functools
import threading
import unicodedata
import concurrent.futures

# Every tokenizer we have is [A-Za-z0-9]+, which SHREDS accents: "Türkiye" came out as ["t","rkiye"]
# and the country simply did not exist to the geolocator. Fold to ASCII first.
# One char in -> exactly one char out, because spaCy's NER spans are character offsets into this
# same text — a length-changing fold (æ -> ae) would silently misalign every veto.
_FOLD_ODD = {"ø": "o", "Ø": "O", "ł": "l", "Ł": "L", "đ": "d", "Đ": "D", "ı": "i", "İ": "I",
             "ß": "s", "æ": "a", "Æ": "A", "œ": "o", "Œ": "O", "þ": "t", "Þ": "T", "ð": "d", "Ð": "D"}


@functools.lru_cache(maxsize=4096)
def _fold(s):
    if not s:
        return s or ""
    if all(ord(c) < 128 for c in s):
        return s
    out = []
    for ch in s:
        if ord(ch) < 128:
            out.append(ch)
            continue
        if ch in _FOLD_ODD:
            out.append(_FOLD_ODD[ch])
            continue
        base = "".join(c for c in unicodedata.normalize("NFKD", ch) if not unicodedata.combining(c))
        out.append(base[0] if base else ch)
    return "".join(out)
import html as _htmlmod
import urllib.request
import urllib.parse

# Let WebView2 use the GPU (and fall back to software if ever needed) so WebGL/globe runs.
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--ignore-gpu-blocklist --enable-unsafe-swiftshader --autoplay-policy=no-user-gesture-required"

import webview

# Paths work both as a plain script AND as a bundled .exe (PyInstaller). When frozen, read-only resources
# (the HTML UI) are unpacked to sys._MEIPASS, while everything WRITABLE — the caches, the user-editable
# channels.txt, an optional key — must live in %LOCALAPPDATA%\Meridian so it persists and isn't wiped when
# the temp bundle is cleaned up. As a script, both are just the folder next to this file.
if getattr(sys, "frozen", False):
    RES_DIR  = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Meridian")
else:
    RES_DIR  = os.path.dirname(os.path.abspath(__file__))
    DATA_DIR = RES_DIR
BASE_DIR  = RES_DIR                                       # back-compat alias for resource lookups
APP_HTML  = os.path.join(RES_DIR, "meridian-relief.html")
KEY_FILE  = os.path.join(DATA_DIR, "gemini_key.txt")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ── VERSION + AUTO-UPDATE ─────────────────────────────────────────────────────────────────────────
# Single source of truth for the app version (installer + updater both read it).
APP_VERSION = "1.2.0"
# GitHub repo ("owner/name") whose Releases hold newer Meridian.exe builds. Empty = auto-update is OFF
# (the app runs normally). It can be set at BUILD time here, OR — so it's "ready the moment you create the
# repo" without rebuilding — by dropping the "owner/name" into %LOCALAPPDATA%\Meridian\update_repo.txt.
UPDATE_REPO_DEFAULT = "biggmonkey2007/Meridian"


def _update_repo():
    try:
        cfg = os.path.join(DATA_DIR, "update_repo.txt")
        if os.path.exists(cfg):
            v = open(cfg, encoding="utf-8").read().strip()
            if v:
                return v
    except Exception:
        pass
    return UPDATE_REPO_DEFAULT


def _ver_tuple(v):
    """'v1.2.3' / '1.2.3' -> (1,2,3) for comparison; junk -> (0,)."""
    nums = re.findall(r"\d+", re.sub(r"^v", "", (v or "").strip(), flags=re.I))
    return tuple(int(x) for x in nums[:4]) or (0,)


def _is_newer(remote, local):
    return _ver_tuple(remote) > _ver_tuple(local)

GEMINI_MODEL = "gemini-2.0-flash"


def load_gemini_key():
    # The Google Gemini integration was removed — the app never uses it. This always reports "no key",
    # so every AI-analysis / AI-quotes path stays off and no Gemini request is ever made.
    return ""


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:80]


# ---------------------------------------------------------------------------
# Live wire — a Telegram-style running feed scraped from public channel previews
# (t.me/s/<channel>), no API key or login needed. Channels are user-editable via
# channels.txt (one @handle or t.me link per line); these are the defaults.
# ---------------------------------------------------------------------------
_TG_FILE = os.path.join(DATA_DIR, "channels.txt")   # writable + user-editable (defaults in code if absent)
_TG_DEFAULT = [
    "disclosetv",       # Disclose.tv — global breaking
    "insiderpaper",     # Insider Paper — breaking
    "WatcherGuru",      # Watcher Guru — markets/crypto/breaking
    "WarMonitors",      # War Monitors — conflict OSINT
    "bellumactanews",   # Bellum Acta — war news
    "noel_reports",     # NOELREPORTS — Ukraine
]


_TG_POOL = {"t": 0.0, "posts": []}


def _tg_all_posts(ttl=90):
    """One shared fetch of every channel's recent posts. event_media is called per opened story, so
    without this each story re-hit all 8 channels over the network."""
    now = time.time()
    if _TG_POOL["posts"] and (now - _TG_POOL["t"] < ttl):
        return _TG_POOL["posts"]
    chans = _tg_channels()
    posts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(chans) or 1)) as ex:
        for res in ex.map(_tg_fetch, chans):
            posts.extend(res)
    _TG_POOL["t"] = now
    _TG_POOL["posts"] = posts
    return posts


def _tg_channels():
    """Read channels.txt if present (one handle / t.me link per line, # comments ok), else defaults."""
    try:
        if os.path.exists(_TG_FILE):
            out = []
            for line in open(_TG_FILE, encoding="utf-8").read().splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                m = re.search(r"(?:t\.me/(?:s/)?)?@?([A-Za-z0-9_]{3,})", line)
                if m and m.group(1) not in out:
                    out.append(m.group(1))
            if out:
                return out[:14]
    except Exception:
        pass
    return list(_TG_DEFAULT)


def _tg_ts(iso):
    try:
        return datetime.datetime.fromisoformat(iso).timestamp()
    except Exception:
        return 0.0


def _tg_clean(text):
    t = re.sub(r"<br\s*/?>", "\n", text)
    t = re.sub(r"</p>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = _htmlmod.unescape(t)
    t = re.sub(r"[ \t]+", " ", t)
    # drop the channel's own self-promo handle lines, "Read here:" boilerplate and injected ads
    _AD = re.compile(r"(?i)(rainbet|non-kyc|casino|sportsbook|promo code|use code|deposit bonus|betting|\bt\.me/\+|📲|referral)")
    lines = [ln.strip() for ln in t.split("\n")]
    out = []
    for ln in lines:
        if not ln:
            continue
        if re.fullmatch(r"@[A-Za-z0-9_]+", ln):
            continue
        if re.match(r"(?i)^(read (here|more)|subscribe|join our|follow us)\b.*", ln):
            continue
        # Telegram's own "this post can't be shown here" chrome
        ln = re.sub(r"(?i)\s*please open telegram to view this post\s*", " ", ln)
        ln = re.sub(r"(?i)\s*view in telegram\s*", " ", ln).strip()
        if not ln:
            continue
        if ln.startswith("💧") or _AD.search(ln):
            continue
        out.append(ln)
    return re.sub(r"\n{2,}", "\n", "\n".join(out)).strip()


def _tg_fetch(ch):
    """Return recent posts for one channel: [{channel,title,text,ts,time,link,photo}]."""
    try:
        req = urllib.request.Request(
            "https://t.me/s/" + ch,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        h = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace")
    except Exception:
        return []
    tm = re.search(r'tgme_channel_info_header_title[^"]*"[^>]*>\s*<span[^>]*>(.*?)</span>', h, re.S)
    title = _clean_channel(_htmlmod.unescape(re.sub(r"<[^>]+>", "", tm.group(1)).strip()) if tm else ch)
    # THE CHANNEL'S REAL PICTURE. Telegram serves it on the preview page — as the page's og:image and
    # again on every message row — so the wire never needed to invent letter tiles. Only channels that
    # genuinely have no photo fall back to initials.
    am = (re.search(r'tgme_page_photo_image[^>]*src="([^"]+)"', h)
          or re.search(r'tgme_widget_message_user_photo[^>]*>\s*<img[^>]*src="([^"]+)"', h)
          or re.search(r'<meta property="og:image" content="([^"]+)"', h))
    avatar = _htmlmod.unescape(am.group(1)) if am else ""
    if not avatar.startswith("http"):
        avatar = ""
    out = []
    for chunk in re.split(r'(?=<div class="tgme_widget_message[ "])', h):
        dp = re.search(r'data-post="([^"]+)"', chunk)
        if not dp:
            continue
        post = dp.group(1)
        tx = re.search(r'<div class="tgme_widget_message_text js-message_text[^"]*"[^>]*>(.*?)</div>\s*(?:<div class="tgme_widget_message_|<a class="tgme_widget_message_date)', chunk, re.S)
        text = _tg_clean(tx.group(1)) if tx else ""
        dt = re.search(r'<time[^>]*datetime="([^"]+)"', chunk)
        when = dt.group(1) if dt else ""
        # ALL the photos, not one — and mind the quoting. Telegram writes background-image:url('…')
        # with PLAIN single quotes, but this only ever matched the HTML-escaped &#39; form, so it
        # found NOTHING on a normal post. A NOELREPORTS album of a struck logistics hub — four
        # photographs of burnt-out trucks — was scraped as "no media", and the card fell back to a
        # stock photo of Luhansk city. We had the pictures all along and threw them away.
        photos = [_css_url(u) for u in
                  re.findall(r"tgme_widget_message_photo_wrap[^>]*background-image:url\(([^)]+)\)", chunk)]
        photos = [p for p in photos if p.startswith("http")]
        photo = photos[0] if photos else ""
        # video (inline .mp4 / round video), its poster thumbnail and duration
        vm = re.search(r'<video[^>]*\bsrc="([^"]+)"', chunk)
        video = _htmlmod.unescape(vm.group(1)) if vm else ""
        vts = [_css_url(u) for u in
               re.findall(r"tgme_widget_message_video_thumb[^>]*background-image:url\(([^)]+)\)", chunk)]
        vthumb = next((v for v in vts if v.startswith("http")), "")
        dm = re.search(r"message_video_duration[^>]*>([^<]+)<", chunk)
        dur = dm.group(1).strip() if dm else ""
        # "MEDIA IS TOO BIG". Telegram refuses to serve big videos to the web preview — there is no
        # <video src> anywhere, not on the post page, not in the embed. But it still hands us a real
        # frame OF THAT FOOTAGE, its duration, and a link. We were binning all three and showing
        # nothing at all, which is strictly worse than showing the frame and saying where to watch it.
        # ...but ONLY when there is genuinely no file. A chunk can carry both markers; if Telegram gave
        # us a real <video src>, the post is playable and must never be flagged as blocked.
        big = (not video) and ("message_media_not_supported" in chunk) and bool(vthumb)
        if not text and not photo and not video and not vthumb:
            continue
        ts = _tg_ts(when)
        if not ts:
            continue
        out.append({
            "channel": ch, "title": title, "text": text, "ts": ts, "time": when,
            "link": "https://t.me/" + post, "photo": photo, "photos": photos[:6],
            "video": video, "thumb": (vthumb or photo), "dur": dur, "big": big,
            "avatar": avatar,
        })
    return out[-16:]


def _css_url(raw):
    """background-image:url(...) — Telegram quotes it plain ('…'), escaped (&#39;…&#39;) or not at all."""
    u = _htmlmod.unescape((raw or "").strip())
    return u.strip("'\" \t")


# officials worth grouping interview clips under (first match wins)
_OFFICIALS = [
    "Trump", "Vance", "Rubio", "Hegseth", "Biden", "Putin", "Lavrov", "Medvedev",
    "Zelenskyy", "Zelensky", "Netanyahu", "Khamenei", "Pezeshkian", "Xi Jinping", "Xi",
    "Kim Jong Un", "Erdogan", "Macron", "Starmer", "Merz", "Scholz", "Meloni", "Modi",
    "Milei", "Orban", "von der Leyen", "Guterres", "Rutte", "Sharif", "al-Sisi", "Sisi",
]


def _official_in(text):
    t = text or ""
    for name in _OFFICIALS:
        if re.search(r"\b" + re.escape(name) + r"\b", t, re.I):
            if name == "Zelenskyy":
                return "Zelensky"
            if name in ("Xi",):
                return "Xi Jinping"
            if name == "Sisi":
                return "al-Sisi"
            return name
    return ""


def _clean_channel(name):
    """The channel's OWN name, not its slogan. Telegram titles are a dumping ground:
        "Bellum Acta - Intel, Urgent News and Archives ✝️ #FreeVenezuela"
    …which the card then printed three times — as the source, in the LIVE badge, and inside the
    "Read the original at …" button. Cut the tagline, drop the hashtags and the flair."""
    n = _htmlmod.unescape(name or "").strip()
    n = re.sub(r"#\w+", " ", n)                                  # #FreeVenezuela
    n = re.sub(r"[^\w\s.,'&()\-|:–—]", " ", n, flags=re.UNICODE)  # emoji / crosses / flags
    n = re.split(r"\s[|:\-–—]\s|\s{2,}", n.strip())[0]           # cut at the tagline separator
    n = re.sub(r"\s+", " ", n).strip(" -–—|:,.")
    return n[:38] or (name or "").strip()[:38]


# Wire-tweet accounts (Insider Paper, etc.) staple promo onto a post: a leading "BREAKING -", a trailing
# link, "READ: <url>", "Follow @Handle for more news". A headline — and its summary — is a sentence, not a
# call to action. Strip it from BOTH so a link or a "go follow @them" never becomes a dot or a story body.
_PROMO_URL    = re.compile(r"(?:https?://|www\.)\S+|\bt\.co/\S+", re.I)
_PROMO_LEAD   = re.compile(r"^\s*(?:breaking|just\s?in|update|developing|exclusive|alert|flash|watch)\s*[-:–—]+\s*", re.I)
_PROMO_TAIL   = re.compile(
    r"[\s\-–—|]*follow\s+(?:@[\w.]+|us)\b.*$"                                  # "Follow @Handle …" / "Follow us …"
    r"|[\s\-–—|]*(?:subscribe|join our (?:channel|telegram|whatsapp))\b.*$"    # channel plugs
    r"|[\s\-–—|]*for\s+more\s+(?:news|updates?|stories|info|coverage)\b.*$"    # "… for more news"
    r"|[\s\-–—|]*(?:read(?:\s+more)?|watch|more|link|source|via|details?|full\s+story)\s*:\s*$",  # a label + colon left dangling after the URL was cut
    re.I | re.S)
_PROMO_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z]\w{2,}")                            # stray "@InsiderPaper"
def _strip_promo(t):
    t = _htmlmod.unescape(t or "")
    t = _PROMO_LEAD.sub("", t)
    t = _PROMO_URL.sub(" ", t)          # bare links first, so a "READ: <url>" collapses to a strippable "READ:"
    prev = None
    while prev != t:                    # trailing promo stacks: "… READ: <url>  Follow @x for more news"
        prev = t
        t = _PROMO_TAIL.sub("", t)
    t = _PROMO_HANDLE.sub("", t)
    return re.sub(r"\s{2,}", " ", t).strip(" \t\r\n-–—|:")


def _tg_headline(text):
    """A map-ready headline: first solid line, urgency tags/emoji stripped, cut at a SENTENCE end (never
    mid-word — "...or Russian attack, The Ti" shipped), and always Capitalised, because Telegram threads
    post continuations that begin lower-case ("imagery also shows significant damage to...")."""
    lines = [ln.strip() for ln in (text or "").split("\n") if ln.strip()]
    line, idx = "", -1
    for k, ln in enumerate(lines):
        if len(ln) >= 12:
            line, idx = ln, k
            break
    if not line:
        line = (text or "").strip()
    # A LEAD-IN IS NOT A HEADLINE. These channels put the speaker on line 1 and what he actually SAID
    # on line 2 — "Russian Foreign Minister Sergey Lavrov:" was going on the map as a dot with no news
    # in it at all. Don't drop the post (RULE 4), finish the sentence.
    if idx >= 0 and re.search(r"[:\-–—]$", line):
        for nxt in lines[idx + 1:]:
            body = re.sub(r"^[\W_]+", "", nxt).strip()
            if len(body) >= 12:
                line = line.rstrip(":-–— ") + ": " + body
                break
    line = re.sub(r"^[\W_]+", "", line)  # leading emoji / symbols
    line = re.sub(r"^(#?breaking|just\s?in|update|flash|now|new|developing|exclusive|watch|alert|report)\b[\s:–\-—]*",
                  "", line, flags=re.I).strip()
    line = re.sub(r"^[\W_]+", "", line)
    line = re.sub(r"\s+https?://\S+$", "", line).strip()   # trailing bare URL
    line = _strip_promo(line)                              # mid-string links, "Follow @x", stray handles
    if len(line) > 150:
        # prefer a real sentence end; the lookbehind keeps "U.S." from counting as one
        cut = -1
        for mm in re.finditer(r"(?<=[a-z0-9)\"'])[.!?](?:\s|$)", line[:175]):
            if mm.end() >= 60:
                cut = mm.end()          # FIRST complete sentence, not the last one that fits
                break
        if cut >= 60:
            line = line[:cut].strip()
        else:
            w = line.rfind(" ", 0, 150)
            line = (line[:w].rstrip(",;:-–— ") if w > 40 else line[:150].rstrip()) + "…"
    if line and line[0].islower():
        line = line[0].upper() + line[1:]
    return line.strip()


_TG_SPECULATIVE = re.compile(
    r"\b(could|would|might|may|reportedly|allegedly|alleged|claim|claims|claimed|"
    r"rumou?r|rumou?rs|unconfirmed|purportedly|apparently|appears?\s+to|seems?\s+to|"
    r"locked and loaded|aimed at|threaten\w*|warns?|warned|vow\w*|"
    r"plan(?:s|ning)?\s+to|set to|expected to|likely to|about to|preparing to|prepares? to|"
    r"imminent|brace[sd]?\s+for|fear\w*|possible|possibly|speculat\w*|"
    r"if\s+(?:iran|russia|china|israel|the\s+us)|would\s+(?:strike|attack)|to\s+strike)\b", re.I)


# The channel talking about ITSELF is not news. "If you reside anywhere in West Asia affected by the
# resuming war, please feel free to give us combat updates and footage in your area via the channel…"
# was a dot on the map.
_TG_HOUSEKEEPING = re.compile(
    r"\b(subscribe|join (our|the) channel|our channel|via the channel|send (us|your)|"
    r"submit (your|footage)|dm us|contact us|give us (combat )?updates|share (with|your) us|"
    r"follow us|boost the channel|donate|patreon|paypal|advertis|promo code|"
    r"mirror channel|backup channel|reserve channel|read more at|link in bio)\b", re.I)

# A channel admin's PERSONAL message — a greeting, a sign-off, a thank-you. NOT an event. "Good night,
# sleep well and see you all tomorrow!" is not news. These slip past the housekeeping filter (they are
# not self-promotion) so the wire showed them verbatim.
_TG_CHATTER = re.compile(
    r"\b(good\s?(night|morning|evening|afternoon)|goodnight|"
    r"sleep well|sleep tight|see (you|ya)(\s+all)?\s+(tomorrow|soon|later|in the morning)|"
    r"see everyone (tomorrow|soon)|catch you (tomorrow|later)|"
    r"have a (great|good|nice|lovely|blessed|wonderful|safe|restful)\s+(day|night|evening|weekend|week|one)|"
    r"stay safe (everyone|out there|all|folks)|take care (everyone|all|folks|out there)|"
    r"thank(s| you)\s+(you )?(all|everyone|guys|folks)|"
    r"thank(s| you)\s+for\s+(watching|reading|following|your support|being (here|with us)|the support)|"
    r"that'?s (all|it)\s+(for )?(today|now|tonight|this evening)|"
    r"wrapping up|signing off|that'?s a wrap|wrap for (today|tonight|the (day|night|evening))|"
    r"(be |we'?ll be |back )back (tomorrow|in the morning|shortly|soon|first thing)|"
    r"until (tomorrow|next time|the morning)|till (tomorrow|the morning)|"
    r"get some (rest|sleep)|rest well|off to (bed|sleep)|calling it a (night|day)|"
    r"we'?re (back|off|signing off|done for)|we are back|"
    r"happy (new year|birthday|holidays|friday|weekend|easter|thanksgiving)|"
    r"enjoy your (day|evening|weekend|night)|good to be back)\b", re.I)


def _tg_is_chatter(text):
    """The admin talking TO the audience rather than reporting an event — a greeting, a sign-off, a
    thank-you, or channel self-promotion. Filtered from the wire; the firehose keeps everything else
    (including speculative breaking posts, which `_tg_reliable` drops only for MAP dots)."""
    h = (text or "").strip()
    if not h:
        return True
    return bool(_TG_CHATTER.search(h) or _TG_HOUSEKEEPING.search(h))


def _tg_reliable(headline):
    """Secondary OSINT channels are noisier — drop speculative, future-tense, threat or unverified posts,
    and anything that is the channel talking about ITSELF rather than reporting an event."""
    h = (headline or "").strip()
    if len(h) < 22:
        return False
    if h.endswith("?"):
        return False
    if _TG_SPECULATIVE.search(h):
        return False
    if _TG_HOUSEKEEPING.search(h):
        return False
    return True


@functools.lru_cache(maxsize=1024)
def _thumb_ok(url):
    """Is this video poster good enough to be a story's HEADLINE picture? A Telegram video poster is
    very often a dark, low-detail frame (night drone footage) — a black or muddy rectangle under the
    headline. The bar here is deliberately HIGH: the hero must be genuinely bright AND sharp, or we
    fall back to a place/person photo. (The poster still appears in the clip strip under a play
    button, where a dark frame is fine — that path does not call this.)"""
    if not url:
        return False
    try:
        import io as _io
        from PIL import Image, ImageStat, ImageChops
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        raw = urllib.request.urlopen(req, timeout=8).read(400000)
        im = Image.open(_io.BytesIO(raw)).convert("L")
        im.thumbnail((80, 80))
        st = ImageStat.Stat(im)
        mean, sd = st.mean[0], st.stddev[0]
        # SHARPNESS: mean absolute 1px horizontal gradient. A blurry/flat frame has almost no edges;
        # a detailed daytime frame has plenty. This is what rejects the blurry smears.
        shifted = ImageChops.offset(im, 1, 0)
        sharp = ImageStat.Stat(ImageChops.difference(im, shifted)).mean[0]
        too_dark = mean < 45          # a hero can't be murky — this is the "no black" bar
        too_flat = sd < 28            # low overall variation = a near-uniform frame
        too_soft = sharp < 6          # few edges = blurry or featureless
        return not (too_dark or too_flat or too_soft)
    except Exception:
        return False


def _tg_arts(h):
    """Recent geolocatable Telegram posts, shaped like RSS 'arts' so world_events can map them as dots."""
    channels = _tg_channels()
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(channels) or 1)) as ex:
            results = list(ex.map(_tg_fetch, channels))
    except Exception:
        results = []
    now = time.time()
    _thumbs = [p["thumb"] for posts in results for p in posts
               if p.get("thumb") and not p.get("photo")]
    if _thumbs:
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=8) as ex:
                list(ex.map(_thumb_ok, _thumbs))     # warms the lru_cache in parallel
        except Exception:
            pass
    out = []
    for posts in results:
        for p in posts:
            ts = p.get("ts") or 0
            if not ts:
                continue
            hrs = (now - ts) / 3600.0
            if hrs < 0 or hrs > h:
                continue
            head = _tg_headline(p.get("text") or "")
            if not _tg_reliable(head):
                continue
            _img = p.get("photo") or ""
            if not _img and p.get("thumb") and _thumb_ok(p.get("thumb")):
                _img = p["thumb"]                      # only if the frame isn't black/blank
            out.append({
                "url": p.get("link") or "",
                "title": head,
                "hrs": round(hrs, 2),
                "socialimage": _img,
                "desc": (p.get("text") or "")[:360],
                "sourcecountry": "",
                "geo_text": (p.get("text") or ""),   # geolocate on the FULL post, so "southern Lebanon" beats the "Israeli" demonym
                "_src": p.get("title") or p.get("channel") or "Telegram",
                "_tg": True,
            })
    return out


def build_prompt(country, fb_hint=""):
    return (
        "You are a PhD-level geopolitical analyst writing for an educated reader who wants to deeply "
        "understand a country's politics, power structure, religious/ethnic/historical foundations, internal "
        "fault lines, key factions, militias and proxies, alliances, and its role in regional and global "
        "dynamics. Be rigorous, specific and balanced — no propaganda, no lazy false-balance, no filler.\n\n"
        "Country: " + country + "\n" + (("Context: " + fb_hint + "\n") if fb_hint else "") +
        "\nReturn ONLY valid minified JSON (no markdown, no code fences) of exactly this shape:\n"
        '{"lens":"a <=200 char strategic thesis for this country",'
        '"sections":[{"h":"short section title","b":"2-4 sentence rigorous analytical paragraph"}],'
        '"actors":[{"name":"exact English Wikipedia article title of a key actor/institution/ideology/sect/event/proxy",'
        '"note":"<=70 char reason it matters"}]}\n'
        "Provide 6 sections, in this order: (1) Historical, religious & ethnic foundations; (2) Power structure — "
        "who really rules; (3) Internal fault lines & conflicts; (4) Key factions, proxies & alliances; (5) Economy "
        "& sources of leverage; (6) The central strategic question it faces now. Provide 8-12 actors whose 'name' is a "
        "real Wikipedia article title (people, institutions, sects, ideologies, treaties, wars, proxy groups). JSON only."
    )


class Api:
    """Exposed to the page as window.pywebview.api.*"""

    def has_ai(self):
        return bool(load_gemini_key())

    def ping(self):
        return {"ok": True, "ai": bool(load_gemini_key())}

    def find_clip(self, query):
        """Resolve the top YouTube result for a query to an embeddable video id (no API key). A long,
        quote-heavy clip caption matches NOTHING on YouTube (tested: the full caption returns 0 results,
        the first ~8 words return the real footage), so fall back to progressively shorter queries and
        keep the first that lands a hit — this is what lets *every* clip find something to play."""
        try:
            cache = os.path.join(CACHE_DIR, "clip_" + _slug(query) + ".json")
            if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 7 * 86400:
                return json.load(open(cache, encoding="utf-8"))
            import yt_dlp
            opts = {"quiet": True, "skip_download": True, "extract_flat": True,
                    "noplaylist": True, "no_warnings": True, "socket_timeout": 15}

            def _search(qq):
                if not qq:
                    return []
                with yt_dlp.YoutubeDL(opts) as ydl:
                    info = ydl.extract_info("ytsearch1:" + qq, download=False)
                return (info or {}).get("entries") or []

            words = re.sub(r'["“”„«»]', " ", query or "").split()
            tries = [query]
            if len(words) > 8:
                tries.append(" ".join(words[:8]))
            if len(words) > 5:
                tries.append(" ".join(words[:5]))
            entries = []
            for qq in tries:
                entries = _search(qq)
                if entries:
                    break
            out = {}
            if entries:
                e = entries[0]
                out = {"id": e.get("id"), "title": e.get("title"),
                       "channel": e.get("channel") or e.get("uploader") or ""}
            try:
                json.dump(out, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return out
        except Exception as ex:
            return {"error": str(ex)}

    def find_clips(self, query, n=5):
        """Up to n recent YouTube results for a query - {clips:[{id,title,channel,dur}]}. No key. Cached 2 days."""
        try:
            try:
                n = int(n)
            except Exception:
                n = 5
            n = max(1, min(8, n))
            cache = os.path.join(CACHE_DIR, "clips_%d_" % n + _slug(query) + ".json")
            if _fresh(cache, 2 * 86400):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            import yt_dlp
            # full extract (not flat) so we get real upload dates; limited to n so it stays quick; cached 2 days
            opts = {"quiet": True, "skip_download": True, "extract_flat": False,
                    "noplaylist": True, "no_warnings": True, "socket_timeout": 15,
                    "ignoreerrors": True, "playlist_items": "1-%d" % n}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info("https://www.youtube.com/results?search_query=" + urllib.parse.quote(query) + "&sp=CAI%253D", download=False)
            out = []
            for e in ((info or {}).get("entries") or []):
                if e and e.get("id"):
                    out.append({"id": e.get("id"), "title": e.get("title") or "",
                                "channel": e.get("channel") or e.get("uploader") or "",
                                "dur": e.get("duration"),
                                "ts": e.get("timestamp") or e.get("release_timestamp")})
            out.sort(key=lambda c: (c.get("ts") or 0), reverse=True)
            _now = time.time()
            for _win in (3, 10, 45):
                _recent = [c for c in out if c.get("ts") and (_now - c["ts"]) < _win * 86400]
                if _recent:
                    out = _recent
                    break
            res = {"clips": out}
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"error": str(ex)}

    def translate(self, text, target="en"):
        """Free machine translation (Google gtx endpoint, no key). Returns {text, src} or {error}. Cached 30d."""
        try:
            text = (text or "").strip()
            if not text:
                return {"text": ""}
            target = re.sub(r"[^a-z-]", "", (target or "en").lower())[:5] or "en"
            cache = os.path.join(CACHE_DIR, "tr_" + target + "_" + hashlib.md5(text.encode("utf-8")).hexdigest()[:16] + ".json")
            if _fresh(cache, 30 * 86400):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            url = ("https://translate.googleapis.com/translate_a/single?client=gtx&sl=auto&tl="
                   + urllib.parse.quote(target) + "&dt=t&q=" + urllib.parse.quote(text[:1800]))
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            j = json.loads(urllib.request.urlopen(req, timeout=15).read().decode("utf-8"))
            out = "".join(seg[0] for seg in j[0] if seg and seg[0])
            res = {"text": out, "src": (j[2] if len(j) > 2 else "")}
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"error": str(ex)}

    def outlet_news(self, domain, limit=12):
        """Recent headlines from ONE outlet (Google News site: search) so browsing stays in-app. Cached 20 min."""
        try:
            domain = (domain or "").strip().lower()
            if not domain:
                return {"items": []}
            cache = os.path.join(CACHE_DIR, "outlet_" + _slug(domain) + ".json")
            if _fresh(cache, 1200):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            res = _news_get_q("site:" + domain + " when:5d", limit)
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"error": str(ex)}

    def perspectives(self, query, domains):
        """Headlines for an event from ONE country's own outlets (domains) so readers can compare sides. Cached 30 min."""
        try:
            query = (query or "").strip()
            domains = [str(d).strip() for d in (domains or []) if d][:6]
            if not query:
                return {"items": []}
            words = [w for w in re.findall(r"[A-Za-z][A-Za-z'\-]{3,}", query) if w.lower() not in _STOP]
            # search on DISTINCTIVE words first (places/names) so we don't drop "Lebanon" for generic filler
            dist = [w for w in words if _stem(w.lower()) not in _GENERIC_WORDS]
            picked = (dist or words)[:5]
            terms = " ".join(picked) if picked else query
            if domains:
                q = terms + " (" + " OR ".join("site:" + d for d in domains) + ") when:6d"
            else:
                q = terms + " when:6d"
            cache = os.path.join(CACHE_DIR, "persp_" + _slug(q)[:90] + ".json")
            if _fresh(cache, 1800):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            res = _news_get_q(q, 14)
            # RELEVANCE GATE: a site: search on Google News falls back to the outlet's TOP story when it
            # has nothing on the event — so require real word-overlap with the event title, or we'd show
            # e.g. "Russian strikes kill 28 in Kyiv" under a story about a Jordan air base.
            qsig = _sigwords(query)
            qdist = qsig - _GENERIC_WORDS   # distinctive words (places, names) — not generic conflict filler
            items = []
            for it in (res.get("items") or []):
                isig = _sigwords(it.get("title") or "")
                if qdist:
                    if qdist & isig:            # must share a distinctive word (Jordan, Iranian, Muwaffaq…)
                        items.append(it)
                elif len(qsig & isig) >= 2:     # query had only generic words — fall back to strong overlap
                    items.append(it)
            out = {"items": items[:8]}
            try:
                json.dump(out, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return out
        except Exception as ex:
            return {"error": str(ex)}

    def live_feed(self, limit=400):
        """A Telegram-style running wire covering the LAST 24 HOURS, then auto-dropping older posts.

        Telegram's public `t.me/s/<handle>` preview only exposes the ~20 most recent posts per channel,
        so a single fetch during heavy news spans only a few hours. To reach a full day we keep a
        ROLLING BUFFER on disk: every poll merges the freshly-visible posts into it, and anything past
        24h is discarded. On a cold start the wire shows whatever the previews expose (a few hours) and
        fills out toward 24h as the app runs."""
        try:
            limit = int(limit or 400)
            now = time.time()
            cutoff = now - 24 * 3600
            buf_path = os.path.join(CACHE_DIR, "livewire_24h.json")

            def _key(p):
                return p.get("link") or ((p.get("channel") or "") + "|" + str(p.get("ts") or "")
                                         + "|" + re.sub(r"\s+", " ", (p.get("text") or "")[:50]).lower())

            buffer, last_fetch = {}, 0
            try:
                if os.path.exists(buf_path):
                    saved = json.load(open(buf_path, encoding="utf-8"))
                    last_fetch = saved.get("fetched", 0)
                    for p in saved.get("posts", []):
                        if (p.get("ts") or 0) >= cutoff and not _tg_is_chatter(p.get("text") or ""):
                            buffer[_key(p)] = p          # also purges chatter cached before this filter
            except Exception:
                pass

            # re-poll the channels at most ~every 50s; between polls the buffer is served as-is
            if now - last_fetch >= 50 or not buffer:
                channels = _tg_channels()
                fresh = []
                with concurrent.futures.ThreadPoolExecutor(max_workers=min(6, len(channels) or 1)) as ex:
                    for res in ex.map(_tg_fetch, channels):
                        fresh.extend(res)
                for it in fresh:
                    if (it.get("ts") or 0) < cutoff:
                        continue
                    if _tg_is_chatter(it.get("text") or ""):
                        continue                         # admin greetings/sign-offs are not news
                    buffer[_key(it)] = it                # newest copy of each post wins
                last_fetch = now

            # 24h window, newest first, drop near-identical reposts
            posts = sorted((p for p in buffer.values() if (p.get("ts") or 0) >= cutoff),
                           key=lambda x: x.get("ts", 0), reverse=True)
            seen, uniq = set(), []
            for it in posts:
                k = re.sub(r"\s+", " ", (it.get("text") or "")[:90]).lower()
                if not k or k in seen:
                    continue
                seen.add(k)
                uniq.append(it)

            try:
                json.dump({"posts": uniq, "fetched": last_fetch},
                          open(buf_path, "w", encoding="utf-8"))
            except Exception:
                pass
            return {"items": uniq[:limit], "channels": len(_tg_channels()),
                    "generated": int(now), "span_h": 24}
        except Exception as ex:
            return {"error": str(ex), "items": []}

    def clips_feed(self, limit=44):
        """Recent VIDEO clips from the channels (interviews, statements, footage), newest first, with the
        featured official detected so the UI can group an unfolding interview together. Cached ~90s."""
        try:
            limit = int(limit or 44)
            cache = os.path.join(CACHE_DIR, "clipsfeed.json")
            if _fresh(cache, 90):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            channels = _tg_channels()
            allp = _tg_all_posts()
            items = []
            seen_media = set()               # the twin substitution can serve one file twice
            for p in allp:
                # A blocked clip used to be skipped outright, so the ▶ Clips tab silently hid the
                # LONGEST footage on the wire — being long is exactly what made it "too big".
                if not (p.get("video") or p.get("big")):
                    continue
                vid, thumb, chan, link = (p.get("video") or ""), (p.get("thumb") or ""), \
                    (p.get("title") or p.get("channel") or ""), (p.get("link") or "")
                yt_id = yt_title = yt_channel = ""
                if not vid:
                    twin = self._playable_twin(p, allp)      # another channel's copy may play
                    if twin:
                        vid = twin.get("video") or ""
                        chan = twin.get("title") or twin.get("channel") or chan
                        link = twin.get("link") or link
                    else:
                        yt = _exact_youtube(p.get("text") or "")   # the EXACT footage on YouTube, else drop
                        if not (yt and yt.get("id")):
                            continue
                        yt_id, yt_title, yt_channel = yt["id"], yt.get("title") or "", yt.get("channel") or ""
                mkey = vid or thumb
                if not mkey or mkey in seen_media:
                    continue                 # the same file, already on the wall
                seen_media.add(mkey)
                cap = _tg_headline(p.get("text") or "") or (p.get("text") or "").strip()
                items.append({
                    "channel": chan,
                    "avatar": p.get("avatar") or "",
                    "caption": cap[:220],
                    "official": _official_in(p.get("text") or ""),
                    "ts": p.get("ts") or 0,
                    "time": p.get("time") or "",
                    "video": vid,
                    "thumb": thumb,
                    "dur": p.get("dur") or "",
                    "link": link,
                    "youtube": yt_id,
                    "yt_title": yt_title,
                    "yt_channel": yt_channel,
                })
            items.sort(key=lambda x: x.get("ts", 0), reverse=True)
            out = {"items": items[:limit], "generated": int(time.time())}
            if items:
                try:
                    json.dump(out, open(cache, "w", encoding="utf-8"))
                except Exception:
                    pass
            return out
        except Exception as ex:
            return {"error": str(ex), "items": []}

    def _playable_twin(self, p, posts):
        """THE SAME FOOTAGE FROM A CHANNEL THAT WILL ACTUALLY SERVE IT.
        OSINT channels repost each other constantly, and whether Telegram releases the file is a
        property of the UPLOAD, not the footage — one channel's copy is 30MB and blocked, another's is
        18MB and streams fine. So when our copy is blocked, go and find a twin that is not. It has to
        pass the same subject match as any other clip, so we can never staple the wrong video on."""
        head = _tg_headline(p.get("text") or "")
        if not head:
            return None
        best = None
        for q in posts:
            if q is p or not q.get("video"):
                continue
            if _clip_matches(head, q.get("text") or ""):
                if best is None or (q.get("ts") or 0) > (best.get("ts") or 0):
                    best = q
        return best

    def event_media(self, title, limit=6):
        """Photos & video clips from the Telegram channels that match THIS story, for the media strip."""
        try:
            title = (title or "").strip()
            if not title:
                return {"items": []}
            cache = os.path.join(CACHE_DIR, "media_" + _slug(title)[:70] + ".json")
            if _fresh(cache, 600):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            want = _sigwords(title)
            if not want:
                return {"items": []}
            want_proper = _proper_words(title)
            _ev = _geolocate(title, "", "")
            _ev_place = _ev[2] if _ev else ""
            _ev_country = _ev[3] if _ev else ""
            out = []
            _seen_media = set()

            def _push(item):
                """NEVER THE SAME FILE TWICE, and NEVER A CLIP THAT BELONGS TO ANOTHER DOT. The twin
                substitution ate its own tail: a blocked post is served with a playable channel's copy
                of the footage — and that playable post ALSO matches the story on its own, so the
                identical file went out twice. It can also graft a clip OWNED by a different story onto
                this one. Key on the media itself; honour the feed-wide owner map for both cases."""
                raw = item.get("video") or item.get("photo") or item.get("thumb")
                key = _media_id(raw)
                if not key or key in _seen_media:
                    return
                own = _CLIP_OWNER.get(key)
                if own and own != title:            # this footage is another dot's — leave it there
                    return
                _seen_media.add(key)
                out.append(item)

            _all = _tg_all_posts()
            for p in _all:
                # a "too big" video has NEITHER photo nor video — only a poster. It used to be
                # dropped here, so the biggest clips on the wire were the ones we never showed.
                if not (p.get("photo") or p.get("video") or p.get("thumb")):
                    continue
                _txt = p.get("text") or ""
                if not _clip_matches(title, _txt):
                    continue
                # ONE CLIP, ONE STORY — enforced in _push (covers direct, twin and album media), so an
                # unassigned clip still shows and never blanks the strip.
                base = {
                    "channel": p.get("title") or p.get("channel") or "Telegram",
                    "time": p.get("time") or "", "ts": p.get("ts") or 0,
                    "link": p.get("link") or "",
                }
                if p.get("video"):
                    _push(dict(base, text=_txt[:900], photo="", video=p["video"],
                               thumb=p.get("thumb") or "", dur=p.get("dur") or "", big=False))
                elif p.get("big"):
                    # Telegram will not serve OUR copy — but another channel may have posted the very
                    # same footage in a size it will serve. Play theirs, in-app, and credit them.
                    twin = self._playable_twin(p, _all)
                    if twin:
                        _push(dict(base, text=_txt[:900], photo="", video=twin["video"],
                                   thumb=(p.get("thumb") or twin.get("thumb") or ""),
                                   dur=p.get("dur") or twin.get("dur") or "", big=False,
                                   channel=twin.get("title") or twin.get("channel") or base["channel"],
                                   link=twin.get("link") or base["link"]))
                    else:
                        # No playable copy anywhere. Show it ONLY if the EXACT footage is on YouTube
                        # (embeddable, works for every user) — otherwise drop the clip entirely: no still
                        # frame, no wrong video. "Exact clip or nothing."
                        yt = _exact_youtube(_txt)
                        if yt and yt.get("id"):
                            _push(dict(base, text=_txt[:900], photo="", video="",
                                       thumb=p.get("thumb") or "", dur=p.get("dur") or "", big=False,
                                       youtube=yt["id"], yt_title=yt.get("title") or "",
                                       yt_channel=yt.get("channel") or ""))
                # AN ALBUM IS ALL OF ITS PHOTOS. A NOELREPORTS post of a struck logistics hub carried
                # FOUR pictures of burnt-out trucks; only the first was ever surfaced. The caption
                # goes on the first frame only, so the same paragraph is not repeated under each.
                for k, ph in enumerate(p.get("photos") or ([p["photo"]] if p.get("photo") else [])):
                    _push(dict(base, text=(_txt[:900] if (k == 0 and not p.get("video")) else ""),
                               photo=ph, video="", thumb="", dur="", big=False))
            out.sort(key=lambda x: x["ts"], reverse=True)
            res = {"items": out[:int(limit or 6)]}
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"error": str(ex), "items": []}

    def feed_channels(self):
        """The list of channels currently powering the live wire (so the UI can show/manage them)."""
        return {"channels": _tg_channels(), "file": _TG_FILE}

    def place_photo(self, place, country=""):
        """A picture OF THE PLACE — the city, the sea, the region, the country. Used only after a real
        photo of the event has failed, and always shown behind a "FILE PHOTO" chip so it can never be
        mistaken for evidence of what happened.

        This runs in the BACKEND on purpose. The browser's own fetch to Wikipedia comes back empty
        inside the webview, which is why the client-side version of this silently did nothing and
        every photoless card stayed a bare colour block — while the PEOPLE photos, which go through
        Python, worked fine all along."""
        place = (place or "").strip()
        country = (country or "").strip()
        key = _slug(place + "|" + country) or "none"
        cache = os.path.join(CACHE_DIR, "placepic_" + key + ".json")
        if _fresh(cache, 30 * 86400):
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass

        # most specific first, then widen: "Odesa (port, unspecified)" -> Odesa -> Odesa Oblast -> Ukraine
        qs = []
        # strip the parenthetical BEFORE splitting on commas — "Odesa (port, unspecified)" contains a
        # comma INSIDE the brackets, so splitting first left the query as the literal "Odesa (port".
        clean = re.sub(r"\s*\([^)]*\)", "", place).strip()
        head = clean.split(",")[0].strip()
        if head:
            qs.append(head)
            base = re.sub(r"\s+(oil refinery|refinery|oil depot|depot|air ?base|airbase|airport|"
                          r"nuclear power plant|npp|terminal|port|shipyard|complex)$", "", head, flags=re.I).strip()
            if base and base != head:
                qs.append(base)
        qs.extend([p.strip() for p in clean.split(",")[1:] if p.strip()])
        if country:
            qs.append(_co_short(country))
        seen, out = set(), None
        for q in qs:
            if not q or q.lower() in seen:
                continue
            seen.add(q.lower())
            j = _wiki_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                           + urllib.parse.quote(q.replace(" ", "_")))
            if not j or j.get("type") == "disambiguation":
                continue
            src = ((j.get("originalimage") or {}).get("source")
                   or (j.get("thumbnail") or {}).get("source") or "")
            if src:
                out = {"url": re.sub(r"/\d+px-([^/]*)$", r"/1280px-\1", src),
                       "title": j.get("title") or q}
                break
        res = out or {}
        try:
            json.dump(res, open(cache, "w", encoding="utf-8"))
        except Exception:
            pass
        return res

    def story_photo(self, title, desc="", place="", country=""):
        """The picture for a story that shipped without one — chosen by WHAT THE STORY IS ABOUT.

        If the dot is somewhere SPECIFIC (a refinery, a city, a sea), that place IS the story, so show
        it. If the dot is only a whole country, the country is scenery — the story is about a PERSON
        ("Iran says ELON MUSK's Starlink is a legitimate target") or an ORGANISATION, so show them.
        Falls through to the country, and finally the client draws a map of the exact spot.
        """
        try:
            place, country = (place or "").strip(), (country or "").strip()
            short = _co_short(country) if country else ""
            generic = (not place) or place in (short, country)

            def _person():
                p = _hero_person(title or "", desc or "")
                return dict(p, kind="person") if p else None

            def _place():
                if generic:
                    return None
                p = self.place_photo(place, "")          # the SPECIFIC place only, never the country
                return dict(p, kind="place") if p.get("url") else None

            def _org():
                oc = _org_country(title or "")
                if not oc:
                    return None
                low = " " + re.sub(r"[^a-z ]", " ", _fold(title or "").lower()) + " "
                for k in _ORG_KEYS:
                    if (" " + k + " ") in low and len(k) > 3:
                        p = self.place_photo(k.title(), "")
                        if p.get("url"):
                            return dict(p, kind="org")
                        break
                return None

            def _country():
                if not country:
                    return None
                p = self.place_photo(short or country, "")
                return dict(p, kind="country") if p.get("url") else None

            # A named FACILITY (a refinery, an airbase, a strait) IS the story — it beats even the
            # person who announced it. Anything less specific does not: a PERSON beats a mere city,
            # because "Greta Thunberg joins a Berlin protest" is a picture of Thunberg, not of Berlin.
            head = re.sub(r"\s*\([^)]*\)", "", place).split(",")[0].strip().lower()
            r = _resolve(head, []) if head else None
            is_facility = bool(r and r[0] == "city" and r[5] >= _FACILITY_PRIOR)

            if is_facility:
                order = [_place, _person, _org, _country]
            elif not generic:
                order = [_person, _place, _org, _country]
            else:
                order = [_person, _org, _country]
            for step in order:
                hit = step()
                if hit:
                    return hit
            return {}
        except Exception as ex:
            return {"error": str(ex)}

    def event_people(self, title, desc=""):
        """Faces for the people this story names, so the reader knows WHO is being talked about.
        Returns [] far more often than not, and that is the design — see _person_card(): a photo
        must be an exact Wikipedia title match for a Wikidata human who holds public office.
        The gating lives HERE, not in the UI, so phone clients get the same answer."""
        try:
            return {"people": _story_people(title or "", desc or "")}
        except Exception as ex:
            return {"error": str(ex), "people": []}

    def cached_analysis(self, country):
        """Return a previously-generated brief from disk WITHOUT calling the API (free, instant)."""
        cache = os.path.join(CACHE_DIR, "an_" + _slug(country) + ".json")
        if os.path.exists(cache):
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass
        return {}

    def generate_analysis(self, country, fb_hint=""):
        """PhD-level country brief via Gemini, cached to disk. Returns {lens,sections,actors} or {error}."""
        key = load_gemini_key()
        if not key:
            return {"error": "no_key"}
        cache = os.path.join(CACHE_DIR, "an_" + _slug(country) + ".json")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 30 * 86400:
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass
        try:
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   + GEMINI_MODEL + ":generateContent?key=" + urllib.parse.quote(key))
            body = json.dumps({
                "contents": [{"parts": [{"text": build_prompt(country, fb_hint)}]}],
                "generationConfig": {"temperature": 0.55, "maxOutputTokens": 2200, "responseMimeType": "application/json"},
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=70) as r:
                j = json.loads(r.read().decode("utf-8"))
            text = j["candidates"][0]["content"]["parts"][0]["text"]
            text = text.strip()
            if text.startswith("```"):
                text = re.sub(r"^```[a-z]*\s*", "", text)
                text = re.sub(r"\s*```$", "", text)
            data = json.loads(text)
            data["_model"] = GEMINI_MODEL
            try:
                json.dump(data, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return data
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode("utf-8")
            except Exception:
                detail = str(ex)
            if getattr(ex, "code", None) == 429:
                return {"error": "quota", "detail": detail[:400]}
            return {"error": "http", "code": getattr(ex, "code", 0), "detail": detail[:400]}
        except Exception as ex:
            return {"error": str(ex)}

    def country_news(self, country):
        """Last-24h news for a country from Google News RSS (free, cached 20 min)."""
        try:
            cache = os.path.join(CACHE_DIR, "news_" + _slug(country) + ".json")
            if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 1200:
                return json.load(open(cache, encoding="utf-8"))
            res = _news_get(country)
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"error": str(ex)}

    def leader_posts(self, accounts):
        """VERBATIM posts from officials' OWN accounts (Telegram / Truth Social). No AI, no key.
        accounts = [{name,title,type,handle}]; returns {"accounts":[{name,title,platform,handle,posts:[{text,url,when}]}]}."""
        try:
            accounts = accounts or []
            key = _slug("|".join((a.get("type", "") + ":" + a.get("handle", "")) for a in accounts))[:90]
            cache = os.path.join(CACHE_DIR, "posts_" + key + ".json")
            if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 900:
                return json.load(open(cache, encoding="utf-8"))
            out = []
            for a in accounts[:6]:
                typ = (a.get("type") or "").lower()
                h = (a.get("handle") or "").strip()
                if not h:
                    continue
                posts = []
                try:
                    if typ == "truth":
                        posts = _truth_posts(h)
                    elif typ == "telegram":
                        posts = _telegram_posts(h)
                except Exception:
                    posts = []
                if posts:
                    out.append({"name": a.get("name", ""), "title": a.get("title", ""),
                                "platform": ("Truth Social" if typ == "truth" else "Telegram"),
                                "handle": h.lstrip("@"), "posts": posts[:3]})
            res = {"accounts": out}
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"error": str(ex)}

    def official_releases(self, country):
        """VERBATIM official statements/releases from the government's own feed (curated majors). No AI."""
        info = CURATED_FEEDS.get(country)
        if not info:
            return {"items": [], "curated": False}
        source, feeds = info
        try:
            cache = os.path.join(CACHE_DIR, "rel_" + _slug(country) + ".json")
            if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 1200:
                return json.load(open(cache, encoding="utf-8"))
            items = []
            for u in feeds:
                try:
                    items += _feed_items(u)
                except Exception:
                    pass
            res = {"items": items[:6], "curated": True, "source": source}
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"items": [], "curated": True, "source": source, "error": str(ex)}

    def people_news(self, names, tag=""):
        """NON-AI 'what officials are saying': recent (48h) news per named official, from Google News,
        filtered so the OFFICIAL is the speaker (not third parties). {"people":[{name, items:[...]}]}. Cached 20 min."""
        try:
            key = _slug(tag or "|".join(names or []))
            cache = os.path.join(CACHE_DIR, "ppl_" + key + ".json")
            if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 1200:
                return json.load(open(cache, encoding="utf-8"))
            people = []
            for nm in (names or [])[:5]:
                nm = (nm or "").strip()
                if not nm:
                    continue
                try:
                    raw = _news_get_q('"' + nm + '" when:2d', limit=12).get("items", [])
                except Exception:
                    raw = []
                for it in raw:
                    sp, q = _analyze_headline(it.get("title", ""), nm)
                    it["speaker"] = sp
                    it["quote"] = q
                # order: the official actually quoted/speaking first, then other recent coverage
                raw.sort(key=lambda it: (2 if it["quote"] and it["speaker"] else 1 if it["speaker"] else 0), reverse=True)
                people.append({"name": nm, "items": raw[:3], "any": len(raw)})
            res = {"people": people}
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return res
        except Exception as ex:
            return {"error": str(ex)}

    def cached_statements(self, country):
        """Return recently-fetched statements from disk WITHOUT calling the API."""
        cache = os.path.join(CACHE_DIR, "stmt_" + _slug(country) + ".json")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3 * 3600:
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass
        return {}

    def official_statements(self, country):
        """Recent (24-48h) official statements via Gemini + Google Search grounding, cached 3h."""
        key = load_gemini_key()
        if not key:
            return {"error": "no_key"}
        cache = os.path.join(CACHE_DIR, "stmt_" + _slug(country) + ".json")
        if os.path.exists(cache) and time.time() - os.path.getmtime(cache) < 3 * 3600:
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass
        try:
            url = ("https://generativelanguage.googleapis.com/v1beta/models/"
                   + GEMINI_MODEL + ":generateContent?key=" + urllib.parse.quote(key))
            body = json.dumps({
                "contents": [{"parts": [{"text": _stmt_prompt(country)}]}],
                "tools": [{"google_search": {}}],
                "generationConfig": {"temperature": 0.3, "maxOutputTokens": 2200},
            }).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=75) as r:
                j = json.loads(r.read().decode("utf-8"))
            text = j["candidates"][0]["content"]["parts"][0]["text"].strip()
            m = re.search(r"\{.*\}", text, re.S)
            if m:
                text = m.group(0)
            data = json.loads(text)
            try:
                json.dump(data, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return data
        except urllib.error.HTTPError as ex:
            detail = ""
            try:
                detail = ex.read().decode("utf-8")
            except Exception:
                detail = str(ex)
            if getattr(ex, "code", None) == 429:
                return {"error": "quota", "detail": detail[:300]}
            return {"error": "http", "code": getattr(ex, "code", 0), "detail": detail[:300]}
        except Exception as ex:
            return {"error": str(ex)}

    def world_events(self, hours=24):
        """Real, geolocated world news for the map — GDELT DOC 2.0 (free, no key). Cached 15 min.
        Returns {"events":[{title,cat,lat,lng,place,country,hrs,source,domain,url,image}], "generated":ts}."""
        try:
            h = int(hours)
        except Exception:
            h = 24
        if h not in (6, 12, 24, 48):
            h = 24
        cache = os.path.join(CACHE_DIR, "world_%dh.json" % h)
        # STALE-WHILE-REVALIDATE: serve ANY cached copy INSTANTLY so the map fills the moment the app opens
        # (the cache is a file on disk, so it survives relaunches). If the copy is stale, kick off a
        # background refresh so the next poll is fresh. Only a first-EVER cold start (no cache at all) has
        # to wait for the live fetch — and the UI covers that with a splash.
        cached = None
        if os.path.exists(cache):
            try:
                cached = json.load(open(cache, encoding="utf-8"))
            except Exception:
                cached = None
        if cached:
            # restore the clip->owner map too, or the feed would serve with an EMPTY owner map and the
            # same clip would reappear under several dots until the next rebuild.
            if isinstance(cached.get("clip_owner"), dict):
                global _CLIP_OWNER
                _CLIP_OWNER = cached["clip_owner"]
            if not _fresh(cache, 900):
                _spawn_world_refresh(self, h)
                cached = dict(cached)
                cached["stale"] = True
            return cached
        return self._build_world_events(h)

    def _build_world_events(self, h):
        """The live build — GDELT + feeds + Telegram, geolocated and deduped, written to the cache. Blocks;
        run synchronously only on a cold start, and in a background thread by stale-while-revalidate."""
        cache = os.path.join(CACHE_DIR, "world_%dh.json" % h)
        span = "%dh" % h
        # fetch GDELT and the RSS feeds in parallel, then merge (dedup handles overlap)
        arts = []
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as _ex:
            _fg = _ex.submit(_gdelt_doc, COMBINED_QUERY, span, 250)
            _ff = _ex.submit(_collect_feeds)
            try:
                arts += _fg.result(timeout=12) or []
            except Exception:
                pass
            try:
                arts += _ff.result(timeout=16) or []
            except Exception:
                pass
        # OSINT Telegram channels — fast, on-the-ground; any post that names a place becomes a dot
        try:
            arts += _tg_arts(h)
        except Exception:
            pass
        events, seen_urls, seen_titles, added_sigs = [], set(), set(), []
        # Freshest first, so the per-category caps keep the NEWEST stories. Previously the caps were
        # first-come-first-served, and a 1h-old strike on the Tver oil depot was silently dropped
        # because 18 older security stories happened to be processed before it.
        arts.sort(key=lambda a: (a.get("hrs") if a.get("hrs") is not None else _seendate_hours(a.get("seendate") or "")))
        per_cat, per_country = {}, {}
        for a in arts:
            url = (a.get("url") or "").strip()
            title = _clean_headline(a.get("title") or "")
            if not url or len(title) < 12 or url in seen_urls:
                continue
            if _is_fluff(title, url):
                continue
            norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:55]
            if norm in seen_titles:
                continue
            hrs = a.get("hrs")
            if hrs is None:
                hrs = _seendate_hours(a.get("seendate") or "")
            if hrs > h:                    # dots strictly expire after the 24h window
                continue
            loc = _geolocate(a.get("geo_text") or title, a.get("sourcecountry") or "", a.get("desc") or "", url)
            if not loc:
                continue
            lat, lng, place, country = loc
            cat = _classify(title, a.get("desc") or "")
            # The SECTION is the definitive word on whether a story is sport. Keyword scoring will
            # never catch "Folarin Balogun: Ban reversal caused a lot of outside noise" or "spectre of
            # Maradona looms over Argentina" — but /football/ says it outright, and once it is filed
            # as sport the existing filter drops everything that is not a real result.
            if "/football/" in url or "/sport/" in url or "/sports/" in url:
                cat = "sports"
            if cat == "sports" and not _sports_worthy(title):
                continue
            # Dedup on DISTINCTIVE words only. Comparing raw sigwords merged genuinely different
            # events: every Russia+security story shares {drone, strike, oil, refinery...}, so a
            # fresh strike on the Tver oil depot was thrown away as a "duplicate" of an unrelated
            # strike on Omsk. _GENERIC_WORDS existed for this and was simply never wired in.
            _sig = _sigwords(title)
            _key = _sig - _GENERIC_WORDS
            _toks = _norm_tokens(title)                                # richer set for the similarity meter
            _dup = False
            for _co2, _cat2, _pl2, _key2, _toks2, _hrs2 in added_sigs:
                _inter = len(_key & _key2)
                if _inter >= 4:                                        # near-identical wording
                    _dup = True
                    break
                if _co2 == country and _inter >= 3:                    # same country, strongly alike
                    _dup = True
                    break
                if _pl2 == place and _cat2 == cat and _inter >= 2:     # SAME PLACE, same kind of event
                    _dup = True
                    break
                # SIMILARITY METER: the same story from another source/channel — one copy may carry an
                # extra prefix ("President Trump via Truth Social:"), be re-headlined, or land in a
                # different category. Near-total token overlap, same location, close in time = duplicate.
                if (_co2 == country or _pl2 == place) and abs(hrs - _hrs2) <= 12 and _same_story(_toks, _toks2):
                    _dup = True
                    break
            if _dup:
                continue
            # RULE 4: never drop a story. These caps DID drop real news — the per-country cap of 7
            # silently binned the 8th Russia story of the day, and a full-scale war produces far more
            # than seven. Dedup (above) is what protects the map from repetition; a cap is a blunt
            # instrument that throws away events we correctly fetched, classified and placed.
            # They are kept only as a runaway guard, set high enough that they should never bite.
            _cap = 5 if cat == "sports" else (70 if cat == "security" else 45)
            if per_cat.get(cat, 0) >= _cap or per_country.get(country, 0) >= 30:
                continue
            img = a.get("socialimage") or ""
            _is_tg = bool(a.get("_tg"))
            events.append({
                "title": title, "cat": cat,
                "lat": round(lat, 4), "lng": round(lng, 4),
                "place": place, "country": country,
                "hrs": round(hrs, 1),
                "source": (a.get("_src") or _domain_name(a.get("domain") or "")),
                "domain": ("t.me" if _is_tg else (a.get("domain") or "")),
                "url": url,
                "image": img if (_is_tg or _good_img(img)) else "",
                "sum": _strip_promo(a.get("desc") or "")[:360],
                "involved": (_involved_countries(title, country) or [country]),
                "channel": (a.get("_src") or "") if _is_tg else "",
                "tg": _is_tg,
            })
            seen_urls.add(url)
            seen_titles.add(norm)
            added_sigs.append((country, cat, place, _key, _toks, hrs))
            per_cat[cat] = per_cat.get(cat, 0) + 1
            per_country[country] = per_country.get(country, 0) + 1
        # picture-bearing + most-recent first, then cap
        events.sort(key=lambda e: (0 if e["image"] else 1, e["hrs"]))
        events = _collapse_colocated(events)   # one dot per place — merge a co-located barrage
        events = events[:260]
        try:
            _assign_clips(events, _tg_all_posts())   # each clip belongs to ONE dot, feed-wide
        except Exception:
            pass
        _spread(events)   # fan out dots that share a location
        res = {"events": events, "generated": int(time.time()), "clip_owner": _CLIP_OWNER}
        if events:  # never cache an empty/failed result — let it retry next time
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
        return res

    def country_news(self, country, hours=24):
        """ALL recent news for ONE country — the "starred country" feed. A user from Latvia stars Latvia
        and its everyday news lights up the map, even the minor stories world_events would never surface.
        So this deliberately RELAXES the importance filters: no per-category caps, no sports-worthiness
        gate — but it still drops ads/galleries (_is_fluff) and only keeps stories that actually land IN
        the starred country. Sourced from that country's own English-language outlets (GDELT
        sourcecountry:). Cached ~15 min per country."""
        try:
            country = (country or "").strip()
            fips = _fips_for(country)
            if not fips:
                return {"events": [], "country": country, "unsupported": True}
            try:
                h = int(hours)
            except Exception:
                h = 24
            if h not in (6, 12, 24, 48):
                h = 24
            cache = os.path.join(CACHE_DIR, "starred_%s_%dh.json" % (_slug(country)[:24], h))
            if _fresh(cache, 900):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            try:
                arts = _gdelt_doc("sourcecountry:%s sourcelang:eng" % fips, "%dh" % h, 120) or []
            except Exception:
                arts = []
            events, seen_urls, seen_titles, added_toks = [], set(), set(), []
            arts.sort(key=lambda a: _seendate_hours(a.get("seendate") or ""))     # freshest first
            for a in arts:
                url = (a.get("url") or "").strip()
                title = _clean_headline(a.get("title") or "")
                if not url or len(title) < 12 or url in seen_urls:
                    continue
                if _is_fluff(title, url):
                    continue
                norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:55]
                if norm in seen_titles:
                    continue
                hrs = _seendate_hours(a.get("seendate") or "")
                if hrs > h:
                    continue
                loc = _geolocate(a.get("geo_text") or title, a.get("sourcecountry") or country,
                                 a.get("desc") or "", url)
                if not loc:
                    continue
                lat, lng, place, ev_country = loc
                if not _country_match(ev_country, country):        # keep only news that lands IN this country
                    continue
                _toks = _norm_tokens(title)
                if any(_same_story(_toks, t2) for t2 in added_toks):   # same story from two outlets
                    continue
                cat = _classify(title, a.get("desc") or "")            # every category kept — even "bland"
                img = a.get("socialimage") or ""
                events.append({
                    "title": title, "cat": cat,
                    "lat": round(lat, 4), "lng": round(lng, 4),
                    "place": place, "country": ev_country, "hrs": round(hrs, 1),
                    "source": _domain_name(a.get("domain") or ""),
                    "domain": a.get("domain") or "", "url": url,
                    "image": img if _good_img(img) else "",
                    "sum": _strip_promo(a.get("desc") or "")[:360],
                    "involved": (_involved_countries(title, ev_country) or [ev_country]),
                    "starred": True, "tg": False, "channel": "",
                })
                seen_urls.add(url)
                seen_titles.add(norm)
                added_toks.append(_toks)
                if len(events) >= 45:
                    break
            _spread(events)   # fan out dots that share the country centroid so they don't stack into one
            out = {"events": events, "country": country, "generated": int(time.time())}
            if events:
                try:
                    json.dump(out, open(cache, "w", encoding="utf-8"))
                except Exception:
                    pass
            return out
        except Exception as ex:
            return {"events": [], "country": country, "error": str(ex)}

    def app_version(self):
        return {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)), "repo": _update_repo()}

    def check_update(self):
        """Ask GitHub Releases whether a newer Meridian.exe exists. Returns {available, version, url, notes}.
        No-ops (available:False) unless running as the packaged .exe with a repo configured. Cached 6h."""
        try:
            repo = _update_repo()
            if not getattr(sys, "frozen", False) or not repo:
                return {"available": False, "reason": "disabled"}
            cache = os.path.join(CACHE_DIR, "update_check.json")
            if _fresh(cache, 6 * 3600):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            api = "https://api.github.com/repos/%s/releases/latest" % repo
            req = urllib.request.Request(api, headers={"User-Agent": "Meridian",
                                                       "Accept": "application/vnd.github+json"})
            j = json.loads(urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace"))
            tag = j.get("tag_name") or j.get("name") or ""
            url = ""
            for a in (j.get("assets") or []):
                if (a.get("name") or "").lower().endswith(".exe"):
                    url = a.get("browser_download_url") or ""
                    break
            out = {"available": bool(url) and _is_newer(tag, APP_VERSION),
                   "version": re.sub(r"^v", "", tag, flags=re.I), "url": url,
                   "notes": (j.get("body") or "")[:2000]}
            try:
                json.dump(out, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return out
        except Exception as ex:
            return {"available": False, "error": str(ex)}

    def apply_update(self, url):
        """Download the new Meridian.exe and swap it in. A running .exe can't overwrite itself, so we hand
        off to a tiny batch that waits for us to quit, replaces the file, relaunches, and self-deletes."""
        try:
            if not getattr(sys, "frozen", False):
                return {"ok": False, "error": "Updates only apply to the installed app."}
            if not (url and url.startswith("https://") and "github" in url):
                return {"ok": False, "error": "bad url"}
            target = sys.executable                       # the installed Meridian.exe (what's running now)
            upd_dir = os.path.join(DATA_DIR, "update")
            os.makedirs(upd_dir, exist_ok=True)
            newexe = os.path.join(upd_dir, "Meridian-new.exe")
            req = urllib.request.Request(url, headers={"User-Agent": "Meridian"})
            with urllib.request.urlopen(req, timeout=180) as r, open(newexe, "wb") as f:
                shutil.copyfileobj(r, f)
            if os.path.getsize(newexe) < 1_000_000:       # a real build is tens of MB — reject a bad download
                return {"ok": False, "error": "download looks incomplete"}
            bat = os.path.join(upd_dir, "swap.bat")
            with open(bat, "w", encoding="utf-8") as f:
                f.write(
                    "@echo off\r\n"
                    "ping 127.0.0.1 -n 2 >nul\r\n"
                    ":wait\r\n"
                    'tasklist /fi "imagename eq Meridian.exe" | find /i "Meridian.exe" >nul '
                    "&& (ping 127.0.0.1 -n 2 >nul & goto wait)\r\n"
                    'copy /y "' + newexe + '" "' + target + '" >nul\r\n'
                    'del "' + newexe + '" >nul 2>&1\r\n'
                    'start "" "' + target + '"\r\n'
                    'del "%~f0" >nul 2>&1\r\n'
                )
            DETACHED = 0x00000008 | 0x00000200            # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(["cmd", "/c", bat], creationflags=DETACHED, close_fds=True)

            def _bye():
                time.sleep(0.7)                           # let the response reach the UI first
                try:
                    webview.windows[0].destroy()
                except Exception:
                    os._exit(0)
            threading.Thread(target=_bye, daemon=True).start()
            return {"ok": True}
        except Exception as ex:
            return {"ok": False, "error": str(ex)}

    def country_grade(self, country):
        """Authoritative regime grading (V-Dem) for a country, or {} if unlisted.
        {regime, camp, regime_n, edi, year, source, as_of}."""
        g = _grade_for(country)
        if not g:
            return {}
        return {"regime": g.get("regime"), "camp": g.get("camp"), "regime_n": g.get("regime_n"),
                "edi": g.get("edi"), "year": g.get("year"),
                "source": _GRADES.get("source", ""), "as_of": _GRADES.get("generated", "")}

    def country_leaders(self, qid, country="", fb=None):
        """Current head of state + head of government — read from Wikidata's reliable entity API and
        cross-checked against the Factbook names in `fb` ({hos:{name,title}, hog:{name,title}}). Cached
        ~daily per country. Returns {"leaders":[{name,title,role,img,x,telegram,truth}]}."""
        try:
            qid = (qid or "").strip()
            if not re.match(r"^Q\d+$", qid):
                return {"leaders": []}
            fb = fb or {}
            fb_cos_name, fb_cos_title = _fb_parse(fb.get("cos", ""))   # Factbook chief of state (raw text)
            fb_hog_name, fb_hog_title = _fb_parse(fb.get("hog", ""))   # Factbook head of government (raw text)
            cache = os.path.join(CACHE_DIR, "leaders_" + qid + ".json")
            if _fresh(cache, 20 * 3600):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            ent = _wd_entities(qid, "claims").get(qid, {})
            roles, offices = {"P35": [], "P6": []}, {}
            for prop, off in (("P35", "P1906"), ("P6", "P1313")):
                for c in ent.get("claims", {}).get(prop, []):
                    if c.get("rank") == "deprecated":
                        continue
                    pqid = _wd_claim_qid(c)
                    if pqid:
                        roles[prop].append({"qid": pqid, "ended": "P582" in c.get("qualifiers", {}),
                                            "start": _wd_qual_time(c, "P580"),
                                            "preferred": c.get("rank") == "preferred"})
                oc = ent.get("claims", {}).get(off, [])
                if oc and _wd_claim_qid(oc[0]):
                    offices[prop] = _wd_claim_qid(oc[0])
            # one batch fetch for every person + office label
            ids = list({c["qid"] for r in roles.values() for c in r}) + list(offices.values())
            pents = {}
            for i in range(0, len(ids), 45):
                chunk = ids[i:i + 45]
                if chunk:
                    pents.update(_wd_entities("|".join(chunk), "labels|claims"))

            def _lbl(q):
                return ((pents.get(q, {}).get("labels", {}) or {}).get("en", {}) or {}).get("value", "")

            def _social(e, pid):
                cl = e.get("claims", {}).get(pid, [])
                if cl:
                    vv = cl[0].get("mainsnak", {}).get("datavalue", {}).get("value", "")
                    return vv if isinstance(vv, str) else ""
                return ""
            for r in roles.values():
                for c in r:
                    e = pents.get(c["qid"], {})
                    c["name"] = _lbl(c["qid"])
                    c["dead"] = bool(e.get("claims", {}).get("P570"))   # date of death -> never current
                    c["img"] = ""
                    p18 = e.get("claims", {}).get("P18", [])
                    if p18:
                        fn = p18[0].get("mainsnak", {}).get("datavalue", {}).get("value", "")
                        if fn:
                            c["img"] = ("https://commons.wikimedia.org/wiki/Special:FilePath/"
                                        + urllib.parse.quote(fn.replace(" ", "_")) + "?width=240")
                    c["x"], c["tg"], c["truth"] = _social(e, "P2002"), _social(e, "P3789"), _social(e, "P10858")
            hos = _pick_leader(roles["P35"], fb_cos_name)
            hog = _pick_leader(roles["P6"], fb_hog_name)
            # SUPPLEMENT: Wikidata often lacks a DISTINCT head of government (e.g. Saudi Arabia's PM, MBS —
            # its P6 still points to the King). If Wikidata gives none, or the same person as the head of
            # state, but the Factbook names a different head of government, resolve that person via Wikidata
            # search (name -> entity + photo) and use the Factbook's title.
            hog_forced_title = ""
            _hos_qid = hos.get("qid") if hos else None
            # When Wikidata gives no head of government, or the SAME person as the head of state (MBS and
            # King Salman share a family name, so compare by QID — not fuzzy names), resolve the Factbook's
            # head of government via search and add them if they're genuinely a different person.
            if fb_hog_name and (not hog or (_hos_qid and hog.get("qid") == _hos_qid)):
                p = _wd_search_person(fb_hog_name) or _wd_search_person(" ".join(fb_hog_name.split()[:3]))
                if p and p["qid"] != _hos_qid:
                    hog = {"qid": p["qid"], "name": p["name"], "img": p["img"], "x": "", "tg": "", "truth": ""}
                    hog_forced_title = fb_hog_title
            # ALWAYS return CLEAN names. If Wikidata was rate-limited/incomplete and left a role unresolved,
            # use the cleanly-parsed Factbook name (the client fetches its photo from Wikipedia). This is what
            # stops a slow fetch from EVER showing a blank or a garbled fallback again.
            if fb_cos_name and (not hos or not hos.get("name")):
                hos = {"qid": (hos or {}).get("qid"), "name": fb_cos_name, "img": (hos or {}).get("img", "")}
            if fb_hog_name and (not hog or not hog.get("name")) and not (
                    hos and _same_person(fb_hog_name, None, hos.get("name", ""), hos.get("qid"))):
                hog = {"qid": (hog or {}).get("qid"), "name": fb_hog_name, "img": (hog or {}).get("img", "")}
                hog_forced_title = hog_forced_title or fb_hog_title
            out = []

            def _emit(c, prop, role, default_title, fb_name="", fb_title="", forced_title=""):
                # prefer the Factbook's specific title when it names the same person (Wikidata's office label
                # calls Iran's head of state "President"; the Factbook correctly says "Supreme Leader").
                title = forced_title or _clean_office_title(_lbl(offices.get(prop, "")), default_title)
                if not forced_title and fb_title and _name_match(c["name"], fb_name):
                    title = fb_title
                out.append({"name": c["name"], "title": title, "role": role, "img": c.get("img", ""),
                            "x": c.get("x"), "telegram": c.get("tg"), "truth": c.get("truth")})
            if hos and hos.get("name"):
                _emit(hos, "P35", "Head of state", "President", fb_name=fb_cos_name, fb_title=fb_cos_title)
            if hog and hog.get("name") and not (
                    hos and _same_person(hog.get("name"), hog.get("qid"), hos.get("name"), hos.get("qid"))):
                _emit(hog, "P6", "Head of government", "Prime Minister",
                      fb_name=fb_hog_name, fb_title=fb_hog_title, forced_title=hog_forced_title)
            # governing lean = the party of whoever runs the government (head of gov, else head of state)
            ruling = hog if (hog and hog.get("qid")) else hos
            lean = _ruling_party_lean(ruling.get("qid"), pents.get(ruling.get("qid"))) if ruling else None
            res = {"leaders": out, "lean": lean, "generated": int(time.time())}
            if out:
                try:
                    json.dump(res, open(cache, "w", encoding="utf-8"))
                except Exception:
                    pass
            return res
        except Exception as ex:
            return {"leaders": [], "error": str(ex)}

    def article_detail(self, url):
        """One article's picture + clean text for the detail panel. Cached 1 day.
        Returns {title,desc,image,paragraphs:[...],published,site} or {error}."""
        try:
            if not url or not url.startswith("http"):
                return {}
            cache = os.path.join(CACHE_DIR, "art_" + _slug(url) + ".json")
            if _fresh(cache, 86400):
                try:
                    return json.load(open(cache, encoding="utf-8"))
                except Exception:
                    pass
            try:
                page = _http_get(url, 22)
            except Exception as ex:
                return {"error": str(ex)}
            data = _extract_article(page)
            try:
                json.dump(data, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return data
        except Exception as ex:
            return {"error": str(ex)}


def _news_get_q(query, limit=16):
    url = "https://news.google.com/rss/search?" + urllib.parse.urlencode(
        {"q": query, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    xml = urllib.request.urlopen(req, timeout=25).read().decode("utf-8", "replace")
    items = re.findall(r"<item>(.*?)</item>", xml, re.S)
    out = []
    for it in items[:limit]:
        def g(tag):
            m = re.search(r"<" + tag + r"[^>]*>(.*?)</" + tag + r">", it, re.S)
            return (m.group(1).replace("<![CDATA[", "").replace("]]>", "").strip() if m else "")
        title, link, pub = g("title"), g("link"), g("pubDate")
        sm = re.search(r"<source[^>]*>(.*?)</source>", it, re.S)
        source = (sm.group(1).replace("<![CDATA[", "").replace("]]>", "").strip() if sm else "")
        if source:
            title = re.sub(r"\s*[-–—|]\s*" + re.escape(source) + r"\s*$", "", title).strip()
        if title:
            out.append({"title": title, "source": source, "link": link, "pub": pub})
    return {"items": out}


def _news_get(country):
    return _news_get_q(country + " when:1d")


# Official primary sources that publish leaders'/governments' VERBATIM statements (RSS/Atom).
CURATED_FEEDS = {
    "Russia":                   ("Kremlin (Office of the President)", ["http://en.kremlin.ru/events/president/transcripts/feed"]),
    "United Kingdom":           ("10 Downing Street", ["https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=prime-ministers-office-10-downing-street"]),
    "United States of America": ("The White House", ["https://www.whitehouse.gov/presidential-actions/feed/"]),
}


def _http_get(url, timeout=20):
    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout).read().decode("utf-8", "replace")


def _clean_post(h):
    h = re.sub(r"<br\s*/?>", "\n", h)
    h = re.sub(r"</p>\s*<p[^>]*>", "\n", h)
    h = re.sub(r"<[^>]+>", " ", h)
    return re.sub(r"[ \t]+", " ", _htmlmod.unescape(h)).strip()


def _truth_posts(handle):
    handle = handle.lstrip("@")
    acc = json.loads(_http_get("https://truthsocial.com/api/v1/accounts/lookup?acct=" + urllib.parse.quote(handle), 18))
    aid = acc["id"]
    raw = json.loads(_http_get("https://truthsocial.com/api/v1/accounts/" + aid +
                               "/statuses?limit=10&exclude_replies=true&exclude_reblogs=true", 18))
    out = []
    for s in raw:
        txt = _clean_post(s.get("content", "") or "")
        if len(txt) >= 6:
            out.append({"text": txt, "url": s.get("url") or ("https://truthsocial.com/@" + handle), "when": s.get("created_at", "")})
    return out


def _telegram_posts(handle):
    handle = handle.lstrip("@")
    page = _http_get("https://t.me/s/" + urllib.parse.quote(handle), 18)
    blocks = re.split(r'<div class="tgme_widget_message ', page)[1:]
    out = []
    for b in blocks:
        pm = re.search(r'data-post="([^"]+)"', b)
        tm = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>\s*<div class="tgme_widget_message_footer', b, re.S) \
            or re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', b, re.S)
        if not tm:
            continue
        txt = _clean_post(tm.group(1))
        dm = re.search(r'datetime="([^"]+)"', b)
        if len(txt) >= 10:
            out.append({"text": txt, "url": ("https://t.me/" + pm.group(1)) if pm else ("https://t.me/" + handle),
                        "when": (dm.group(1) if dm else "")})
    return out[::-1]  # page lists oldest->newest; newest first


def _feed_items(url):
    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    x = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=22).read().decode("utf-8", "replace")
    blocks = re.findall(r"<item>(.*?)</item>", x, re.S) or re.findall(r"<entry>(.*?)</entry>", x, re.S)
    out = []
    for b in blocks[:10]:
        tm = re.search(r"<title[^>]*>(.*?)</title>", b, re.S)
        title = re.sub(r"<[^>]+>", "", (tm.group(1) if tm else "").replace("<![CDATA[", "").replace("]]>", "")).strip()
        lm = re.search(r'<link[^>]*href="([^"]+)"', b) or re.search(r"<link>(.*?)</link>", b, re.S)
        link = (lm.group(1).strip() if lm else "")
        dm = re.search(r"<pubDate>(.*?)</pubDate>", b, re.S) or re.search(r"<updated>(.*?)</updated>", b, re.S) or re.search(r"<published>(.*?)</published>", b, re.S)
        date = (dm.group(1).strip() if dm else "")
        if title:
            out.append({"title": title, "link": link, "date": date})
    return out


# unambiguous saying-verbs (avoid noun-homographs like claims/calls/reports/backs/orders/signs)
_SAY = (r"says?|said|tells?|told|warns?|warned|vows?|vowed|announces?|announced|declares?|declared|"
        r"pledges?|pledged|insists?|insisted|urges?|urged|threatens?|threatened|denies?|denied|"
        r"hails?|hailed|slams?|slammed|rejects?|rejected|blasts?|accuses?|accused|condemns?|condemned|"
        r"admits?|admitted|confirms?|confirmed|warns|vowed")
_THIRD = re.compile(r"\b(?:analysts?|experts?|sources?|officials|report|reports|reported|study|studies|poll|"
                    r"think[ -]?tank|researchers?|critics?|media|opposition|watchdog|survey|data|figures)\b\s*"
                    r"(?:" + _SAY + r")\b", re.I)
_ACCORDING = re.compile(r"\baccording to\b|,\s*(?:report|reports|study|analysts?|sources?)\b", re.I)
_QUOTE = re.compile(u"[“”\"]([^“”\"]{8,220})[“”\"]")  # real quotation marks only, never apostrophes


def _analyze_headline(title, name):
    """Return (is_official_the_speaker, embedded_quote_text)."""
    surname = (name.split() or [name])[-1]
    speaker = False
    # official's surname must sit right before a saying-verb (surname is the subject), not after it
    if re.search(r"\b" + re.escape(surname) + r"\b(?:['’]s)?\s+(?:[a-z]+\s+){0,3}(?:" + _SAY + r")\b", title, re.I) \
            and not _THIRD.search(title) and not _ACCORDING.search(title):
        speaker = True
    qm = _QUOTE.search(title)
    return speaker, (qm.group(1).strip() if qm else "")


def _stmt_prompt(country):
    return (
        "Using current web-search results, compile notable public statements, official remarks, "
        "social-media posts (e.g. Truth Social / X) or press communications from roughly the LAST 24-48 HOURS "
        "by senior government officials, heads of state or government, ministers, spokespeople, or state/military "
        "institutions of " + country + ". Only real, recent, verifiable items. For each give: who said it (exact "
        "name of the person OR institution), their title/role, a short direct quote or faithful paraphrase, the "
        "approximate date, and the source outlet. Return ONLY minified JSON, no markdown fences: "
        '{"statements":[{"name":"","title":"","quote":"","when":"","source":"","wiki":"exact English Wikipedia '
        'article title for a photo/logo of this person or institution"}]}. Up to 8 items, most recent first. If little '
        "happened in 24-48h, include the most recent available."
    )


# ============================================================
#  LIVE WORLD NEWS  —  GDELT DOC 2.0 (free, no key) + article scrape
#  Real headlines, real photos, real sources, geolocated by headline.
# ============================================================
GDELT_DOC = "https://api.gdeltproject.org/api/v2/doc/doc"

# ONE broad query (GDELT rate-limits hard) — categories are assigned in Python below
COMBINED_QUERY = (
    '(airstrike OR ceasefire OR clashes OR missile OR troops OR offensive OR militants '
    'OR election OR parliament OR president OR sanctions OR "prime minister" OR summit '
    'OR inflation OR "interest rate" OR tariff OR "stock market" OR recession OR economy '
    'OR earthquake OR flood OR wildfire OR hurricane OR drought OR eruption OR cyclone '
    'OR "artificial intelligence" OR semiconductor OR cyberattack OR satellite '
    'OR outbreak OR epidemic OR vaccine OR pandemic '
    'OR "world cup" OR championship OR olympics) sourcelang:eng'
)

# headline keyword -> category (checked in order; disasters/health beat generic terms)
# ---------------------------------------------------------------------------
# Category scoring.
# The old classifier was FIRST-MATCH-WINS over an ordered keyword list, so one incidental word decided
# everything: "Satellite imagery confirms tanks burned after the July 9 overnight STRIKE" hit tech's
# "satellite" and security was never even tested (bare "strike" wasn't in its list) -> a war story filed
# under Science & Tech.
# Now EVERY category is scored (strong signal = 3, supporting = 1), the highest score wins, and context
# masks remove words that mean something different in context: "satellite imagery" is how a war is
# REPORTED (not a tech story), and a "workers' strike" is not a military one.
# Locked down by test_meridian.py — run it after touching these lists.
# ---------------------------------------------------------------------------
CAT_ORDER = ("security", "climate", "health", "economy", "tech", "sports", "society", "politics")

CAT_STRONG = {
    "security": ("airstrike", "air strike", "air raid", "drone strike", "drone attack", "missile strike",
                 "missile attack", "rocket attack", "rocket strike", "overnight strike", "strike on",
                 "struck", "shelling", "shelled", "artillery", "bombard", "air defence", "air defense",
                 "ballistic missile", "cruise missile", "warplane", "fighter jet", "warship", "gunmen",
                 "gunman", "gunfire", "shot dead", "hostage", "war crime", "frontline", "front line",
                 "ground assault", "offensive", "ceasefire", "militant", "insurgent", "assassinat",
                 "invasion", "besieg", "terror", "paramilitary", "militia", "rebels", "troops",
                 "soldiers", "combat", "sabotage", "mercenar", "nuclear strike", "war planes",
                 # the OSINT firehose is terse: "Kh-22/32 impacts in Odesa", "SHAHED over Kharkiv".
                 # These scored ZERO and fell to politics — the exact posts that then stacked as
                 # duplicate dots. Weapon designations are UNAMBIGUOUS, so they carry the strong signal.
                 # (Matching is substring, so every entry here must be safe as one: no bare "kab" —
                 # it is inside "Kabul" — and the hyphen in "kh-22"/"s-300" makes those collision-proof.)
                 "shahed", "kalibr", "iskander", "kinzhal", "kh-22", "kh-31", "kh-32", "kh-101",
                 "s-300", "s-400", "atacms", "himars", "storm shadow", "loitering munition",
                 "glide bomb", "kab-250", "kab-500", "fab-500", "missile launch", "air raid alert"),
    "climate": ("earthquake", "quake", "wildfire", "flood", "hurricane", "typhoon", "cyclone", "drought",
                "volcano", "eruption", "landslide", "tsunami", "heatwave", "heat wave", "blizzard",
                "mudslide", "famine", "evacuat", "storm surge", "record heat", "storm damage",
                "tropical storm", "severe storm", "winter storm", "thunderstorm", "hailstorm"),
    "health": ("outbreak", "epidemic", "pandemic", "vaccine", "measles", "cholera", "ebola", "virus",
               "public health", "disease", "infection", "malaria", "polio"),
    "economy": ("inflation", "interest rate", "central bank", "tariff", "recession", "stock market",
                "unemployment", "trade deal", "budget deficit", "bankruptcy", "layoff", "earnings",
                "bond yield", "oil price", "workers strike", "strike action", "walkout", "pay dispute",
                "gdp", "gasoline", "petrol", "fuel price", "retail price", "per liter", "per litre",
                "price cap", "cost of living", "shortage"),
    "tech": ("artificial intelligence", "semiconductor", "chipmaker", "chip maker", "cyberattack",
             "data breach", "spacecraft", "spacex", "quantum", "openai", "nvidia", "software",
             "startup", "algorithm", "chatbot", "space station", "reusable rocket", "rocket launch",
             "satellite launch", "microchip", "in orbit"),
    "sports": ("world cup", "olympic", "championship", "grand slam", "grand prix", "playoff",
               "tournament", "gold medal", "the final", "league title", "trophy", "tour de france",
               "wimbledon", "premier league", "champions league", "formula 1", "super bowl", "fifa",
               "uefa", "ryder cup", "test match", "the ashes", "marathon", "peloton", "stage win",
               # A CLUB NAME is the one unambiguous signal that a story is sport. Without it,
               # "Leandro Trossard set for Besiktas move after Arsenal agree deal" scored zero and
               # fell to the POLITICS default — a transfer rumour sat on the map as a politics dot.
               # NB: matching is plain substring, so a club whose name is also a CITY or COUNTRY is
               # forbidden here — "roma" is inside "Romania", and Liverpool/Barcelona/Porto are
               # places that appear in real news. Only unambiguous club names may go on this list.
               "arsenal", "chelsea", "tottenham", "man united", "manchester united",
               "manchester city", "man city", "newcastle united", "aston villa", "west ham",
               "real madrid", "atletico", "bayern", "borussia", "juventus", "ac milan",
               "inter milan", "psg", "paris saint-germain", "ajax", "benfica",
               "celtic", "besiktas", "fenerbahce", "galatasaray"),
    "society": ("shooting", "murder", "stabbing", "manslaughter", "kidnap", "immigration", "refugee",
                "migrant", "asylum", "verdict", "convicted", "sentenced", "hate crime"),
    "politics": ("election", "parliament", "referendum", "impeachment", "coalition", "prime minister",
                 "summit", "sanction", "diplomat", "resign", "cabinet", "ballot", "inaugurat", "treaty",
                 "foreign minister", "ambassador", "nominat", "no-confidence", "protest",
                 "midterm", "primary election", "campaign trail"),
}
CAT_WEAK = {
    "security": ("strike", "attack", "raid", "bomb", "killed", "kills", "wounded", "injured", "casualt",
                 "blast", "explosion", "drone", "missile", "weapon", "military", "army", "fired",
                 "clash", "siege", " war ", "conflict",
                 # "Russian FORCES SHELL Toretsk overnight" scored ZERO for security and fell to the
                 # politics default — the most basic war verb we have was not on the list.
                 "shell", "shells", "shelled", "shelling", "troops", "offensive",
                 # A MILITARY ACTOR. SHIPPED BUG: "RUSSIAN FORCES SET FIRE to the Kherson Maritime
                 # Academy" was filed under CLIMATE — security scored ZERO ("forces" was not a word we
                 # knew) while climate scored 1 on "fire". An army burning a building is an attack.
                 "forces", "soldiers", "servicemen", "occupiers", "militants", "militia",
                 "brigade", "battalion", "paratroopers", "garrison", "infantry",
                 # ARSON IS DELIBERATE — an accidental fire is never "set". This is what separates the
                 # Kherson attack from "Bangkok pub fire" (a real climate/disaster story).
                 "set fire", "set ablaze", "torched", "arson attack",
                 "front line", "frontline", "captured", "seized", "artillery",
                 # aftermath / damage-assessment wording — a strike report often never says "strike"
                 "damage", "damaged", "destroyed", "ablaze", "wreckage", "debris", "rubble",
                 "shrapnel", "interception", "refinery", "depot", "distillation unit", "hit by",
                 # more OSINT-wire strike wording. SAFE substrings only — "on course for" is out (it
                 # matched "on course for EU membership"), as is bare "impact". Weapon/alert terms that
                 # do not collide with common words: a Geran/FPV/UAV story is a strike, an air alert is
                 # an incoming raid. Enough (+1) to lift a terse strike post off the politics default.
                 "incoming missile", "geran", "fpv drone", " uav ", "air alert", "drone activity"),
    "climate": ("storm", "climate", "emissions", "burned", "burning", "fires", "fire", "blaze",
                "engulfed", "temperature", "rainfall"),
    "health": ("health", "hospital", "patients", "medical", "doctors"),
    "economy": ("economy", "economic", "stocks", "markets", "currency", "jobs", "wages", "investment",
                "revenue", "profit", "trade"),
    "tech": (" ai ", "satellite", "robot", "digital", "online", "internet", "platform", "smartphone"),
    # A TRANSFER is sport. It used to score ZERO here and default to "politics", which meant the
    # sports filter (_sports_worthy: keep results, drop transfer chatter) never got to see it —
    # "Winger Trossard to join Besiktas from Arsenal" sat on the map as a POLITICS dot on Singapore.
    "sports": ("match", "goal", "goals", "semi-final", "semis", "striker", "scores", "scored",
               "penalty", "midfielder", "coach", "cyclist", "cycling", "tennis", "football",
               "soccer", "cricket", "rugby", "basketball", "athletics", "sprint", "stage",
               "winger", "defender", "goalkeeper", "forward line", "squad", "club", "transfer fee",
               "signing", "signs for", "on loan", "free agent", "dressing room", "manager",
               "fixture", "kick-off", "friendly", "derby", "players", "injury", "sacked", "caps",
               # whole SPORTS had no keywords: golf, rugby and a "semi" all defaulted to politics
               "golf", "pga", "birdie", "putt", "fairway", "skipper", "call up", "call-up",
               "semi", "quarter-final", "knockout", "dugout", "bowler", "batsman", "innings",
               "scrum", "try line", "six nations", "nba", "nfl", "mlb", "nhl", "boxing", "ufc"),
    "society": ("crime", "police", "arrest", "trial", "court"),
    "politics": ("government", "minister", "talks", "policy", "bill", "vote", "party", "president",
                 "senator", "congress", "lawmaker", "ruling"),
}
# text removed BEFORE a category is scored — these words mean something else in this context
CAT_MASK = {
    "tech": (r"satellite (imagery|image|images|photo|photos|picture|pictures|data)",
             r"(rocket|missile)s? (attack|strike|fire|barrage|launch(ed)? at)"),
    # a LABOUR strike is not a military one. The old pattern demanded the words be adjacent, so
    # "teachers SET TO strike" sailed through and filed a pay dispute under CONFLICT & SECURITY.
    "security": (r"(workers?|staff|union|general|nationwide|teachers?|doctors?|nurses?|rail|pilots?|"
                 r"drivers?|junior doctors?|civil servants?)\b[^.;]{0,32}\bstrikes?\b",
                 r"strike action", r"on strike", r"hunger strike", r"strike ballot", r"pay strike"),
    "climate": (r"(gun|artillery|rocket|missile)\s*fire", r"ceasefire", r"under fire", r"opened fire"),
}


_STOP = set("the a an of to in on for and or as at by with from into over under after before amid says said will has have had are was were is been new latest live update world news that this its his her their also more than what when where who how".split())
def _stem(w):
    if len(w) > 5 and w.endswith("ing"):
        return w[:-3]
    if len(w) > 5 and w.endswith("ed"):
        return w[:-2]
    if len(w) > 4 and w.endswith("es") and w[-3] in "shxz":   # launches->launch, clashes->clash, boxes->box
        return w[:-2]
    if len(w) > 4 and w.endswith("s") and not w.endswith("ss"):
        return w[:-1]
    return w
# Words too generic to prove two stories are about the same event ("state", "american", "president").
# Country names and demonyms are also weak on their own — plenty of stories mention the same country.
_COMMON_MATCH = set("""state states president government people world country countries news report reports
media official officials minister national international time year years today week month first last great
power tests test story stories experiment freedom opportunity leader leaders capital city public life
support security military forces war peace crisis talks deal plan move call calls case group party
""".split())
_WEAK_MATCH = set()


def _init_weak_match():
    for key in list(COUNTRY_ALIASES.keys()) + list(DEMONYMS.keys()):
        for w in re.findall(r"[a-z]{4,}", key):
            _WEAK_MATCH.add(_stem(w))
    for w in _COMMON_MATCH:
        _WEAK_MATCH.add(_stem(w))


def _clip_matches(event_title, clip_text):
    """Does this clip belong to this story? Three gates, all needed:
       1) the clip must BE ABOUT the event — see below,
       2) a shared DISTINGUISHING name (not "northern"/"control", not "Ukrainian"/"Russian"), and
       3) the same country — a Bangkok pub fire cannot belong to a raid in Burkina Faso.
    A person-led story may legitimately cross borders (Trump talking about Lindsey Graham), so a strong
    name+word match can stand in for the location.

    MATCH THE CLIP'S OWN SUBJECT, NOT ITS WHOLE BODY. A post's subject is its FIRST SENTENCE; anything
    after that is context the poster added.
    SHIPPED BUG: a Sergey Lavrov talking-head — "Europe and Ukraine buried the US-Russia agreements
    reached in Alaska" — was filed under a story about Russian tankers being struck in the Sea of Azov,
    purely because its SECOND sentence tacked on "he also labelled the Azov drone strikes 'terrorism'".
    Matching the body let one passing clause hijack a clip into an event it is not footage of. A
    mention is not a subject."""
    if not _WEAK_MATCH:
        _init_weak_match()
    subject = _tg_headline(clip_text) or (clip_text or "")
    shared_names = (_proper_words(event_title) & _proper_words(subject)) - _WEAK_MATCH
    if not shared_names:
        return False
    ev = _geolocate(event_title, "", "")
    cl = _geolocate(subject, "", subject)
    # country/demonym words identify nothing in a war where every story says "Russian" and "Ukraine"
    shared_words = (_sigwords(event_title) & _sigwords(subject)) - _GENERIC_WORDS - _WEAK_MATCH
    # A SHARED LOCATION IS NOT A SHARED SUBJECT. Two Ukraine stories both set in Odesa — a ship struck
    # OFF the coast, and a street PROTEST — are both in Ukraine and both say "Odesa", but the protest is
    # not footage of the ship attack. Strip the event's OWN place before judging distinctiveness; if the
    # only thing shared is that place, the clip merely happens to be in the same town.
    ev_place = (ev[2] if ev else "") or ""
    place_toks = _proper_words(ev_place) | _sigwords(ev_place)
    if not ((shared_names | shared_words) - place_toks):
        return False
    if ev and cl and ev[3] and cl[3] == ev[3]:
        return True
    return len(shared_names) >= 2 and len(shared_words) >= 2


def _clip_score(event_title, clip_text):
    """How WELL a clip fits a story — 0 means it fails the gate in `_clip_matches`, otherwise the count
    of distinctive shared names + words. Used to give a clip to its SINGLE best-matching dot, so one
    piece of footage never appears under two different stories."""
    if not _clip_matches(event_title, clip_text):
        return 0
    subject = _tg_headline(clip_text) or (clip_text or "")
    names = (_proper_words(event_title) & _proper_words(subject)) - _WEAK_MATCH
    words = (_sigwords(event_title) & _sigwords(subject)) - _GENERIC_WORDS - _WEAK_MATCH
    return 2 * len(names) + len(words)


def _exact_youtube(text):
    """Find the EXACT footage of a clip on YouTube (embeddable, no API key) — or {} if we can't be
    confident. A clip Telegram won't serve is shown ONLY when this returns a match, never a related/wrong
    video: search with the clip's distinctive QUOTE + names (a full caption matches nothing on YouTube;
    the quote is what a news upload titles it), then VERIFY the result actually shares the event's content
    words, not just the person's name — that is what separates the real footage from a commentary show."""
    subject = (_tg_headline(text) or text or "").strip()
    if len(subject) < 12:
        return {}
    cache = os.path.join(CACHE_DIR, "yt_" + _slug(subject)[:70] + ".json")
    if _fresh(cache, 3 * 86400):
        try:
            return json.load(open(cache, encoding="utf-8"))
        except Exception:
            pass
    out = {}
    try:
        import yt_dlp
        if not _WEAK_MATCH:
            _init_weak_match()
        opts = {"quiet": True, "skip_download": True, "extract_flat": True,
                "noplaylist": True, "no_warnings": True, "socket_timeout": 15}

        def _search(qq, n=3):
            if not qq:
                return []
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info("ytsearch%d:%s" % (n, qq), download=False)
            return (info or {}).get("entries") or []

        cap_content = _sigwords(subject) - _proper_words(subject) - _GENERIC_WORDS - _WEAK_MATCH
        cap_names = _proper_words(subject)
        caps = re.findall(r"[A-Z][A-Za-z0-9'.\-]{1,}", subject)
        QCH = '"“”‘’«»'
        quotes = re.findall(r"[%s]([^%s]{5,60})[%s]" % (QCH, QCH, QCH), text or "")
        queries = []
        if quotes:
            qstems = {_stem(x) for x in re.findall(r"[a-z0-9]{4,}", quotes[0].lower())}
            namebits = [w for w in caps if _stem(w.lower().strip(".'")) not in qstems][-2:]
            queries.append((quotes[0] + " " + " ".join(namebits)).strip())
        queries.append(" ".join(subject.replace('"', ' ').split()[:8]))
        cands = {}
        for qq in queries:
            for e in _search(qq, 3):
                if e.get("id"):
                    cands.setdefault(e["id"], e)
        best, best_sc = None, (0, 0)
        for e in cands.values():
            tc = (e.get("title") or "") + " " + (e.get("channel") or e.get("uploader") or "")
            sc = len(cap_content & _sigwords(tc))
            sn = len(cap_names & _proper_words(tc))
            if sc >= 2 and sn >= 1 and (sc, sn) > best_sc:   # shares the event's CONTENT and the person — not just a name
                best, best_sc = e, (sc, sn)
        if best:
            out = {"id": best.get("id"), "title": best.get("title"),
                   "channel": best.get("channel") or best.get("uploader") or ""}
    except Exception:
        out = {}
    try:
        json.dump(out, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


# One clip belongs to ONE story. Rebuilt each feed: clip media-id -> the event title that owns it.
_CLIP_OWNER = {}


def _media_id(url):
    """The STABLE identity of a clip. Telegram's cdn URLs carry a `?token=` that is regenerated on
    every fetch, so the raw URL changes between the assign pass and the later event_media call — the
    owner lookup silently missed. Key on the path, without the volatile query."""
    return (url or "").split("?", 1)[0]


def _assign_clips(events, posts):
    """Give every clip to its single best-matching dot. event_media then shows a clip only under its
    owner, so the same footage never appears under several stories (a Ukraine strike clip used to
    attach to every Ukraine strike headline of the day)."""
    owner, best = {}, {}
    for p in posts:
        key = _media_id(p.get("video") or p.get("photo") or p.get("thumb"))
        if not key:
            continue
        text = p.get("text") or ""
        # the SAME clip can be reposted with different captions — score every (post, event) pair and
        # keep the single highest across ALL of them, so the best-fitting dot wins, not the last seen.
        for e in events:
            s = _clip_score(e["title"], text)
            if s > best.get(key, 0):
                best[key], owner[key] = s, e["title"]
    global _CLIP_OWNER
    _CLIP_OWNER = owner


def _proper_words(text):
    """The NAMES in a story — Capitalised tokens (JNIM, Konkoura, Tver, Syzran, Lindsey Graham).
    Word-overlap alone is worthless for matching: a Bangkok pub fire and a JNIM raid in Burkina Faso
    share {'control', 'northern'} by pure coincidence ("seizing CONTROL ... NORTHERN Burkina Faso" vs
    "brought under CONTROL ... NORTHERN Bangkok"). Names do not collide like that."""
    out = set()
    for mm in re.finditer(r"\b([A-Z][A-Za-z0-9'\-]{2,})", text or ""):
        w = mm.group(1).lower()
        if w not in _STOP and len(w) > 2:
            out.add(_stem(w))
    return out


def _distinctive(words):
    """At least one shared word must actually identify the event (a name, place detail, or specific noun)."""
    if not _WEAK_MATCH:
        _init_weak_match()
    return any(w not in _WEAK_MATCH for w in words)


def _sigwords(title):
    return set(_stem(w) for w in re.findall(r"[a-z0-9]{4,}", title.lower()) if w not in _STOP)
# generic conflict/news filler that shouldn't, on its own, make two stories "the same event"
_GENERIC_WORDS = set(_stem(w) for w in (
    "missile missiles ballistic rocket rockets strike strikes airstrike airstrikes attack attacks "
    "force forces military army troops soldier soldiers killed kills dead death deaths war wars "
    "base bases facility facilities launch launched drone drones report reports reported analysis "
    "update latest news breaking target targets targeted fire clash clashes raid raids bomb bombs "
    "bombing shelling operation operations official officials source sources security defense "
    "defence border area region talks deal warns said kill hit hits struck").split())


# ── SIMILARITY METER — "are these two headlines the SAME story?" ───────────────────────────────────
# The distinctive-word dedup (above, _sigwords - _GENERIC_WORDS) reduces a headline to only its rare
# words, which is right for telling two DIFFERENT strikes apart — but it also throws away numbers and
# short words, so the SAME Truth Social post reposted by two channels ("President Trump via Truth Social:
# Afghanistan War: 20 years, 2,000 DEAD." vs the bare "Afghanistan War: 20 years, 2,000 DEAD.") collapsed
# to just {afghanistan, year} in each and never merged — two dots for one story. This meter keeps a RICHER
# token set (numbers and >=2-letter words, commas stripped inside numbers so "2,000" stays one token) and
# scores the OVERLAP COEFFICIENT: shared / the smaller set. That stays ~1.0 when one headline is a near
# subset of the other — exactly what an added "via Truth Social:" prefix, a re-headline, or a category
# split produces — while two genuinely different events (different city, different numbers) score low.
def _norm_tokens(title):
    t = re.sub(r"(?<=\d)[,.](?=\d)", "", (title or "").lower())      # 2,000 -> 2000
    return set(_stem(w) for w in re.findall(r"[a-z0-9]{2,}", t) if w not in _STOP)


def _same_story(a_toks, b_toks):
    """Text half of the duplicate test: True when two rich token sets overlap almost entirely (at least
    4 shared tokens AND an overlap coefficient >= 0.72). world_events gates this with same-place/country
    and a time window, so it only ever collapses the same story told twice — never two different ones."""
    if not a_toks or not b_toks:
        return False
    shared = len(a_toks & b_toks)
    return shared >= 4 and shared / min(len(a_toks), len(b_toks)) >= 0.72
_SPORTS_RESULT = re.compile(r"\b(wins?|won|beat|beats|beaten|defeat(?:s|ed)?|champions?|championship|title|titles|crowned|clinch(?:es|ed)?|gold medal|silver medal|bronze medal|trophy|triumph(?:s|ed)?|victor(?:y|ious)|lift(?:s|ed)? the|grand slam|world record)\b", re.I)
def _sports_worthy(title):
    """Only keep sport when it's an actual result/championship, not routine match/transfer/preview chatter."""
    return bool(_SPORTS_RESULT.search(title or ""))
# Features, op-eds, documentaries, listicles and quizzes are not events — they make dots that show nothing.
# The article's URL SECTION is the most reliable signal (an Al Jazeera "featured-documentaries" piece is
# never breaking news), so block on that first. Note /video/ alone is NOT fluff — France24 files real news
# video under /video/ — so only specific sections are blocked.
_FLUFF_PATHS = (
    "/opinion", "/commentisfree", "/features/", "/longform", "/featured-documentaries", "/documentar",
    "/podcast", "/programmes", "/program/", "/gallery", "/galleries", "/quiz", "/upfront", "/the-take",
    "/inside-story", "/reels", "/lifestyle", "/travel", "/food/", "/culture", "/entertainment", "/arts/",
    "/books", "/style/", "/obituar", "/review", "/explainer", "/in-pictures", "/photo-", "/photos/",
    "/games/", "/gaming", "/tv-and-radio", "/music/", "/film/", "/movies", "/theatre", "/recipes",
    "/wellness", "/health-and-fitness", "/fashion", "/sport/blog", "/live/",
)
_FLUFF_PAT = re.compile(
    r"(?i)("
    r"^(opinion|analysis|explainer|comment|commentary|editorial|review|watch|photos?|podcast|quiz|profile|obituary|long read|the take|reels?)\b\s*[:\-]|"
    r"\bnews live\b|\blive blog\b|\bas it happened\b|\blive updates\b|"
    r"\b(what to know|things to know|here'?s what|a look at|in pictures|photo essay|photo story|round-?up|recap|our picks|best of)\b|"
    r"\b(three|four|five|six|seven|eight|nine|ten|\d+)\s+"
    r"(tests?|things?|ways?|reasons?|lessons?|takeaways?|stories|moments|charts|maps|questions|facts|myths)\b|"
    r"\bat\s+[1-9]\d{2}\s*:|"                       # "United States at 250:" (3-digit anniversary, NOT an age like "dies at 71:")
    r"^the (rise|fall|making|story|life|legacy|meaning|architect) (and|of)\b|"
    r"[:\-]\s*(how|why)\b|"                         # explainer shape: "Greed and loopholes: How ... works"
    r"\bwhy\b.*\bmatters?\b|"
    r"\b(goes viral|feel-good|heartwarming|everything you need)\b|"
    # a READER CALLOUT is not news: "We'd like to speak to maritime workers…" was a dot on the map
    r"^(we|we'?d|we'?re)\b.*\b(like to (speak|hear)|want to hear|would like to)\b|"
    r"\b(share your|tell us|get in touch|contact us|have you been affected)\b|"
    # SPONSORED CONTENT. "New Scholarships. New Programs. Your Next Step." sat on the map as an
    # Israel dot. An advert has no verb and no event — it is addressed to YOU, not reporting a fact.
    r"\b(your next step|apply now|enrol|enroll|register today|limited time|"
    r"special offer|sponsored|advertisement|promoted|partner content|in partnership with|"
    r"book your|sign up (today|now)|find out how|learn more today|discount code)\b|"
    # the ESSAY shape: "The UK and international law – Palestine is the test" is a column, not an event
    r"[–—]\s*\w+ is the (test|answer|problem|solution|key|question|real|future|point)\b|"
    r"\?\s*$"                                       # question headlines are debates, not events
    r")")


def _is_fluff(title, url=""):
    """True for features/op-eds/documentaries that aren't a real event worth a dot on the map.
    Deliberately does NOT require an 'event verb' — that wrongly dropped real news ('missiles have
    IMPACTED the port', 'president NOMINATES a PM'). Losing real news is worse than keeping a feature."""
    low = (url or "").lower()
    for p in _FLUFF_PATHS:
        if p in low:
            return True
    return bool(_FLUFF_PAT.search(title or ""))


def _classify(title, desc=""):
    """Score every category and take the highest — never first-match-wins (see the notes on CAT_ORDER).
    If the headline alone is inconclusive (a bare damage report scored 0 and fell to the 'politics'
    default), score again over the story's own text before giving up."""
    best, best_score = _score_cats(title)
    if best_score == 0 and desc:
        best, best_score = _score_cats(title + " " + desc[:300])
    return best


def _score_cats(text):
    low = " " + re.sub(r"\s+", " ", (text or "").lower()) + " "
    best, best_score = "politics", 0
    for cat in CAT_ORDER:
        text = low
        for pat in CAT_MASK.get(cat, ()):
            text = re.sub(pat, " ", text)
        score = 0
        for kw in CAT_STRONG.get(cat, ()):
            if kw in text:
                score += 3
        for kw in CAT_WEAK.get(cat, ()):
            if kw in text:
                score += 1
        if score > best_score:          # strict > so ties keep CAT_ORDER priority
            best, best_score = cat, score
    return best, best_score


def _fresh(path, ttl):
    try:
        return os.path.exists(path) and (time.time() - os.path.getmtime(path) < ttl)
    except Exception:
        return False


def _gdelt_doc(query, timespan="24h", maxrecords=250):
    params = urllib.parse.urlencode({
        "query": query, "mode": "artlist", "maxrecords": str(maxrecords),
        "timespan": timespan, "format": "json", "sort": "hybridrel",
    })
    url = GDELT_DOC + "?" + params
    # GDELT rate-limits to ~1 request every few seconds → back off on 429
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (Meridian)"})
            raw = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
            try:
                return (json.loads(raw) or {}).get("articles") or []
            except Exception:
                return []
        except urllib.error.HTTPError as ex:
            if getattr(ex, "code", None) == 429 and attempt < 1:
                time.sleep(2)
                continue
            raise
    return []


# Background refresh for stale-while-revalidate: rebuild the world feed off the UI thread so a stale
# cache can be served instantly while the fresh one is computed. Guarded so only one runs per window.
_WORLD_REFRESH = set()
_WORLD_REFRESH_LOCK = threading.Lock()


def _spawn_world_refresh(api, h):
    with _WORLD_REFRESH_LOCK:
        if h in _WORLD_REFRESH:
            return
        _WORLD_REFRESH.add(h)

    def _run():
        try:
            api._build_world_events(h)
        except Exception:
            pass
        finally:
            with _WORLD_REFRESH_LOCK:
                _WORLD_REFRESH.discard(h)
    threading.Thread(target=_run, daemon=True).start()


# Country name -> FIPS 10-4 code, for GDELT's sourcecountry: filter (which is NOT ISO). Keyed by the
# app's own country names plus the obvious aliases, so country_news("Latvia") -> "LG". A country missing
# here just can't be starred yet (country_news returns unsupported) — add its code to enable it.
_FIPS = {
    "afghanistan": "AF", "albania": "AL", "algeria": "AG", "angola": "AO", "argentina": "AR",
    "armenia": "AM", "australia": "AS", "austria": "AU", "azerbaijan": "AJ", "bahrain": "BA",
    "bangladesh": "BG", "belarus": "BO", "belgium": "BE", "bolivia": "BL", "bosnia and herzegovina": "BK",
    "bosnia": "BK", "brazil": "BR", "bulgaria": "BU", "cambodia": "CB", "cameroon": "CM", "canada": "CA",
    "chile": "CI", "china": "CH", "colombia": "CO", "croatia": "HR", "cuba": "CU", "cyprus": "CY",
    "czechia": "EZ", "czech republic": "EZ", "denmark": "DA", "dominican republic": "DR", "ecuador": "EC",
    "egypt": "EG", "el salvador": "ES", "estonia": "EN", "ethiopia": "ET", "finland": "FI", "france": "FR",
    "georgia": "GG", "germany": "GM", "ghana": "GH", "greece": "GR", "guatemala": "GT", "honduras": "HO",
    "hungary": "HU", "iceland": "IC", "india": "IN", "indonesia": "ID", "iran": "IR", "iraq": "IZ",
    "ireland": "EI", "israel": "IS", "italy": "IT", "ivory coast": "IV", "cote d'ivoire": "IV",
    "jamaica": "JM", "japan": "JA", "jordan": "JO", "kazakhstan": "KZ", "kenya": "KE",
    "north korea": "KN", "south korea": "KS", "korea": "KS", "kuwait": "KU", "kyrgyzstan": "KG",
    "laos": "LA", "latvia": "LG", "lebanon": "LE", "libya": "LY", "lithuania": "LH", "luxembourg": "LU",
    "malaysia": "MY", "mali": "ML", "malta": "MT", "mexico": "MX", "moldova": "MD", "mongolia": "MG",
    "montenegro": "MJ", "morocco": "MO", "mozambique": "MZ", "myanmar": "BM", "burma": "BM",
    "nepal": "NP", "netherlands": "NL", "new zealand": "NZ", "nicaragua": "NU", "nigeria": "NI",
    "north macedonia": "MK", "macedonia": "MK", "norway": "NO", "oman": "MU", "pakistan": "PK",
    "panama": "PM", "paraguay": "PA", "peru": "PE", "philippines": "RP", "poland": "PL", "portugal": "PO",
    "qatar": "QA", "romania": "RO", "russia": "RS", "saudi arabia": "SA", "senegal": "SG", "serbia": "RI",
    "singapore": "SN", "slovakia": "LO", "slovenia": "SI", "somalia": "SO", "south africa": "SF",
    "spain": "SP", "sri lanka": "CE", "sudan": "SU", "sweden": "SW", "switzerland": "SZ", "syria": "SY",
    "taiwan": "TW", "tajikistan": "TI", "tanzania": "TZ", "thailand": "TH", "tunisia": "TS", "turkey": "TU",
    "turkmenistan": "TX", "uganda": "UG", "ukraine": "UP", "united arab emirates": "AE", "uae": "AE",
    "united kingdom": "UK", "uk": "UK", "britain": "UK", "great britain": "UK",
    "united states": "US", "united states of america": "US", "usa": "US", "america": "US",
    "uruguay": "UY", "uzbekistan": "UZ", "venezuela": "VE", "vietnam": "VM", "yemen": "YM",
    "zambia": "ZA", "zimbabwe": "ZI",
}


def _fips_for(country):
    return _FIPS.get((country or "").strip().lower(), "")


def _country_key(s):
    s = (s or "").strip().lower()
    for pre in ("the ", "republic of "):
        if s.startswith(pre):
            s = s[len(pre):]
    s = re.sub(r"[^a-z]", "", s)
    alias = {"unitedstatesofamerica": "unitedstates", "usa": "unitedstates", "america": "unitedstates",
             "us": "unitedstates", "greatbritain": "unitedkingdom", "britain": "unitedkingdom",
             "uk": "unitedkingdom", "czechrepublic": "czechia", "burma": "myanmar",
             "koreasouth": "southkorea", "korea": "southkorea", "koreanorth": "northkorea"}
    return alias.get(s, s)


def _country_match(a, b):
    """Do two country names refer to the same country? Bridges 'United States of America' vs 'United
    States', 'Czechia' vs 'Czech Republic', etc., so the starred filter and geolocation agree."""
    ka, kb = _country_key(a), _country_key(b)
    return bool(ka) and ka == kb


# ── CURRENT LEADERS (reliable) ─────────────────────────────────────────────────────────────────────
# Leadership USED to be read from Wikidata's SPARQL service (WDQS) IN THE BROWSER — which times out and
# rate-limits constantly, so it kept failing to stale data and showing the wrong leaders. Two fixes here:
#   1) read Wikidata through its CDN-cached ENTITY api (wbgetentities) — fast and reliable; and
#   2) CROSS-CHECK the pick against the CIA World Factbook name the page already has, because Wikidata is
#      sometimes vandalised/mis-modelled (right now it literally has the WRONG person marked "preferred"
#      as Iran's head of state). When the two disagree and Wikidata isn't showing a very recent term start
#      (so it isn't a fresh election), trust the curated Factbook name — and find that same person in
#      Wikidata's claims so we still get their photo. Runs on the backend: cached daily, and TESTABLE.
def _wd_entities(ids, props="claims"):
    url = ("https://www.wikidata.org/w/api.php?action=wbgetentities&ids=" + urllib.parse.quote(ids)
           + "&props=" + props + "&languages=en&format=json")
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Meridian/1.0"})
            raw = urllib.request.urlopen(req, timeout=15).read().decode("utf-8", "replace")
            return (json.loads(raw) or {}).get("entities", {}) or {}
        except urllib.error.HTTPError as ex:
            if getattr(ex, "code", None) == 429 and attempt < 2:
                time.sleep(1.5)
                continue
            return {}
        except Exception:
            if attempt < 2:
                time.sleep(1.0)
                continue
            return {}
    return {}


def _wd_claim_qid(c):
    v = c.get("mainsnak", {}).get("datavalue", {}).get("value", {})
    return v.get("id") if isinstance(v, dict) else None


def _wd_qual_time(c, pid):
    q = c.get("qualifiers", {}).get(pid, [])
    if q:
        t = q[0].get("datavalue", {}).get("value", {}).get("time", "") or ""
        return t.lstrip("+")[:10]
    return ""


def _name_key(s):
    return [w for w in re.sub(r"[^a-z ]", " ", _fold(s or "").lower()).split() if len(w) > 1]


def _name_match(a, b):
    """Same PERSON? Tolerant of middle names / transliteration, but strict enough that a shared surname
    alone is NOT a match — otherwise 'Ali Khamenei' and his son 'Mojtaba Khamenei' would collapse together.
    Needs a shared given name on top of the surname (or two shared tokens generally)."""
    ta, tb = _name_key(a), _name_key(b)
    if not ta or not tb:
        return False
    shared = set(ta) & set(tb)
    if not shared:
        return False
    if len(ta) == 1 or len(tb) == 1:                 # a mononym — a single shared token is all there is
        return True
    return len(shared) >= 2                            # surname + at least one more shared token


def _same_person(a_name, a_qid, b_name, b_qid):
    """Are two resolved leaders the SAME individual? Prefer QIDs. When both come from the Factbook only
    (no QID), a shared family name is NOT enough — Saudi's King Salman and his son Mohammed bin Salman
    share 'bin Salman al Saud' yet are different people — so also require the same given (first) name."""
    if a_qid and b_qid:
        return a_qid == b_qid
    ta, tb = _name_key(a_name), _name_key(b_name)
    if not ta or not tb:
        return False
    return ta[0] == tb[0] and _name_match(a_name, b_name)


def _days_since(datestr):
    try:
        d = datetime.datetime.strptime((datestr or "")[:10], "%Y-%m-%d")
        return (datetime.datetime.utcnow() - d).days
    except Exception:
        return 99999


def _clean_office_title(lbl, fallback):
    t = (lbl or "").strip()
    if not t or re.match(r"^Q\d+$", t):
        return fallback
    parts = re.split(r"\s+of\s+", t, flags=re.I)
    if len(parts) > 1:
        parts.pop()
    t = " of ".join(parts).strip()
    tl = t.lower()
    if tl.startswith("head of gov"):
        return "Prime Minister"
    if tl.startswith("head of state"):
        return "President"
    if re.match(r"^(monarch|sovereign)", tl):
        return "King"
    return fallback if (not t or len(t) > 26) else t


def _pick_leader(cands, fb_name):
    """Choose the CURRENT officeholder. Each cand: {qid,name,ended,dead,start,preferred,...}.
    HARD RULE first: a leader whose term has ended (P582) OR who has DIED (P570) is never current — no
    other source can resurrect them (a stale Factbook once put a dead Supreme Leader back on the map).
    Among those still in office: prefer 'preferred' rank, then the most recent start. The Factbook name is
    only used to DISAMBIGUATE when Wikidata lists more than one live holder (e.g. a vandalised extra claim);
    it can never override the death/end-of-term signal."""
    if not cands:
        return None
    live = [c for c in cands if not c.get("ended") and not c.get("dead")]
    pool = live or cands                                           # if nobody is 'live', fall back gracefully
    if fb_name and len(live) > 1:
        m = next((c for c in live if _name_match(c.get("name", ""), fb_name)), None)
        if m:
            return m
    return sorted(pool, key=lambda c: (1 if c.get("preferred") else 0, c.get("start") or ""), reverse=True)[0]


# The CIA World Factbook is more COMPLETE than Wikidata for "who holds power" (it always lists a chief of
# state AND a head of government), so we parse its free-text fields to fill gaps — e.g. Saudi Arabia, whose
# head of government (Crown Prince MBS) Wikidata doesn't record. Factbook capitalises the surname and puts
# the office first: "Crown Prince and Prime Minister MUHAMMAD BIN SALMAN bin Abd al-Aziz Al Saud (since …)".
_FB_TITLES = set(("president prime minister supreme leader king queen emir emira sultan monarch chancellor "
                  "chief prince princess sheikh sheikha shaykh grand duke duchess governor general captain "
                  "regent co-prince sovereign acting interim transitional head state council chairperson "
                  "chairman chairwoman presidential federal vice deputy and the of crown paramount").split())
_FB_PARTICLES = {"bin", "al", "of", "the", "von", "van", "de", "da", "del", "el", "ibn", "abu", "abd", "ben", "bint", "la"}


def _fb_parse(raw):
    """(name, title) from a Factbook chief-of-state / head-of-government string. Returns ('', '') if blank."""
    s = re.split(r"\(since|\(", raw or "", flags=re.I)[0]
    s = re.split(r"[;,]", s)[0].strip()
    toks = s.split()
    ti = 0
    while ti < len(toks) and re.sub(r"[^a-z-]", "", toks[ti].lower()) in _FB_TITLES:
        ti += 1
    title = " ".join(toks[:ti]).strip()
    parts = []
    for i, t in enumerate(toks[ti:]):
        low = t.lower()
        if i > 0 and low in _FB_PARTICLES:
            parts.append(low)
        elif "-" in t:
            parts.append("-".join(p[:1].upper() + p[1:].lower() for p in t.split("-")))
        elif t.isupper() or t.islower():
            parts.append(t[:1].upper() + t[1:].lower())
        else:
            parts.append(t)
    return (" ".join(parts).strip(), title)


def _wd_search_person(name):
    """Resolve a person's name to a LIVING Wikidata human: {qid,name,img}. Uses wbsearchentities so it
    tolerates transliteration (Factbook 'Muhammad' vs Wikidata 'Mohammed'). None if no living human matches."""
    name = (name or "").strip()
    if len(name) < 3:
        return None
    try:
        url = ("https://www.wikidata.org/w/api.php?action=wbsearchentities&search="
               + urllib.parse.quote(name) + "&language=en&type=item&limit=7&format=json")
        j = json.loads(urllib.request.urlopen(urllib.request.Request(url, headers={"User-Agent": "Meridian/1.0"}),
                                              timeout=12).read().decode("utf-8", "replace"))
        ids = [h.get("id") for h in (j.get("search") or []) if h.get("id")]
        if not ids:
            return None
        ents = _wd_entities("|".join(ids[:7]), "labels|claims")
        for q in ids:
            e = ents.get(q, {})
            if not any(_wd_claim_qid(c) == "Q5" for c in e.get("claims", {}).get("P31", [])):
                continue                                              # not a human
            if e.get("claims", {}).get("P570"):
                continue                                              # dead -> skip
            img = ""
            p18 = e.get("claims", {}).get("P18", [])
            if p18:
                fn = p18[0].get("mainsnak", {}).get("datavalue", {}).get("value", "")
                if fn:
                    img = ("https://commons.wikimedia.org/wiki/Special:FilePath/"
                           + urllib.parse.quote(fn.replace(" ", "_")) + "?width=240")
            return {"qid": q, "name": (e.get("labels", {}).get("en", {}) or {}).get("value", name), "img": img}
        return None
    except Exception:
        return None


# Authoritative political grading for EVERY country — V-Dem's academic regime classification (via Our World
# in Data), snapshotted into country_grades.json by fetch_grades.py. This replaces the app's hand-guessed
# "political leaning" with a real, sourced grade (Closed autocracy → Liberal democracy + a 0-1 score).
def _load_grades():
    try:
        return json.load(open(os.path.join(RES_DIR, "country_grades.json"), encoding="utf-8"))
    except Exception:
        return {"grades": {}, "byname": {}, "source": "", "generated": ""}


_GRADES = _load_grades()


def _grade_for(name):
    key = re.sub(r"[^a-z]", "", _fold(name or "").lower())
    code = (_GRADES.get("byname") or {}).get(key)
    return (_GRADES.get("grades") or {}).get(code) if code else None


# The GOVERNING lean (left <-> right): the party in power's documented political alignment (Wikidata P1387),
# mapped onto a -3..+3 scale. Not an opinion — it's the party's own cited positioning.
_LEAN_LABELS = ["Far left", "Left", "Centre-left", "Centre", "Centre-right", "Right", "Far right"]


def _lean_from_alignments(labels):
    """Map political-alignment strings ('centre-right politics', ...) to -3..+3, or None. Order matters:
    the compound terms (far-*, centre-*) are checked before the bare 'left/right wing'."""
    order = [("far-left", -3), ("far left", -3), ("far-right", 3), ("far right", 3),
             ("centre-left", -1), ("center-left", -1), ("centre-right", 1), ("center-right", 1),
             ("left-wing", -2), ("left wing", -2), ("right-wing", 2), ("right wing", 2),
             ("centrism", 0), ("centre", 0), ("center", 0), ("syncretic", 0), ("big tent", 0)]
    scores = []
    for lab in labels:
        ll = (lab or "").lower()
        for kw, sc in order:
            if kw in ll:
                scores.append(sc)
                break
    if not scores:
        return None
    return max(-3, min(3, round(sum(scores) / len(scores))))


def _ruling_party_lean(ruling_qid, ruling_entity=None):
    """The lean of the party the given leader currently belongs to. Returns {party, label, lean_n} or None."""
    if not ruling_qid:
        return None
    try:
        e = ruling_entity or _wd_entities(ruling_qid, "claims").get(ruling_qid, {})
        party = None
        for c in e.get("claims", {}).get("P102", []):
            if "P582" not in c.get("qualifiers", {}):        # a CURRENT party membership (no end date)
                party = _wd_claim_qid(c)
                break
        if not party:
            pl = e.get("claims", {}).get("P102", [])
            party = _wd_claim_qid(pl[0]) if pl else None
        if not party:
            return None
        pe = _wd_entities(party, "labels|claims").get(party, {})
        aligns = [_wd_claim_qid(c) for c in pe.get("claims", {}).get("P1387", [])]
        aligns = [a for a in aligns if a]
        labels = []
        if aligns:
            ae = _wd_entities("|".join(aligns[:5]), "labels")
            labels = [((ae.get(a, {}).get("labels", {}) or {}).get("en", {}) or {}).get("value", "") for a in aligns]
        n = _lean_from_alignments(labels)
        if n is None:
            return None
        return {"party": ((pe.get("labels", {}) or {}).get("en", {}) or {}).get("value", ""),
                "lean_n": n, "label": _LEAN_LABELS[n + 3]}
    except Exception:
        return None


def _clean_headline(t):
    t = _htmlmod.unescape(t or "")
    t = _strip_promo(t)                                          # bare links, "Follow @x for more news", @handles
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)                       # GDELT spaces before punctuation
    if len(t) > 55:
        # strip a trailing " - Outlet". The separator MUST have whitespace before it — an outlet
        # suffix is " - Reuters", never touching the word. SHIPPED BUG: `\s*` made that space
        # optional, so the hyphen inside "anti-corruption campaign" counted as a separator and the
        # headline was chopped to "...detained in anti". Compounds (anti-, pro-, U-turn, COVID-19)
        # and ranges (2020–2024) have NO space before the dash and are now safe.
        t = re.sub(r"\s+[-–—|]\s*[^-–—|]{2,32}$", "", t)
    t = re.sub(r"\s{2,}", " ", t).strip()
    return t[:200]


def _good_img(u):
    if not u or not u.startswith("http"):
        return False
    lu = u.lower()
    bad = ("og-image.png", "placeholder", "default.jp", "default.pn", "/logo",
           "sprite", "favicon", "blank.", "-logo", "logo.", "share_default", "_default.")
    return not any(b in lu for b in bad)


_OUTLET_NAMES = {
    "aljazeera.com": "Al Jazeera", "aljazeera.net": "Al Jazeera", "bbc.com": "BBC",
    "bbc.co.uk": "BBC", "cnn.com": "CNN", "reuters.com": "Reuters", "apnews.com": "AP",
    "theguardian.com": "The Guardian", "nytimes.com": "The New York Times",
    "washingtonpost.com": "The Washington Post", "wsj.com": "The Wall Street Journal",
    "ft.com": "Financial Times", "bloomberg.com": "Bloomberg", "dawn.com": "Dawn",
    "scmp.com": "South China Morning Post", "france24.com": "France 24",
    "dw.com": "Deutsche Welle", "npr.org": "NPR", "politico.com": "Politico",
    "axios.com": "Axios", "cnbc.com": "CNBC", "moneycontrol.com": "Moneycontrol",
    "thehindu.com": "The Hindu", "timesofindia.indiatimes.com": "The Times of India",
    "kyivindependent.com": "The Kyiv Independent", "pravda.com.ua": "Ukrainska Pravda",
    "tass.com": "TASS", "cbsnews.com": "CBS News", "nbcnews.com": "NBC News",
    "abcnews.go.com": "ABC News", "thehill.com": "The Hill", "jpost.com": "The Jerusalem Post",
    "timesofisrael.com": "The Times of Israel", "arabnews.com": "Arab News",
    "thenationalnews.com": "The National", "japantimes.co.jp": "The Japan Times",
    "koreaherald.com": "The Korea Herald", "straitstimes.com": "The Straits Times",
    "abc.net.au": "ABC (Australia)", "independent.co.uk": "The Independent",
    "telegraph.co.uk": "The Telegraph", "thetimes.co.uk": "The Times",
    "euronews.com": "Euronews", "rferl.org": "RFE/RL", "voanews.com": "VOA",
    "nehandaradio.com": "Nehanda Radio", "aol.co.uk": "AOL",
}


def _domain_name(domain):
    d = (domain or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]
    if d in _OUTLET_NAMES:
        return _OUTLET_NAMES[d]
    core = d.split(".")[0] if d else ""
    return (core[:1].upper() + core[1:]) if core else (domain or "Source")


def _jitter(lat, lng, key):
    hexd = hashlib.md5(key.encode("utf-8")).hexdigest()
    a = int(hexd[:4], 16) / 65535.0 - 0.5
    b = int(hexd[4:8], 16) / 65535.0 - 0.5
    return lat + a * 1.4, lng + b * 1.4


# How map-worthy a category is, most first. A strike outranks an analysis piece at the same spot.
_SEVERITY = {"security": 0, "climate": 1, "health": 2, "society": 3, "economy": 4,
             "tech": 5, "politics": 6, "sports": 7}


def _collapse_colocated(events, window_h=6):
    """The map answers "what is happening WHERE". Several dots on the SAME specific place within a few
    hours are one unfolding situation — the Odesa barrage arrived as three terse posts the classifier
    split across categories ("Kh-22 impacts in Odesa" -> security, "2 on course for Odesa" -> politics)
    that shared only the place word, so the word-overlap dedup never merged them and they STACKED.
    Keep the single most map-worthy dot per place+window. Nothing is lost from the app — the Live Wire
    still carries every original post (the map is the curated layer). A country-level dot never
    collapses: two "Russia" stories are different events, only a SPECIFIC place (city/facility) does."""
    kept, buckets = [], {}
    for e in events:
        pl, co = e.get("place") or "", e.get("country") or ""
        specific = bool(pl) and pl != co and pl != _co_short(co)
        if specific:
            hit = None
            for ki in buckets.get(pl, []):
                if abs(e["hrs"] - kept[ki]["hrs"]) <= window_h:
                    hit = ki
                    break
            if hit is not None:
                k = kept[hit]
                if _SEVERITY.get(e["cat"], 9) < _SEVERITY.get(k["cat"], 9):
                    if not e.get("image") and k.get("image"):
                        e["image"] = k["image"]           # the survivor should still show a picture
                    kept[hit] = e
                elif not k.get("image") and e.get("image"):
                    k["image"] = e["image"]
                continue
        kept.append(e)
        if specific:
            buckets.setdefault(pl, []).append(len(kept) - 1)
    return kept


def _spread(events):
    """Co-located dots would hide each other, so nudge only the EXTRAS a hair off the shared point — the
    first (most map-worthy) dot stays EXACTLY on the real place. The offset is tiny (a few hundred metres,
    growing ring by ring), so a dot still reads as sitting ON its city at a country/regional zoom and only
    fans apart when you zoom right in. The old version pushed EVERY dot up to ~24 km onto a ring, which is
    why generic country news (all placed at the capital seat) looked scattered AROUND Moscow/Tehran/Kyiv
    instead of on them."""
    groups = {}
    for e in events:
        groups.setdefault((round(e["lat"], 3), round(e["lng"], 3)), []).append(e)
    for (blat, blng), grp in groups.items():
        if len(grp) < 2:
            continue
        lngscale = max(0.35, math.cos(math.radians(blat)))
        for i, e in enumerate(grp):
            if i == 0:
                continue                               # primary dot stays exactly on the place
            ring = 1 + (i - 1) // 8                      # 8 dots per ring, then widen a little
            step = (i - 1) % 8
            r = 0.004 * ring                            # ~0.4 km per ring — stays on the city until you zoom in
            ang = 2 * math.pi * (step / 8.0) + 0.4
            e["lat"] = round(blat + r * math.sin(ang), 4)
            e["lng"] = round(blng + r * math.cos(ang) / lngscale, 4)
    return events


def _seendate_hours(s):
    try:
        dt = datetime.datetime.strptime(s, "%Y%m%dT%H%M%SZ").replace(tzinfo=datetime.timezone.utc)
        secs = (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds()
        return max(0.0, secs / 3600.0)
    except Exception:
        return 1.0


# Reliable fallback when GDELT is unavailable: major world/topic RSS feeds.
# These give REAL publisher URLs, so article_detail() resolves photos + paragraphs.
# Each entry: (feed url, home country for headlines with no detectable place).
WORLD_FEEDS = [
    ("https://feeds.bbci.co.uk/news/world/rss.xml", "United Kingdom"),
    ("https://www.theguardian.com/world/rss", "United Kingdom"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Qatar"),
    ("https://feeds.npr.org/1004/rss.xml", "United States of America"),
    ("https://www.france24.com/en/rss", "France"),
    ("https://timesofindia.indiatimes.com/rssfeeds/296589292.cms", "India"),
    ("https://www.cnbc.com/id/100727362/device/rss/rss.html", "United States of America"),
    ("https://feeds.bbci.co.uk/news/science_and_environment/rss.xml", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/technology/rss.xml", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/health/rss.xml", "United Kingdom"),
    ("https://www.theguardian.com/football/rss", "United Kingdom"),
    ("https://www.theguardian.com/business/rss", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/world/africa/rss.xml", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/world/asia/rss.xml", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/world/middle_east/rss.xml", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/world/europe/rss.xml", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/world/latin_america/rss.xml", "United Kingdom"),
    ("https://feeds.bbci.co.uk/news/world/us_and_canada/rss.xml", "United States of America"),
    ("https://feeds.bbci.co.uk/news/business/rss.xml", "United Kingdom"),
    ("https://www.theguardian.com/world/middleeast/rss", "United Kingdom"),
    ("https://www.theguardian.com/us-news/rss", "United States of America"),
    ("https://www.theguardian.com/technology/rss", "United Kingdom"),
    ("https://www.theguardian.com/environment/rss", "United Kingdom"),
    ("https://www.aljazeera.com/xml/rss/all.xml", "Qatar"),
    ("https://www.channelnewsasia.com/api/v1/rss-outbound-feed?_format=xml", "Singapore"),
    ("https://www.abc.net.au/news/feed/51120/rss.xml", "Australia"),
    ("https://www.cbc.ca/webfeed/rss/rss-world", "Canada"),
    ("https://www.jpost.com/rss/rssfeedsheadlines.aspx", "Israel"),
    ("https://www.arabnews.com/rss.xml", "Saudi Arabia"),
    ("https://www.japantimes.co.jp/feed/", "Japan"),
    ("https://www.independent.co.uk/news/world/rss", "United Kingdom"),
    ("https://moxie.foxnews.com/google-publisher/world.xml", "United States of America"),
    ("https://feeds.bbci.co.uk/sport/rss.xml", "United Kingdom"),
    ("https://api.axios.com/feed/", "United States of America"),
    ("https://feeds.bbci.co.uk/news/politics/rss.xml", "United Kingdom"),
    ("https://www.theguardian.com/world/europe-news/rss", "United Kingdom"),
    ("https://tass.com/rss/v2.xml", "Russia"),
    ("https://www.rt.com/rss/", "Russia"),
    ("https://www.ukrinform.net/rss/block-lastnews", "Ukraine"),
    ("https://www.globaltimes.cn/rss/outbrain.xml", "China"),
    ("https://www.aa.com.tr/en/rss/default?cat=guncel", "Turkey"),
    ("https://www.timesofisrael.com/feed/", "Israel"),
    ("https://www.thehindu.com/news/national/feeder/default.rss", "India"),
    ("https://rss.dw.com/xml/rss-en-all", "Germany"),
    ("https://www.batimes.com.ar/feed", "Argentina"),
]


def _cdata(s):
    return _htmlmod.unescape(re.sub(r"<!\[CDATA\[|\]\]>", "", s or "")).strip()


def _domain_of(url):
    try:
        d = urllib.parse.urlparse(url).netloc.lower()
        return d[4:] if d.startswith("www.") else d
    except Exception:
        return ""


def _pub_hours(s):
    s = (s or "").strip()
    if not s:
        return 1.0
    try:
        import email.utils
        dt = email.utils.parsedate_to_datetime(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=datetime.timezone.utc)
        return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600.0)
    except Exception:
        pass
    for f in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%d %H:%M:%S"):
        try:
            dt = datetime.datetime.strptime(s, f)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return max(0.0, (datetime.datetime.now(datetime.timezone.utc) - dt).total_seconds() / 3600.0)
        except Exception:
            pass
    return 1.0


def _upsize_thumb(u):
    # BBC ships tiny 240px thumbs → request a larger crop
    return re.sub(r"/(?:standard|news|cpsprodpb)?/?\d{2,3}/", lambda m: m.group(0).replace(
        re.search(r"\d{2,3}", m.group(0)).group(0), "800"), u) if ("ichef.bbci" in (u or "")) else (u or "")


def _feed_articles(url, home):
    out = []
    try:
        x = _http_get(url, 9)
    except Exception:
        return out
    blocks = re.findall(r"<item>(.*?)</item>", x, re.S) or re.findall(r"<entry>(.*?)</entry>", x, re.S)
    for b in blocks[:22]:
        tm = re.search(r"<title[^>]*>(.*?)</title>", b, re.S)
        title = _cdata(re.sub(r"<[^>]+>", "", tm.group(1))) if tm else ""
        lm = re.search(r"<link[^>]*>(.*?)</link>", b, re.S)
        link = _cdata(lm.group(1)) if lm else ""
        if not link:
            lm = re.search(r'<link[^>]*href="([^"]+)"', b)
            link = lm.group(1).strip() if lm else ""
        pm = (re.search(r"<pubDate>(.*?)</pubDate>", b, re.S) or re.search(r"<published>(.*?)</published>", b, re.S)
              or re.search(r"<updated>(.*?)</updated>", b, re.S) or re.search(r"<dc:date>(.*?)</dc:date>", b, re.S))
        im = (re.search(r'<media:thumbnail[^>]*url="([^"]+)"', b) or re.search(r'<media:content[^>]*url="([^"]+)"', b)
              or re.search(r'<enclosure[^>]*url="([^"]+)"', b))
        dm = (re.search(r"<description>(.*?)</description>", b, re.S) or re.search(r"<summary[^>]*>(.*?)</summary>", b, re.S))
        desc = ""
        if dm:
            _raw = _cdata(dm.group(1))
            _p = re.search(r"<p[^>]*>(.*?)</p>", _raw, re.S)
            _core = _p.group(1) if _p else _raw
            desc = re.sub(r"\s{2,}", " ", re.sub(r"<[^>]+>", " ", _core)).strip()
            desc = re.split(r"(?i)continue reading|prefer the guardian|read more", desc)[0].strip()[:360]
        out.append({
            "title": title, "url": link.split("?")[0] if "theguardian" not in link else link,
            "hrs": round(_pub_hours(_cdata(pm.group(1)) if pm else ""), 1),
            "socialimage": _upsize_thumb(im.group(1)) if im else "",
            "domain": _domain_of(link), "sourcecountry": home, "desc": desc,
        })
    return out


def _collect_feeds():
    arts = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=12) as ex:
        futs = {ex.submit(_feed_articles, url, home): url for url, home in WORLD_FEEDS}
        for fu in concurrent.futures.as_completed(futs, timeout=14):
            try:
                arts += fu.result()
            except Exception:
                pass
    return arts


# capital coordinates, keyed to the map's own country names (so flags resolve)
COUNTRY_COORDS = {
    "United States of America": (38.90, -77.04), "United Kingdom": (51.51, -0.13),
    "France": (48.85, 2.35), "Germany": (52.52, 13.40), "Italy": (41.90, 12.50),
    "Spain": (40.42, -3.70), "Portugal": (38.72, -9.14), "Netherlands": (52.37, 4.90),
    "Belgium": (50.85, 4.35), "Switzerland": (46.95, 7.44), "Austria": (48.21, 16.37),
    "Ireland": (53.35, -6.26), "Norway": (59.91, 10.75), "Sweden": (59.33, 18.06),
    "Denmark": (55.68, 12.57), "Finland": (60.17, 24.94), "Iceland": (64.15, -21.94),
    "Poland": (52.23, 21.01), "Czechia": (50.09, 14.42), "Slovakia": (48.15, 17.11),
    "Hungary": (47.50, 19.04), "Romania": (44.43, 26.10), "Bulgaria": (42.70, 23.32),
    "Greece": (37.98, 23.73), "Ukraine": (50.45, 30.52), "Russia": (55.75, 37.62),
    "Belarus": (53.90, 27.57), "Serbia": (44.79, 20.45), "Croatia": (45.81, 15.98),
    "Turkey": (39.93, 32.87), "Cyprus": (35.17, 33.36), "Georgia": (41.72, 44.79),
    "Armenia": (40.18, 44.51), "Azerbaijan": (40.41, 49.87), "Kazakhstan": (51.16, 71.47),
    "Uzbekistan": (41.31, 69.24), "Israel": (31.78, 35.22), "Palestine": (31.50, 34.47),
    "Lebanon": (33.89, 35.50), "Syria": (33.51, 36.29), "Jordan": (31.95, 35.93),
    "Iraq": (33.32, 44.36), "Iran": (35.69, 51.39), "Saudi Arabia": (24.71, 46.68),
    "United Arab Emirates": (24.45, 54.38), "Qatar": (25.29, 51.53), "Kuwait": (29.38, 47.99),
    "Bahrain": (26.23, 50.59), "Oman": (23.59, 58.41), "Yemen": (15.35, 44.21),
    "Egypt": (30.04, 31.24), "Libya": (32.89, 13.19), "Tunisia": (36.81, 10.18),
    "Algeria": (36.75, 3.06), "Morocco": (34.02, -6.83), "Sudan": (15.50, 32.56),
    "S. Sudan": (4.85, 31.58), "Ethiopia": (9.03, 38.74), "Eritrea": (15.34, 38.93),
    "Somalia": (2.04, 45.34), "Kenya": (-1.29, 36.82), "Uganda": (0.35, 32.58),
    "Tanzania": (-6.79, 39.21), "Rwanda": (-1.94, 30.06), "Nigeria": (9.06, 7.50),
    "Ghana": (5.60, -0.19), "Senegal": (14.72, -17.47), "Mali": (12.64, -8.00),
    "Niger": (13.51, 2.11), "Burkina Faso": (12.37, -1.52), "Cameroon": (3.85, 11.50),
    "Dem. Rep. Congo": (-4.32, 15.31), "Congo": (-4.26, 15.24), "Angola": (-8.84, 13.23),
    "Zambia": (-15.42, 28.28), "Zimbabwe": (-17.83, 31.05), "Mozambique": (-25.97, 32.58),
    "South Africa": (-25.75, 28.19), "Madagascar": (-18.88, 47.51), "Côte d'Ivoire": (5.35, -4.00),
    "China": (39.90, 116.40), "Japan": (35.68, 139.69), "South Korea": (37.57, 126.98),
    "North Korea": (39.02, 125.75), "Taiwan": (25.03, 121.57), "Hong Kong": (22.32, 114.17),
    "India": (28.61, 77.21), "Pakistan": (33.69, 73.06), "Bangladesh": (23.81, 90.41),
    "Sri Lanka": (6.93, 79.85), "Nepal": (27.72, 85.32), "Afghanistan": (34.53, 69.17),
    "Myanmar": (16.87, 96.20), "Thailand": (13.76, 100.50), "Vietnam": (21.03, 105.85),
    "Cambodia": (11.56, 104.92), "Malaysia": (3.14, 101.69), "Singapore": (1.35, 103.82),
    "Indonesia": (-6.21, 106.85), "Philippines": (14.60, 120.98), "Australia": (-35.28, 149.13),
    "New Zealand": (-41.29, 174.78), "Canada": (45.42, -75.70), "Mexico": (19.43, -99.13),
    "Guatemala": (14.63, -90.51), "Cuba": (23.11, -82.37), "Haiti": (18.59, -72.31),
    "Colombia": (4.71, -74.07), "Venezuela": (10.48, -66.90), "Ecuador": (-0.18, -78.47),
    "Peru": (-12.05, -77.04), "Brazil": (-15.79, -47.88), "Bolivia": (-16.50, -68.15),
    "Chile": (-33.45, -70.67), "Argentina": (-34.60, -58.38), "Uruguay": (-34.90, -56.19),
    "Paraguay": (-25.28, -57.63),
}

# city -> (lat, lng, country) — more specific than the country fallback
CITY_COORDS = {
    "gaza city": (31.50, 34.47, "Palestine"), "gaza": (31.50, 34.47, "Palestine"),
    "rafah": (31.29, 34.24, "Palestine"), "west bank": (31.95, 35.30, "Palestine"),
    "jerusalem": (31.78, 35.22, "Israel"), "tel aviv": (32.08, 34.78, "Israel"),
    "new delhi": (28.61, 77.21, "India"), "delhi": (28.61, 77.21, "India"),
    "mumbai": (19.08, 72.88, "India"), "kyiv": (50.45, 30.52, "Ukraine"),
    "kiev": (50.45, 30.52, "Ukraine"), "kharkiv": (49.99, 36.23, "Ukraine"),
    "donetsk": (48.02, 37.80, "Ukraine"), "mariupol": (47.10, 37.54, "Ukraine"),
    "odesa": (46.48, 30.72, "Ukraine"), "moscow": (55.75, 37.62, "Russia"),
    "kabul": (34.53, 69.17, "Afghanistan"), "tehran": (35.69, 51.39, "Iran"),
    "baghdad": (33.32, 44.36, "Iraq"), "beirut": (33.89, 35.50, "Lebanon"),
    "damascus": (33.51, 36.29, "Syria"), "aleppo": (36.20, 37.16, "Syria"),
    "istanbul": (41.01, 28.98, "Turkey"), "ankara": (39.93, 32.87, "Turkey"),
    "cairo": (30.04, 31.24, "Egypt"), "khartoum": (15.50, 32.56, "Sudan"),
    "nairobi": (-1.29, 36.82, "Kenya"), "lagos": (6.52, 3.38, "Nigeria"),
    "abuja": (9.06, 7.50, "Nigeria"), "johannesburg": (-26.20, 28.05, "South Africa"),
    "cape town": (-33.92, 18.42, "South Africa"), "addis ababa": (9.03, 38.74, "Ethiopia"),
    "mogadishu": (2.04, 45.34, "Somalia"), "kinshasa": (-4.32, 15.31, "Dem. Rep. Congo"),
    "goma": (-1.68, 29.22, "Dem. Rep. Congo"), "beijing": (39.90, 116.40, "China"),
    "shanghai": (31.23, 121.47, "China"), "hong kong": (22.32, 114.17, "Hong Kong"),
    "taipei": (25.03, 121.57, "Taiwan"), "tokyo": (35.68, 139.69, "Japan"),
    "seoul": (37.57, 126.98, "South Korea"), "pyongyang": (39.02, 125.75, "North Korea"),
    "islamabad": (33.69, 73.06, "Pakistan"), "karachi": (24.86, 67.01, "Pakistan"),
    "dhaka": (23.81, 90.41, "Bangladesh"), "bangkok": (13.76, 100.50, "Thailand"),
    "hanoi": (21.03, 105.85, "Vietnam"), "manila": (14.60, 120.98, "Philippines"),
    "jakarta": (-6.21, 106.85, "Indonesia"), "kuala lumpur": (3.14, 101.69, "Malaysia"),
    "sydney": (-33.87, 151.21, "Australia"), "melbourne": (-37.81, 144.96, "Australia"),
    "canberra": (-35.28, 149.13, "Australia"), "wellington": (-41.29, 174.78, "New Zealand"),
    "auckland": (-36.85, 174.76, "New Zealand"), "washington": (38.90, -77.04, "United States of America"),
    "new york": (40.71, -74.01, "United States of America"), "los angeles": (34.05, -118.24, "United States of America"),
    "london": (51.51, -0.13, "United Kingdom"), "paris": (48.85, 2.35, "France"),
    "berlin": (52.52, 13.40, "Germany"), "brussels": (50.85, 4.35, "Belgium"),
    "geneva": (46.20, 6.14, "Switzerland"), "rome": (41.90, 12.50, "Italy"),
    "madrid": (40.42, -3.70, "Spain"), "warsaw": (52.23, 21.01, "Poland"),
    "athens": (37.98, 23.73, "Greece"), "ottawa": (45.42, -75.70, "Canada"),
    "toronto": (43.65, -79.38, "Canada"), "mexico city": (19.43, -99.13, "Mexico"),
    "sao paulo": (-23.55, -46.63, "Brazil"), "rio de janeiro": (-22.91, -43.17, "Brazil"),
    "brasilia": (-15.79, -47.88, "Brazil"), "buenos aires": (-34.60, -58.38, "Argentina"),
    "caracas": (10.48, -66.90, "Venezuela"), "bogota": (4.71, -74.07, "Colombia"),
    "lima": (-12.05, -77.04, "Peru"), "santiago": (-33.45, -70.67, "Chile"),
    "riyadh": (24.71, 46.68, "Saudi Arabia"), "jeddah": (21.49, 39.19, "Saudi Arabia"),
    "dubai": (25.20, 55.27, "United Arab Emirates"), "abu dhabi": (24.45, 54.38, "United Arab Emirates"),
    "doha": (25.29, 51.53, "Qatar"), "sanaa": (15.35, 44.21, "Yemen"),
}

# nationality adjective -> country name
DEMONYMS = {
    # SHIPPED BUG: "MALIAN and Russian forces reclaim strategic northern town" dotted RUSSIA, because
    # "malian" was not a demonym and so the only actor left standing was the Russian one. A missing
    # demonym does not merely lose a flag — it hands the dot to whoever else is in the sentence.
    "malian": "Mali", "nigerien": "Niger", "burkinabe": "Burkina Faso", "senegalese": "Senegal",
    "ivorian": "Côte d'Ivoire", "cameroonian": "Cameroon", "tanzanian": "Tanzania",
    "zambian": "Zambia", "eritrean": "Eritrea", "kuwaiti": "Kuwait", "omani": "Oman",
    "bahraini": "Bahrain", "jordanian": "Jordan", "georgian": "Georgia", "armenian": "Armenia",
    "azerbaijani": "Azerbaijan", "kazakh": "Kazakhstan", "uzbek": "Uzbekistan",
    "moldovan": "Moldova", "serbian": "Serbia", "croatian": "Croatia",
    "bosnian": "Bosnia and Herzegovina", "albanian": "Albania", "kosovar": "Kosovo",
    "slovak": "Slovakia", "slovenian": "Slovenia", "estonian": "Estonia", "latvian": "Latvia",
    "lithuanian": "Lithuania", "icelandic": "Iceland", "czech": "Czechia", "bolivian": "Bolivia",
    "ecuadorian": "Ecuador", "cuban": "Cuba", "haitian": "Haiti",
    # Non-state actors that ARE a country's story: the Houthis (official name Ansarullah) are Yemen,
    # so "Ansarullah authorities announced…" both sinks as an actor AND vouches Yemen as context.
    "houthi": "Yemen", "houthis": "Yemen", "ansarullah": "Yemen", "ansar allah": "Yemen",
    "hezbollah": "Lebanon", "hamas": "Palestine", "taliban": "Afghanistan",
    # 141 countries had NO demonym at all — including Hungary, which is why "HUNGARIAN defence
    # minister promises…" was dotted on Ukraine. Filling every newsworthy one.
    "hungarian": "Hungary", "bulgarian": "Bulgaria", "romanian": "Romania", "cypriot": "Cyprus",
    "maltese": "Malta", "montenegrin": "Montenegro", "macedonian": "North Macedonia",
    "luxembourgish": "Luxembourg", "taiwanese": "Taiwan", "singaporean": "Singapore",
    "filipino": "Philippines", "malaysian": "Malaysia", "cambodian": "Cambodia",
    "laotian": "Laos", "burmese": "Myanmar", "nepali": "Nepal", "nepalese": "Nepal",
    "sri lankan": "Sri Lanka", "mongolian": "Mongolia", "kyrgyz": "Kyrgyzstan",
    "tajik": "Tajikistan", "turkmen": "Turkmenistan", "bhutanese": "Bhutan",
    "maldivian": "Maldives", "bruneian": "Brunei",
    "angolan": "Angola", "beninese": "Benin", "botswanan": "Botswana", "burundian": "Burundi",
    "chadian": "Chad", "djiboutian": "Djibouti", "gabonese": "Gabon", "gambian": "Gambia",
    "guinean": "Guinea", "liberian": "Liberia", "malagasy": "Madagascar",
    "malawian": "Malawi", "mauritanian": "Mauritania", "mozambican": "Mozambique",
    "namibian": "Namibia", "rwandan": "Rwanda", "sierra leonean": "Sierra Leone",
    "togolese": "Togo", "guatemalan": "Guatemala", "honduran": "Honduras",
    "nicaraguan": "Nicaragua", "panamanian": "Panama", "paraguayan": "Paraguay",
    "uruguayan": "Uruguay", "salvadoran": "El Salvador", "jamaican": "Jamaica",
    "guyanese": "Guyana", "surinamese": "Suriname", "costa rican": "Costa Rica",
    "fijian": "Fiji", "new zealander": "New Zealand",
    "greenlandic": "Greenland", "greenlander": "Greenland", "icelander": "Iceland",
    "kosovan": "Kosovo", "emirati": "United Arab Emirates", "qatari": "Qatar",
    "yemeni": "Yemen", "somalian": "Somalia", "sudanese": "Sudan", "libyan": "Libya",
    "tunisian": "Tunisia", "algerian": "Algeria", "moroccan": "Morocco",
    "american": "United States of America", "british": "United Kingdom", "french": "France",
    "german": "Germany", "italian": "Italy", "spanish": "Spain", "portuguese": "Portugal",
    "dutch": "Netherlands", "belgian": "Belgium", "swiss": "Switzerland", "austrian": "Austria",
    "irish": "Ireland", "polish": "Poland", "greek": "Greece", "swedish": "Sweden",
    "norwegian": "Norway", "finnish": "Finland", "danish": "Denmark", "russian": "Russia",
    "ukrainian": "Ukraine", "belarusian": "Belarus", "turkish": "Turkey", "chinese": "China",
    "japanese": "Japan", "taiwanese": "Taiwan", "indian": "India", "pakistani": "Pakistan",
    "bangladeshi": "Bangladesh", "afghan": "Afghanistan", "iranian": "Iran", "iraqi": "Iraq",
    "syrian": "Syria", "lebanese": "Lebanon", "israeli": "Israel", "palestinian": "Palestine",
    "saudi": "Saudi Arabia", "emirati": "United Arab Emirates", "qatari": "Qatar",
    "yemeni": "Yemen", "egyptian": "Egypt", "libyan": "Libya", "tunisian": "Tunisia",
    "algerian": "Algeria", "moroccan": "Morocco", "sudanese": "Sudan", "ethiopian": "Ethiopia",
    "somali": "Somalia", "kenyan": "Kenya", "ugandan": "Uganda", "nigerian": "Nigeria",
    "ghanaian": "Ghana", "congolese": "Dem. Rep. Congo", "zimbabwean": "Zimbabwe",
    "south african": "South Africa", "brazilian": "Brazil", "mexican": "Mexico",
    "canadian": "Canada", "australian": "Australia", "venezuelan": "Venezuela",
    "colombian": "Colombia", "argentine": "Argentina", "argentinian": "Argentina",
    "peruvian": "Peru", "chilean": "Chile", "thai": "Thailand", "vietnamese": "Vietnam",
    "filipino": "Philippines", "indonesian": "Indonesia", "malaysian": "Malaysia",
    "burmese": "Myanmar", "nepali": "Nepal",
}

# searchable country names/aliases -> canonical name in COUNTRY_COORDS
COUNTRY_ALIASES = {}
for _k in COUNTRY_COORDS:
    COUNTRY_ALIASES[re.sub(r"[^a-z ]", " ", _k.lower()).strip()] = _k
COUNTRY_ALIASES.update({
    "united states": "United States of America", "us": "United States of America",
    "u s": "United States of America", "usa": "United States of America",
    "america": "United States of America", "britain": "United Kingdom",
    "uk": "United Kingdom", "u k": "United Kingdom", "dr congo": "Dem. Rep. Congo",
    "drc": "Dem. Rep. Congo", "uae": "United Arab Emirates", "emirates": "United Arab Emirates",
    "ivory coast": "Côte d'Ivoire",
    # the names outlets ACTUALLY print. "Türkiye" folds to "turkiye" before it reaches this table;
    # without the entry, a story whose only actor was Türkiye had NO country at all.
    # SHIPPED: "NZ's South Island struck by magnitude-5.9 earthquake" fell back to the PUBLISHER
    # (abc.net.au -> Australia) because "nz" was not an alias for anything.
    "nz": "New Zealand", "aotearoa": "New Zealand", "uae": "United Arab Emirates",
    "turkiye": "Turkey", "turkey": "Turkey", "czech republic": "Czechia", "holland": "Netherlands",
    "burma": "Myanmar", "swaziland": "eSwatini", "cape verde": "Cabo Verde",
    "south korea": "South Korea", "north korea": "North Korea", "the gambia": "Gambia",
})

# GDELT sourcecountry name -> canonical name (for the outlet-country fallback)
GDELT_COUNTRY = {
    "United States": "United States of America", "United Kingdom": "United Kingdom",
    "Congo DRC": "Dem. Rep. Congo", "Congo Republic": "Congo", "South Sudan": "S. Sudan",
    "Ivory Coast": "Côte d'Ivoire", "Czech Republic": "Czechia",
}

_CITY_KEYS = sorted(CITY_COORDS, key=len, reverse=True)   # curated cities only (used by _involved_countries)
_COUNTRY_ALIAS_KEYS = sorted(COUNTRY_ALIASES, key=len, reverse=True)
_DEMONYM_KEYS = sorted(DEMONYMS, key=len, reverse=True)

# ---- world cities gazetteer: ~31k GeoNames cities so news lands on the SPECIFIC city, not the capital.
# Curated CITY_COORDS above win on conflicts. Minor single-word towns are "weak" — they only count when the
# headline gives locational context ("in Omsk", "strike on Toretsk") to avoid false hits on common words.
_WEAK_CITIES = set()
# common/idiom words that are also minor town names — never treat these as a location
_BAD_CITY_NAMES = {"maga", "potus", "flotus", "scotus", "nato", "opec", "brics", "gop", "antifa",
                   "hamas", "isis", "daesh", "taliban", "hezbollah", "houthi", "wagner",
                   # A CONTINENT IS NOT A TOWN. SHIPPED: "African growth boom … across ASIA and
                   # Africa" was dotted on Asia, PHILIPPINES — a real town of 23,546. A continent
                   # names a whole hemisphere; it can never be the scene of one event.
                   "asia", "africa", "europe", "america", "americas", "oceania", "antarctica",
                   "eurasia", "arctic", "scandinavia", "levant", "maghreb", "sahel", "balkans",
                   # COMPANIES that GeoNames also lists as towns. SHIPPED: "…taken to court by energy
                   # giant WOODSIDE" (an Australian company) was dotted on Woodside, California.
                   "woodside", "santos", "orica", "telstra", "optus", "qantas", "bhp", "rio tinto",
                   "shell", "chevron", "phillips", "halliburton", "raytheon", "boeing", "lockheed",
                   # ordinary words that GeoNames also lists as small towns. SHIPPED: "YOUNG Germans
                   # opting out of military service" -> Young, URUGUAY; "PARAMOUNT's Warner takeover"
                   # -> Paramount, California. A sentence-initial word is not a dateline.
                   "young", "paramount", "eagle", "sandy", "mobile", "reading", "bath", "brave",
                   "hope", "normal", "boring", "why", "sale", "deal", "coach", "major", "captain",
                   "junior", "senior", "bank", "battle", "harmony", "concord", "liberal",
                   "defiance", "charge", "honor", "honour", "memory", "response", "solidarity", "support",
                   "custody", "detention", "protest", "protests", "siege", "crisis", "peace", "defense",
                   "defence", "office", "court", "power", "hope", "union", "mission", "progress", "liberty",
                   "independence", "freedom", "victory", "glory", "pride", "surprise", "general", "industry",
                   "enterprise", "commerce", "finance", "summit", "talks", "recovery", "relief", "eden",
                   "paradise", "fortune", "hazard", "boom", "surprise", "advance", "triumph", "climax",
                   # SHIPPED: "House Republicans resurrect SAVE America Act" -> Save, BENIN. Common
                   # verbs/nouns GeoNames also lists as tiny towns, that keep turning up inside a bill
                   # or proper-noun phrase.
                   "save", "america", "liberty", "freedom", "surprise", "protection", "security",
                   "opportunity", "prosperity", "accountability", "unity", "aurora", "energy"}
_MANUAL_PLACES = {   # regions/nicknames GeoNames doesn't list as a city
    "silicon valley": (37.387, -122.058, "United States of America"),
    "wall street": (40.706, -74.009, "United States of America"),
    "hollywood": (34.098, -118.327, "United States of America"),
    "west bank": (31.95, 35.3, "Palestine"),
    "gaza strip": (31.42, 34.35, "Palestine"),
    "donbas": (48.5, 37.8, "Ukraine"),
    "crimea": (45.3, 34.4, "Ukraine"),
    "strait of hormuz": (26.57, 56.25, "Iran"),
    "hormuz": (26.57, 56.25, "Iran"),
    "suez canal": (30.42, 32.35, "Egypt"),
    "bosphorus": (41.12, 29.07, "Turkey"),
    "dardanelles": (40.22, 26.40, "Turkey"),
    "strait of gibraltar": (35.95, -5.60, "Spain"),
    "bab el mandeb": (12.58, 43.33, "Yemen"),
    "taiwan strait": (24.50, 119.50, "Taiwan"),
    "english channel": (50.30, 0.30, "France"),
    "strait of malacca": (2.50, 101.00, "Malaysia"),
    "panama canal": (9.08, -79.68, "Panama"),
    "red sea": (20.00, 38.50, "Saudi Arabia"),
    "south china sea": (13.00, 114.00, "Philippines"),
    "persian gulf": (26.50, 51.50, "Iran"),
    "gulf of aden": (12.50, 47.50, "Yemen"),
}
# Russian/older transliterations of Ukrainian places. Without these, TASS/RT copy either fails to
# geolocate or lands in the wrong country entirely ("Odessa" -> Odessa, TEXAS). Coordinates are the
# real (Ukrainian) ones — the map places events by geography, so Russian-occupied Ukrainian land
# still plots inside Ukraine.
_PLACE_ALIASES = {
    # Apostrophe-transliterated names tokenise with the apostrophe as a SPACE, so register the spaced
    # form. Bare "Sana'a" (no "airport") was landing on Sana, PERU; "Ta'izz"/"Ma'rib" resolved to
    # nothing at all. Yemen is a live front (Saudi strikes), so these come up constantly.
    "sana a": (15.348, 44.207, "Yemen"), "sanaa": (15.348, 44.207, "Yemen"),
    "ta izz": (13.578, 44.020, "Yemen"), "taizz": (13.578, 44.020, "Yemen"),
    "ma rib": (15.470, 45.323, "Yemen"), "marib": (15.470, 45.323, "Yemen"),
    "hodeidah": (14.802, 42.954, "Yemen"), "hudaydah": (14.802, 42.954, "Yemen"),
    "saada": (16.940, 43.764, "Yemen"), "sa dah": (16.940, 43.764, "Yemen"),
    # SHIPPED BUG: "KIEV" — the spelling RT and TASS use in every single story — resolved to NOTHING.
    # It sat in the display table but was never loaded into the place scanner, so a story whose
    # summary read "…storage sites in Kiev" had no Kyiv hit at all.
    "kiev": (50.45, 30.52, "Ukraine"),
    # Ukraine's Black Sea ports, by the names the wire actually prints
    "yuzhny": (46.62, 31.10, "Ukraine"), "pivdennyi": (46.62, 31.10, "Ukraine"),
    "yuzhne": (46.62, 31.10, "Ukraine"), "chernomorsk": (46.30, 30.65, "Ukraine"),
    "chornomorsk": (46.30, 30.65, "Ukraine"), "ilyichevsk": (46.30, 30.65, "Ukraine"),
    "izmail": (45.35, 28.84, "Ukraine"), "reni": (45.46, 28.28, "Ukraine"),
    "ochakov": (46.61, 31.55, "Ukraine"), "ochakiv": (46.61, 31.55, "Ukraine"),
    "zaporozhye": (47.84, 35.14, "Ukraine"), "zaporizhzhia": (47.84, 35.14, "Ukraine"),
    "zaporizhia": (47.84, 35.14, "Ukraine"), "zaporozhia": (47.84, 35.14, "Ukraine"),
    "kharkov": (49.99, 36.23, "Ukraine"), "odessa": (46.48, 30.73, "Ukraine"),
    "nikolaev": (46.98, 31.99, "Ukraine"), "mykolayiv": (46.98, 31.99, "Ukraine"),
    "lugansk": (48.57, 39.31, "Ukraine"), "luhansk": (48.57, 39.31, "Ukraine"),
    "dnepropetrovsk": (48.46, 35.05, "Ukraine"), "dnipropetrovsk": (48.46, 35.05, "Ukraine"),
    "chernigov": (51.50, 31.29, "Ukraine"), "vinnitsa": (49.23, 28.47, "Ukraine"),
    "zhitomir": (50.25, 28.66, "Ukraine"), "slavyansk": (48.87, 37.60, "Ukraine"),
    "sloviansk": (48.87, 37.60, "Ukraine"), "artemovsk": (48.60, 38.00, "Ukraine"),
    "bakhmut": (48.60, 38.00, "Ukraine"), "gorlovka": (48.33, 38.05, "Ukraine"),
    "horlivka": (48.33, 38.05, "Ukraine"), "krasnoarmeysk": (48.28, 37.18, "Ukraine"),
    "pokrovsk": (48.28, 37.18, "Ukraine"), "ugledar": (47.78, 37.25, "Ukraine"),
    "vuhledar": (47.78, 37.25, "Ukraine"), "avdeevka": (48.14, 37.75, "Ukraine"),
    "avdiivka": (48.14, 37.75, "Ukraine"), "kupyansk": (49.71, 37.62, "Ukraine"),
    "kupiansk": (49.71, 37.62, "Ukraine"), "izyum": (49.21, 37.25, "Ukraine"),
    "izium": (49.21, 37.25, "Ukraine"), "konstantinovka": (48.53, 37.72, "Ukraine"),
    "kostiantynivka": (48.53, 37.72, "Ukraine"), "energodar": (47.50, 34.65, "Ukraine"),
    "enerhodar": (47.50, 34.65, "Ukraine"), "severodonetsk": (48.95, 38.49, "Ukraine"),
    "sievierodonetsk": (48.95, 38.49, "Ukraine"), "lisichansk": (48.90, 38.43, "Ukraine"),
    "lysychansk": (48.90, 38.43, "Ukraine"), "kamenka dneprovskaya": (47.48, 34.40, "Ukraine"),
    "kamianka dniprovska": (47.48, 34.40, "Ukraine"), "chasov yar": (48.59, 37.83, "Ukraine"),
    "toretsk": (48.40, 37.85, "Ukraine"), "kherson": (46.64, 32.61, "Ukraine"),
    "sovetsky": (45.34, 34.92, "Ukraine"), "dzhankoi": (45.71, 34.39, "Ukraine"),
    "dzhankoy": (45.71, 34.39, "Ukraine"), "yevpatoria": (45.20, 33.37, "Ukraine"),
    "feodosia": (45.03, 35.38, "Ukraine"), "kerch": (45.36, 36.47, "Ukraine"),
    "armyansk": (46.11, 33.69, "Ukraine"), "krasnoperekopsk": (45.95, 33.79, "Ukraine"),
    "bakhchisaray": (44.75, 33.86, "Ukraine"), "saky": (45.13, 33.60, "Ukraine"),
    "stakhanov": (48.57, 38.64, "Ukraine"), "alchevsk": (48.47, 38.80, "Ukraine"),
    "debaltseve": (48.34, 38.41, "Ukraine"), "makiivka": (48.05, 37.96, "Ukraine"),
    "yenakiieve": (48.23, 38.21, "Ukraine"), "lutugine": (48.41, 39.20, "Ukraine"),
}
_US = "United States of America"
_REGIONS = {
    # US states — news says "flooding in Missouri", and a state beats a same-named foreign town
    "alabama": (32.8, -86.8, _US), "alaska": (64.0, -152.0, _US), "arizona": (34.3, -111.7, _US),
    "arkansas": (34.9, -92.4, _US), "california": (37.2, -119.5, _US), "colorado": (39.0, -105.5, _US),
    "connecticut": (41.6, -72.7, _US), "delaware": (39.0, -75.5, _US), "florida": (28.6, -82.4, _US),
    "hawaii": (20.3, -156.4, _US), "idaho": (44.4, -114.6, _US), "illinois": (40.0, -89.2, _US),
    "indiana": (39.9, -86.3, _US), "iowa": (42.1, -93.5, _US), "kansas": (38.5, -98.4, _US),
    "kentucky": (37.5, -85.3, _US), "louisiana": (31.0, -92.0, _US), "maine": (45.4, -69.2, _US),
    "maryland": (39.0, -76.8, _US), "massachusetts": (42.3, -71.8, _US), "michigan": (44.3, -85.4, _US),
    "minnesota": (46.3, -94.3, _US), "mississippi": (32.7, -89.7, _US), "missouri": (38.4, -92.5, _US),
    "montana": (47.0, -109.6, _US), "nebraska": (41.5, -99.8, _US), "nevada": (39.3, -116.6, _US),
    "new hampshire": (43.7, -71.6, _US), "new jersey": (40.2, -74.7, _US), "new mexico": (34.4, -106.1, _US),
    "north carolina": (35.5, -79.4, _US), "north dakota": (47.4, -100.5, _US), "ohio": (40.3, -82.8, _US),
    "oklahoma": (35.6, -97.5, _US), "oregon": (43.9, -120.6, _US), "pennsylvania": (40.9, -77.8, _US),
    "rhode island": (41.7, -71.6, _US), "south carolina": (33.9, -80.9, _US), "south dakota": (44.4, -100.2, _US),
    "tennessee": (35.8, -86.4, _US), "texas": (31.5, -99.3, _US), "utah": (39.3, -111.7, _US),
    "vermont": (44.1, -72.7, _US), "virginia": (37.5, -78.9, _US), "west virginia": (38.6, -80.6, _US),
    "wisconsin": (44.6, -89.7, _US), "wyoming": (43.0, -107.6, _US),
    "georgia": (32.16, -82.9, _US),          # resolved by context vs the country
    "washington state": (47.4, -120.5, _US), "new york state": (42.9, -75.5, _US),
    # UK nations — "England" previously matched nothing at all
    "england": (52.5, -1.5, "United Kingdom"), "scotland": (56.5, -4.2, "United Kingdom"),
    "wales": (52.3, -3.7, "United Kingdom"), "northern ireland": (54.6, -6.5, "United Kingdom"),
    # Canada / Australia
    "ontario": (50.0, -85.0, "Canada"), "quebec": (52.0, -71.5, "Canada"), "alberta": (55.0, -115.0, "Canada"),
    "british columbia": (54.0, -125.0, "Canada"), "manitoba": (54.0, -97.0, "Canada"),
    "saskatchewan": (54.0, -106.0, "Canada"), "nova scotia": (45.0, -63.0, "Canada"),
    "new south wales": (-32.0, 147.0, "Australia"), "queensland": (-22.0, 144.0, "Australia"),
    "western australia": (-25.0, 122.0, "Australia"), "south australia": (-30.0, 135.0, "Australia"),
    "tasmania": (-42.0, 147.0, "Australia"),
}


# Publicly-documented infrastructure that news reports name directly. A strike is reported as hitting
# "the Syzran oil refinery", not "Syzran" — there is exactly one, so the dot can sit on the site itself
# instead of the city centre. Multi-word keys, so the n-gram scan matches them before the bare city.
_FACILITIES = {
    # Russian refineries / terminals / depots
    "syzran oil refinery": (53.129, 48.505, "Russia"), "syzran refinery": (53.129, 48.505, "Russia"),
    "novokuibyshevsk refinery": (53.099, 49.943, "Russia"),
    "ryazan oil refinery": (54.554, 39.663, "Russia"), "ryazan refinery": (54.554, 39.663, "Russia"),
    "volgograd refinery": (48.606, 44.606, "Russia"), "tuapse refinery": (44.094, 39.073, "Russia"),
    "kirishi refinery": (59.443, 32.055, "Russia"), "afipsky refinery": (44.902, 38.841, "Russia"),
    "ilsky refinery": (44.851, 38.573, "Russia"), "novoshakhtinsk refinery": (47.784, 39.934, "Russia"),
    "saratov refinery": (51.503, 46.052, "Russia"), "slavyansk refinery": (45.261, 38.131, "Russia"),
    "omsk oil refinery": (54.985, 73.516, "Russia"), "omsk refinery": (54.985, 73.516, "Russia"),
    "nizhnekamsk refinery": (55.700, 51.851, "Russia"), "taneco refinery": (55.700, 51.851, "Russia"),
    "tvernefteprodukt": (56.861, 35.922, "Russia"), "ust luga": (59.671, 28.303, "Russia"),
    "primorsk port": (60.362, 28.611, "Russia"), "novorossiysk port": (44.722, 37.789, "Russia"),
    "engels air base": (51.481, 46.211, "Russia"), "engels airbase": (51.481, 46.211, "Russia"),
    "olenya air base": (68.152, 33.464, "Russia"), "belaya air base": (52.915, 103.605, "Russia"),
    "morozovsk air base": (48.308, 41.791, "Russia"), "millerovo air base": (48.951, 40.302, "Russia"),
    # Ukraine
    "zaporizhzhia nuclear power plant": (47.512, 34.586, "Ukraine"),
    "zaporizhzhia npp": (47.512, 34.586, "Ukraine"), "kakhovka dam": (46.778, 33.369, "Ukraine"),
    "chernobyl": (51.389, 30.099, "Ukraine"), "crimean bridge": (45.311, 36.520, "Ukraine"),
    "kerch bridge": (45.311, 36.520, "Ukraine"), "saky air base": (45.093, 33.599, "Ukraine"),
    # Middle East
    "shuwaikh port": (29.350, 47.930, "Kuwait"), "al udeid air base": (25.117, 51.315, "Qatar"),
    "ain al asad": (33.785, 42.441, "Iraq"), "muwaffaq salti air base": (32.356, 36.259, "Jordan"),
    "natanz": (33.724, 51.727, "Iran"), "fordow": (34.885, 50.993, "Iran"),
    "kharg island": (29.231, 50.324, "Iran"), "bandar abbas": (27.183, 56.277, "Iran"),
    "ras tanura": (26.644, 50.158, "Saudi Arabia"), "abqaiq": (25.934, 49.671, "Saudi Arabia"),
    "haifa port": (32.826, 35.001, "Israel"), "dimona": (31.070, 35.033, "Israel"),
    "ben gurion airport": (32.011, 34.887, "Israel"), "ben gurion": (32.011, 34.887, "Israel"),
    # seats of power are PLACES. "Trump welcomes the Iraqi PM to the WHITE HOUSE" was a dot on IRAQ.
    "white house": (38.898, -77.037, "United States of America"),
    "the kremlin": (55.752, 37.617, "Russia"),
    "downing street": (51.503, -0.128, "United Kingdom"),
    "elysee palace": (48.870, 2.317, "France"), "capitol hill": (38.890, -77.009, "United States of America"),
    "un headquarters": (40.750, -73.968, "United States of America"),
    "hodeidah port": (14.802, 42.940, "Yemen"), "port sudan": (19.617, 37.216, "Sudan"),
    # An apostrophe is a TOKEN SEPARATOR, so "Sana'a International Airport" tokenises to
    # sana|a|international|airport — the facility key must use that spaced form. SHIPPED BUG: with no
    # facility, the lone token "sana" matched a town of that name in PERU and dotted South America.
    "sana a international airport": (15.476, 44.220, "Yemen"),
    "sanaa international airport": (15.476, 44.220, "Yemen"),
    # Deep-strike targets NOEL_REPORTS names constantly. Every one of these was a dot on a city
    # centre (or nowhere) before — "a refinery in Bashkortostan" resolved to NOTHING, because
    # GeoNames never gave us Ufa, a city of 1.1M.
    "ufa refinery": (54.836, 56.020, "Russia"), "ufaneftekhim": (54.860, 56.021, "Russia"),
    "novoil refinery": (54.836, 56.020, "Russia"), "bashneft refinery": (54.836, 56.020, "Russia"),
    "salavat refinery": (53.359, 55.924, "Russia"),
    "gazprom neftekhim salavat": (53.359, 55.924, "Russia"),
    "sterlitamak refinery": (53.638, 55.953, "Russia"),
    "kstovo refinery": (56.147, 44.199, "Russia"), "lukoil nizhegorodnefteorgsintez": (56.147, 44.199, "Russia"),
    "yaroslavl refinery": (57.535, 39.981, "Russia"), "slavneft yanos": (57.535, 39.981, "Russia"),
    "perm refinery": (57.964, 56.316, "Russia"), "orsk refinery": (51.201, 58.560, "Russia"),
    "achinsk refinery": (56.269, 90.500, "Russia"), "angarsk refinery": (52.545, 103.888, "Russia"),
    "astrakhan gas processing plant": (46.132, 48.108, "Russia"),
    "tikhoretsk oil depot": (45.856, 40.126, "Russia"), "temryuk port": (45.276, 37.383, "Russia"),
    "kavkaz port": (45.336, 36.686, "Russia"), "taman port": (45.209, 36.702, "Russia"),
    "sheskharis": (44.700, 37.800, "Russia"), "sheskharis oil terminal": (44.700, 37.800, "Russia"),
    "feodosia oil terminal": (45.078, 35.396, "Russia"), "yeysk oil depot": (46.712, 38.277, "Russia"),
    "primorsko akhtarsk": (46.046, 38.176, "Russia"),
    "engels 2 air base": (51.481, 46.211, "Russia"), "borisoglebsk air base": (51.366, 42.089, "Russia"),
    "yeysk air base": (46.683, 38.208, "Russia"), "taganrog airfield": (47.198, 38.851, "Russia"),
    "belbek air base": (44.689, 33.571, "Russia"), "kacha air base": (44.775, 33.586, "Russia"),
    "novofedorivka air base": (45.093, 33.599, "Russia"),
}

# TOWNS GeoNames never gave us, that the news names every single day. Each of these resolved to
# NOTHING, so the dot fell back to the whole country/region: "Israeli forces raid JENIN" was a dot
# on the entire West Bank, and every Pokrovsk-sector story lost its town.
_WAR_TOWNS = {
    "ufa": (54.735, 55.958, "Russia"),                       # 1.1M — simply absent
    "jenin": (32.461, 35.300, "Palestine"), "tulkarem": (32.311, 35.028, "Palestine"),
    "khan younis": (31.340, 34.306, "Palestine"), "jabalia": (31.528, 34.483, "Palestine"),
    "deir al balah": (31.418, 34.351, "Palestine"), "deir al-balah": (31.418, 34.351, "Palestine"),
    "chasiv yar": (48.590, 37.836, "Ukraine"), "huliaipole": (47.660, 36.259, "Ukraine"),
    "orikhiv": (47.568, 35.786, "Ukraine"), "siversk": (48.869, 38.104, "Ukraine"),
}

# BROAD AREAS — a town named inside one of these still wins by CONTAINMENT.
_WAR_PLACES = {
    # SHIPPED BUG: "New York becomes first state to…" dotted YORK (a town) because the gazetteer had
    # no "new york"; "Man charged with murder in VICTORIA's east" (ABC Australia) dotted VICTORIA,
    # HONG KONG; "Fighter jet over EAST AZERBAIJAN, northeastern IRAN" dotted the COUNTRY Azerbaijan,
    # though East Azerbaijan is an Iranian province.
    "new york": (40.713, -74.006, "United States of America"),
    "new york city": (40.713, -74.006, "United States of America"),
    "new york state": (42.900, -75.500, "United States of America"),
    "victoria": (-37.020, 144.960, "Australia"),
    "east azerbaijan": (37.900, 46.290, "Iran"), "west azerbaijan": (37.550, 45.070, "Iran"),
    "sistan and baluchestan": (27.530, 60.850, "Iran"),
    "south ossetia": (42.220, 43.970, "Georgia"), "abkhazia": (43.000, 41.020, "Georgia"),
    "bashkortostan": (54.200, 56.500, "Russia"),
    "tatarstan": (55.500, 50.500, "Russia"),
    "krasnodar krai": (45.500, 39.000, "Russia"),
    "leningrad region": (60.000, 32.000, "Russia"),
    "bryansk region": (52.900, 33.500, "Russia"),
    "belgorod region": (50.700, 37.700, "Russia"),
    "kursk region": (51.700, 36.100, "Russia"),
    "rostov region": (47.700, 41.000, "Russia"),
    "voronezh region": (51.000, 40.000, "Russia"),
    "samara region": (53.200, 50.500, "Russia"),
    "ryazan region": (54.400, 40.500, "Russia"),
    "tver region": (57.000, 34.500, "Russia"),
    "novgorod region": (58.300, 32.500, "Russia"),
    "nizhny novgorod region": (55.800, 44.000, "Russia"),
    "orenburg region": (52.000, 55.000, "Russia"),
    "volgograd region": (49.500, 44.000, "Russia"),
    "saratov region": (51.500, 46.500, "Russia"),
    "irkutsk region": (56.000, 105.000, "Russia"),
    # Iranian PROVINCES the war names constantly (areas — a named town inside them still wins)
    "lorestan": (33.500, 48.350, "Iran"), "khuzestan": (31.330, 48.690, "Iran"),
    "sistan and baluchestan": (29.500, 60.900, "Iran"), "hormozgan": (27.500, 56.000, "Iran"),
    "west azerbaijan": (37.500, 45.200, "Iran"), "east azerbaijan": (37.800, 46.600, "Iran"),
    "baluchistan": (28.000, 63.000, "Pakistan"),
}

# Places GeoNames ranks WRONG — the world-famous one loses to a bigger namesake, or is missing.
# SHIPPED: "Singaporean arrested in BALI" was dotted on Bali, INDIA (pop 296,973) because the Balinese
# island — one of the most reported places on earth — is not in the gazetteer at all.
# The Iranian towns are here for the same reason: without them a strike post has NO scene, so the only
# hit left is the ATTACKER ("US airstrikes") and the dot lands on the United States.
_FAMOUS_PLACES = {
    "sirik": (26.492, 57.161, "Iran"), "rask": (26.239, 61.397, "Iran"),
    "urmia": (37.555, 45.076, "Iran"), "khorramabad": (33.487, 48.356, "Iran"),
    "khorram abad": (33.487, 48.356, "Iran"), "dezful": (32.381, 48.401, "Iran"),
    "andimeshk": (32.460, 48.355, "Iran"), "mahshahr": (30.559, 49.198, "Iran"),
    "bandar e mahshahr": (30.559, 49.198, "Iran"), "khvormuj": (28.650, 51.380, "Iran"),
    "saravan": (27.371, 62.334, "Iran"), "qeshm": (26.955, 56.271, "Iran"),
    "kish": (26.558, 53.980, "Iran"), "kish island": (26.558, 53.980, "Iran"),
    "abadan": (30.339, 48.304, "Iran"), "bushehr": (28.969, 50.838, "Iran"),
    "kermanshah": (34.314, 47.065, "Iran"),
    "bali": (-8.409, 115.189, "Indonesia"),
    "java": (-7.500, 110.000, "Indonesia"),
    "sumatra": (-0.589, 101.343, "Indonesia"),
    "borneo": (0.961, 114.554, "Indonesia"),
    "phuket": (7.880, 98.392, "Thailand"),
    "ibiza": (38.909, 1.432, "Spain"),
    "santorini": (36.393, 25.461, "Greece"),
    "mykonos": (37.445, 25.328, "Greece"),
    "maui": (20.798, -156.331, "United States of America"),
    "aleppo": (36.202, 37.134, "Syria"),
    "kandahar": (31.628, 65.738, "Afghanistan"),
    "darfur": (13.000, 25.000, "Sudan"),
    "tigray": (14.000, 38.500, "Ethiopia"),
}
# Ships burn at sea, not on land. "Burning Russian tankers in the SEA OF AZOV" was dotting the CITY of
# Azov because the sea wasn't in the gazetteer at all. Water bodies are named in news constantly
# (Red Sea, Hormuz, Black Sea, Kerch Strait) and they are real event locations. Multi-word keys, so the
# longest-match n-gram picks "sea of azov" over the town "azov".
_WATERS = {
    "sea of azov": (46.10, 36.60, "Ukraine"), "azov sea": (46.10, 36.60, "Ukraine"),
    "black sea": (43.40, 34.30, "Turkey"), "baltic sea": (58.00, 20.00, "Sweden"),
    "north sea": (56.00, 3.00, "United Kingdom"), "mediterranean sea": (35.00, 18.00, "Italy"),
    "adriatic sea": (43.00, 15.50, "Italy"), "aegean sea": (39.00, 25.00, "Greece"),
    "ionian sea": (38.00, 18.50, "Greece"), "tyrrhenian sea": (40.00, 12.00, "Italy"),
    "ligurian sea": (43.50, 8.80, "Italy"), "levantine sea": (33.50, 32.50, "Cyprus"),
    "sea of marmara": (40.70, 28.20, "Turkey"), "marmara sea": (40.70, 28.20, "Turkey"),
    "alboran sea": (36.00, -3.50, "Spain"), "arabian sea": (15.00, 65.00, "India"),
    "caspian sea": (41.50, 50.50, "Kazakhstan"), "caribbean sea": (15.00, -75.00, "Colombia"),
    "east china sea": (29.00, 125.00, "China"), "yellow sea": (35.50, 123.00, "China"),
    "sea of japan": (40.00, 135.00, "Japan"), "sea of okhotsk": (53.00, 148.00, "Russia"),
    "bering sea": (58.00, -178.00, "United States of America"),
    "barents sea": (74.00, 40.00, "Russia"), "kara sea": (75.00, 70.00, "Russia"),
    "laptev sea": (76.00, 125.00, "Russia"), "white sea": (65.50, 37.00, "Russia"),
    "norwegian sea": (68.00, 3.00, "Norway"), "labrador sea": (57.00, -53.00, "Canada"),
    "coral sea": (-18.00, 155.00, "Australia"), "tasman sea": (-38.00, 160.00, "Australia"),
    "andaman sea": (10.00, 96.00, "Thailand"), "java sea": (-5.00, 112.00, "Indonesia"),
    "celebes sea": (3.00, 122.00, "Indonesia"), "sulu sea": (8.00, 120.00, "Philippines"),
    "banda sea": (-5.00, 128.00, "Indonesia"), "timor sea": (-11.00, 128.00, "Australia"),
    "philippine sea": (18.00, 133.00, "Philippines"), "dead sea": (31.50, 35.47, "Israel"),
    "sea of galilee": (32.80, 35.59, "Israel"), "aral sea": (45.00, 60.00, "Kazakhstan"),
    # gulfs & bays
    "gulf of oman": (24.50, 58.50, "Oman"), "gulf of mexico": (25.00, -90.00, "Mexico"),
    "gulf of guinea": (2.00, 3.00, "Nigeria"), "gulf of sidra": (31.50, 18.00, "Libya"),
    "gulf of finland": (60.00, 26.00, "Finland"), "gulf of bothnia": (62.00, 20.00, "Sweden"),
    "gulf of thailand": (10.00, 101.50, "Thailand"), "gulf of suez": (28.50, 33.20, "Egypt"),
    "gulf of aqaba": (28.80, 34.70, "Egypt"), "gulf of alaska": (57.00, -145.00, "United States of America"),
    "gulf of california": (28.00, -112.00, "Mexico"), "bay of bengal": (15.00, 88.00, "India"),
    "bay of biscay": (45.50, -4.00, "France"), "hudson bay": (60.00, -86.00, "Canada"),
    "chesapeake bay": (38.00, -76.20, "United States of America"),
    "arabian gulf": (26.50, 51.50, "Iran"),
    # straits, channels, canals
    "kerch strait": (45.25, 36.55, "Ukraine"), "korea strait": (34.50, 129.00, "South Korea"),
    "bering strait": (65.80, -169.00, "United States of America"),
    "strait of dover": (51.00, 1.50, "France"), "dover strait": (51.00, 1.50, "France"),
    "skagerrak": (57.80, 9.00, "Denmark"), "kattegat": (57.00, 11.30, "Denmark"),
    "kiel canal": (54.20, 9.60, "Germany"), "strait of messina": (38.20, 15.60, "Italy"),
    "bosporus": (41.12, 29.07, "Turkey"),
    # lakes & rivers that get named in conflict reporting
    "lake baikal": (53.50, 108.00, "Russia"), "lake victoria": (-1.00, 33.00, "Tanzania"),
    "lake chad": (13.00, 14.00, "Chad"), "dnieper": (48.50, 34.60, "Ukraine"),
    "euphrates": (34.50, 41.00, "Iraq"), "tigris": (34.00, 44.00, "Iraq"),
}
_WATER_NAMES = set()          # international water -> the label carries no country suffix
_AREA_NAMES = set()           # broad areas (states, oblasts, Crimea) that a named town can refine
_REGION_PRIOR = 5_000_000        # a state/province outranks a town, but a COUNTRY outranks it
_FACILITY_PRIOR = 9_000_000      # a named facility is the most specific thing there is
_COUNTRY_PRIOR = 10 ** 9

CITY_CANDS = {}                  # name -> [(lat, lng, country, prior), ...] strongest first


def _add_cand(name, lat, lng, country, prior, exclusive=False):
    lst = CITY_CANDS.setdefault(name, [])
    if exclusive:
        lst[:] = [c for c in lst if c[2] != country]
    lst.append((lat, lng, country, prior))
    lst.sort(key=lambda c: -c[3])


def _load_city_gazetteer():
    try:
        path = os.path.join(BASE_DIR, "cities_gaz.json")
        cmax = {}
        if os.path.exists(path):
            gaz = json.load(open(path, encoding="utf-8"))
            for name, cands in gaz.items():
                for v in cands:
                    lat, lng, co = v[0], v[1], v[2]
                    pop = v[3] if len(v) > 3 else 0
                    _add_cand(name, lat, lng, co, pop)
                    if co and (co not in cmax or pop > cmax[co][0]):
                        cmax[co] = (pop, lat, lng)
                top = CITY_CANDS[name][0]
                CITY_COORDS[name] = (top[0], top[1], top[2])
                if (" " not in name) and top[3] < 80000:
                    _WEAK_CITIES.add(name)
        for co, (pop, lat, lng) in cmax.items():
            if co not in COUNTRY_COORDS:
                COUNTRY_COORDS[co] = (lat, lng)
            COUNTRY_ALIASES.setdefault(co.lower(), co)
        for name, v in _MANUAL_PLACES.items():
            _add_cand(name, v[0], v[1], v[2], _REGION_PRIOR, exclusive=True)
            CITY_COORDS.setdefault(name, v)
        for name, v in _REGIONS.items():
            _add_cand(name, v[0], v[1], v[2], _REGION_PRIOR, exclusive=True)
            CITY_COORDS[name] = v
            _WEAK_CITIES.discard(name)
        for name, v in _PLACE_ALIASES.items():
            _add_cand(name, v[0], v[1], v[2], _REGION_PRIOR, exclusive=True)
            CITY_COORDS[name] = v
            _WEAK_CITIES.discard(name)
        for name, v in _FACILITIES.items():
            _add_cand(name, v[0], v[1], v[2], _FACILITY_PRIOR, exclusive=True)
            CITY_COORDS[name] = v
            _WEAK_CITIES.discard(name)
        for name, v in _WATERS.items():
            _add_cand(name, v[0], v[1], v[2], _FACILITY_PRIOR, exclusive=True)
            CITY_COORDS[name] = v
            _WEAK_CITIES.discard(name)
            _WATER_NAMES.add(name)
        # Towns are real CITIES (refinable, never an "area"); the oblasts are broad AREAS, so
        # CONTAINMENT can still swap in a town the post names ("a refinery in Bashkortostan" stays on
        # Bashkortostan until the post says Salavat, and then it moves to Salavat).
        for name, v in _WAR_TOWNS.items():
            _add_cand(name, v[0], v[1], v[2], 900_000, exclusive=True)
            CITY_COORDS[name] = v
            _WEAK_CITIES.discard(name)
        # The famous one WINS. Registered at CITY scale (not region) so it stays a specific scene and a
        # facility inside it can still refine the dot — but it outranks any same-named town.
        for name, v in _FAMOUS_PLACES.items():
            _add_cand(name, v[0], v[1], v[2], 3_000_000, exclusive=True)
            CITY_COORDS[name] = v
            _WEAK_CITIES.discard(name)
        for name, v in _WAR_PLACES.items():
            _add_cand(name, v[0], v[1], v[2], _REGION_PRIOR, exclusive=True)
            CITY_COORDS[name] = v
            _WEAK_CITIES.discard(name)
            _AREA_NAMES.add(name)
        _AREA_NAMES.update(_REGIONS.keys())
        _AREA_NAMES.update(("crimea", "donbas", "west bank", "gaza strip", "silicon valley"))
        for name in ("strait of hormuz", "suez canal", "strait of gibraltar", "bab el mandeb",
                     "taiwan strait", "english channel", "strait of malacca", "panama canal",
                     "red sea", "south china sea", "persian gulf", "gulf of aden", "bosphorus",
                     "dardanelles", "hormuz"):
            _WATER_NAMES.add(name)
    except Exception:
        pass
_load_city_gazetteer()


# The article's own section is the single most reliable country hint there is: a Guardian story filed
# under /us-news/ about "Georgia" means the US state, not the country in the Caucasus.
_URL_COUNTRY = (
    ("/us-news", "United States of America"), ("/world/us-canada", "United States of America"),
    ("/news/us", "United States of America"), ("/uk-news", "United Kingdom"),
    ("/news/uk", "United Kingdom"), ("/australia-news", "Australia"),
    ("/world/africa", ""), ("/india", "India"), ("/china", "China"),
    # the desk that filed it. Longer/more specific fragments must come FIRST.
    ("/us-politics", "United States of America"), ("/business/us", "United States of America"),
    ("/singapore", "Singapore"), ("/turkiye", "Turkey"),
    ("abc.net.au", "Australia"), ("/australia/", "Australia"), ("/canada", "Canada"),
    ("/europe/ukraine", "Ukraine"), ("/ukraine", "Ukraine"), ("/israel", "Israel"),
    ("/middleeast/iran", "Iran"), ("/iran", "Iran"), ("/russia", "Russia"),
    ("/germany", "Germany"), ("/france", "France"), ("/japan", "Japan"), ("/korea", "South Korea"),
    ("/pakistan", "Pakistan"), ("/brazil", "Brazil"), ("/mexico", "Mexico"), ("/nigeria", "Nigeria"),
)


def _url_country(url):
    low = (url or "").lower()
    for frag, co in _URL_COUNTRY:
        if co and frag in low:
            return co
    return ""


# Country names that are ALSO a common place elsewhere — they must not vouch for themselves when we
# build the context ("Georgia" in a US school-shooting story is not evidence of the Caucasus country).
def _context_mentions(text, url=""):
    """Which countries this story is about, and WHICH WORD vouched for each. Keeping the source word
    matters: a name must not vouch for itself ("Georgia ... in Georgia" is no evidence of the Caucasus
    country) but it must still vouch for OTHER names ("Tripoli in northern Lebanon" -> Lebanon)."""
    out = []
    hint = _url_country(url)
    if hint:
        out.append((hint, "__url__"))
    words = re.findall(r"[a-z0-9]+", _fold(text or "").lower())
    n, i = len(words), 0
    while i < n:
        step = 1
        for size in (4, 3, 2, 1):
            if i + size > n:
                continue
            gram = " ".join(words[i:i + size])
            co = COUNTRY_ALIASES.get(gram)
            if co and co in COUNTRY_COORDS:
                out.append((co, gram)); step = size; break
            dm = DEMONYMS.get(gram)
            if dm and dm in COUNTRY_COORDS:
                out.append((dm, gram)); step = size; break
        i += step
    # A national LEADER (or "POTUS"/"the White House") named in the story puts THEIR country in context, so
    # an ambiguous name resolves at home: "President Trump in GEORGIA" is the US state, not the Caucasus.
    low = " " + " ".join(words) + " "
    for _nm, _co in _OFFICIAL_COUNTRY.items():
        if _co in COUNTRY_COORDS and (" " + _nm + " ") in low:
            out.append((_co, "__leader__"))
    if " potus " in low or " white house " in low:
        out.append(("United States of America", "__leader__"))
    return out


def _co_short(name):
    return {"United States of America": "United States",
            "United Arab Emirates": "UAE"}.get(name, name)


_GEO_PREP = {"in", "at", "near", "across", "outside", "throughout", "around", "amid", "inside", "over", "above"}
# Capitalised words that legitimately sit in front of a place name ("East Aleppo", "South Sudan")
_DIRECTIONS = {"north", "south", "east", "west", "northern", "southern", "eastern", "western",
               "central", "greater", "upper", "lower", "new", "old", "port", "san", "saint", "st",
               "the", "occupied", "besieged", "downtown", "central"}

# A country used ATTRIBUTIVELY in front of one of these is the ACTOR, exactly like a demonym:
# "U.S. attack targeted the city of Saravan" happens in SARAVAN. SHIPPED BUG: it dotted the US.
# Bare "strike/strikes" is deliberately absent — "Gaza strikes" means strikes ON Gaza, not by it.
_ACTOR_NOUNS = {"attack", "attacks", "airstrike", "airstrikes", "raid", "raids", "offensive",
                "bombing", "forces", "troops", "military", "army", "navy", "marines", "soldiers",
                "jets", "warplanes", "drone", "drones", "missile", "missiles", "commandos",
                "officials", "government", "embassy", "envoy", "ambassador", "president",
                "minister", "delegation", "intelligence", "spokesman", "spokesperson",
                # …and the VERB right after a country subject: "RUSSIA STRUCK the vessel off Odessa"
                # names the attacker. Bare "strike/strikes" is deliberately excluded — in "Gaza
                # strikes will continue" that is a NOUN, and the strikes are ON Gaza, not by it.
                "struck", "attacked", "bombed", "shelled", "launched", "invaded", "downed",
                "raided", "seized", "captured", "stormed", "shelling", "bombarded"}

# A threat/claim BY a named official is news where that official is, not where the target is.
_OFFICIAL_COUNTRY = {
    "trump": "United States of America", "vance": "United States of America",
    "rubio": "United States of America", "hegseth": "United States of America",
    "biden": "United States of America",
    "putin": "Russia", "lavrov": "Russia", "medvedev": "Russia", "peskov": "Russia",
    "zelensky": "Ukraine", "zelenskyy": "Ukraine",
    "netanyahu": "Israel", "khamenei": "Iran", "pezeshkian": "Iran", "araghchi": "Iran",
    "xi jinping": "China", "macron": "France", "starmer": "United Kingdom",
    "merz": "Germany", "scholz": "Germany", "meloni": "Italy",
    "erdogan": "Turkey", "modi": "India", "kim jong un": "North Korea",
    "milei": "Argentina", "lula": "Brazil", "orban": "Hungary",
}
_SAY_VERBS = {"says", "said", "tells", "told", "threatens", "threatened", "warns", "warned",
              "vows", "vowed", "urges", "urged", "calls", "called", "announces", "announced",
              "slams", "slammed", "condemns", "condemned", "rejects", "rejected", "denies",
              "denied", "claims", "claimed", "pledges", "pledged", "demands", "demanded",
              "praises", "praised", "insists", "insisted", "declares", "declared", "accuses",
              "accused", "blasts", "blasted", "hints", "suggests", "signals", "dismisses",
              "dismissed", "backs", "backed", "confirms", "confirmed", "reveals", "revealed",
              # a ceremony/commemoration is a thing a state DOES, in its own capital
              "commemorates", "commemorated", "marks", "marked", "honors", "honored", "honours",
              "honoured", "remembers", "remembered", "mourns", "mourned", "hails", "hailed",
              "celebrates", "celebrated", "summons", "summoned", "expels", "expelled", "recalls",
              "apologises", "apologizes", "unveils", "unveiled", "protests", "protested",
              "promises", "promised", "insists", "repeats", "reiterates", "reiterated"}


# Things a CITY cannot do — if a "place" is doing one of these it's really a person's surname
# ("Bellingham scores twice" = Jude Bellingham, not Bellingham, Washington). Metonymy verbs a city CAN
# do ("Kyiv says", "Moscow denies", "Beijing warns") are deliberately NOT in this list.
_PERSON_VERBS = {"scores", "scored", "nets", "netted", "signs", "signed", "joins", "joined",
                 "retires", "retired", "quits", "resigns", "resigned", "dies", "died", "pleads",
                 "pleaded", "testifies", "apologises", "apologizes", "misses", "missed", "sacked",
                 "elected", "appointed", "arrested", "charged", "convicted", "sentenced", "jailed",
                 "injured", "suspended", "banned", "wed", "married", "born"}

# verbs that take a PERSON as their object — a capitalised word right after one is a name, not a place
_NAME_VERBS = {"kill", "killed", "kills", "murder", "murdered", "murders", "assassinate",
               "assassinated", "behead", "beheaded", "execute", "executed", "name", "named",
               "names", "accuse", "accused", "accuses", "honor", "honour", "honored", "honoured",
               "honors", "mourn", "mourned", "mourns", "arrest", "arrested", "detain", "detained",
               "kidnap", "kidnapped", "abduct", "abducted", "shoot", "shot", "stab", "stabbed",
               "target", "targeted", "sue", "sued", "sues", "praise", "praised"}


# ---------------------------------------------------------------------------
# Named-entity veto. Measured on real headlines, NEITHER approach works alone:
#   * NER alone MISSES places it doesn't know ("Kyiv", "Toretsk" -> nothing) and mislabels
#     ("Omsk oil refinery" -> ORG), and it has no coordinates.
#   * The gazetteer alone knows Omsk/Toretsk/Kyiv exactly, but thinks a surname is a city
#     ("Bellingham scores", "Lindsey Graham dies").
# They fail in opposite directions, so: the GAZETTEER finds the place (authoritative + coords),
# and NER is used only to VETO a hit it recognises as a person. Optional — degrades gracefully.
# ---------------------------------------------------------------------------
_NLP = None
_NLP_TRIED = False


def _nlp():
    global _NLP, _NLP_TRIED
    if not _NLP_TRIED:
        _NLP_TRIED = True
        try:
            import spacy
            _NLP = spacy.load("en_core_web_sm",
                              disable=["parser", "lemmatizer", "tagger", "attribute_ruler"])
        except Exception:
            _NLP = None
    return _NLP


@functools.lru_cache(maxsize=4096)
def _person_spans(text):
    """Character spans NER reads as a PERSON / ORG, used to veto a gazetteer hit."""
    nlp = _nlp()
    if not nlp or not text:
        return ()
    try:
        doc = nlp(text[:600])
        return tuple((e.start_char, e.end_char, e.label_)
                     for e in doc.ents if e.label_ in ("PERSON", "ORG"))
    except Exception:
        return ()


# ── WHO IS THIS? faces for the people a story names ───────────────────────────────────────────
# A face beside a name tells the reader instantly who is being talked about. It is also the easiest
# way to ship a humiliating error, because NAMES ARE COMMON: the wrong John Smith, or a footballer
# in place of a minister, is far worse than no photo. So a photo must survive ALL of:
#   1. the name is a real full name (or a curated head of state), not an NER hallucination;
#   2. Wikipedia has that EXACT page — the title must equal the name, so a redirect to some other
#      person, or a disambiguation page, is refused outright;
#   3. Wikidata says the subject is a HUMAN (P31 = Q5) ...
#   4. ... who HOLDS OR HELD PUBLIC OFFICE (P39), or is a politician/diplomat/officer by occupation.
# (4) is what keeps this to "government officials and people like that". A private citizen who
# shares a name with someone notable gets NO photo, which is the correct outcome.
_OFFICIAL_WIKI = {
    "trump": "Donald Trump", "biden": "Joe Biden", "vance": "JD Vance", "rubio": "Marco Rubio",
    "hegseth": "Pete Hegseth", "putin": "Vladimir Putin", "lavrov": "Sergey Lavrov",
    "medvedev": "Dmitry Medvedev", "peskov": "Dmitry Peskov", "zelensky": "Volodymyr Zelenskyy",
    "zelenskyy": "Volodymyr Zelenskyy", "netanyahu": "Benjamin Netanyahu",
    "khamenei": "Ali Khamenei", "pezeshkian": "Masoud Pezeshkian", "araghchi": "Abbas Araghchi",
    "xi jinping": "Xi Jinping", "macron": "Emmanuel Macron", "starmer": "Keir Starmer",
    "merz": "Friedrich Merz", "scholz": "Olaf Scholz", "meloni": "Giorgia Meloni",
    "erdogan": "Recep Tayyip Erdogan", "modi": "Narendra Modi", "kim jong un": "Kim Jong Un",
    "milei": "Javier Milei", "lula": "Luiz Inacio Lula da Silva", "orban": "Viktor Orban",
    "von der leyen": "Ursula von der Leyen", "guterres": "Antonio Guterres",
    "rutte": "Mark Rutte", "sanchez": "Pedro Sanchez", "carney": "Mark Carney",
    "albanese": "Anthony Albanese", "ishiba": "Shigeru Ishiba", "sharif": "Shehbaz Sharif",
}
_OFFICIAL_WIKI_KEYS = sorted(_OFFICIAL_WIKI, key=len, reverse=True)

# honorifics NER happily swallows into the name span
_HONORIFICS = re.compile(
    r"^(?:president|vice president|senator|sen|rep|representative|congressman|congresswoman|"
    r"prime minister|minister|chancellor|premier|governor|mayor|secretary|ambassador|general|"
    r"gen|colonel|col|admiral|major|captain|judge|justice|dr|mr|mrs|ms|sir|dame|lord|pope|king|"
    r"queen|prince|princess|sheikh|imam|rabbi)\.?\s+", re.I)
# Wikidata occupations that count as a public figure even without a formal "position held"
_PUBLIC_OCC = {"Q82955", "Q193391", "Q189290", "Q30461", "Q372436", "Q2285706", "Q806798",
               "Q116", "Q43845"}   # politician, diplomat, military officer, president, statesperson…


def _wiki_json(url):
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Meridian/1.0 (news map)"})
        with urllib.request.urlopen(req, timeout=8) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def _wikidata_person(qid):
    """(is_human, holds_public_office). Split apart because the two gates want different things:
    the little faces UNDER the headline identify officials, but the HERO picture just needs to be a
    real, correctly-identified human — "Iran says ELON MUSK's Starlink is a target" should show Musk,
    and he holds no office."""
    j = _wiki_json("https://www.wikidata.org/wiki/Special:EntityData/%s.json" % qid)
    try:
        claims = j["entities"][qid]["claims"]
    except Exception:
        return False, False

    def _ids(prop):
        out = []
        for c in claims.get(prop, []):
            try:
                out.append(c["mainsnak"]["datavalue"]["value"]["id"])
            except Exception:
                pass
        return out

    if "Q5" not in _ids("P31"):                      # not a human at all (a band, a ship, a town)
        return False, False
    office = bool(claims.get("P39")) or any(o in _PUBLIC_OCC for o in _ids("P106"))
    return True, office


def _wikidata_public_figure(qid):
    human, office = _wikidata_person(qid)
    return human and office


def _person_card(name, curated=False):
    """One validated face, or None. Cached hard — these answers do not change hour to hour."""
    name = (name or "").strip()
    if not name:
        return None
    cache = os.path.join(CACHE_DIR, "person_" + _slug(name) + ".json")
    if _fresh(cache, 30 * 86400):
        try:
            hit = json.load(open(cache, encoding="utf-8"))
            return hit or None
        except Exception:
            pass
    out = None
    j = _wiki_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                   + urllib.parse.quote(name.replace(" ", "_")))
    if j and j.get("type") == "standard":
        thumb = (j.get("thumbnail") or {}).get("source") or ""
        title = j.get("title") or ""
        qid = ((j.get("wikibase_item")) or "")
        # Gate 2: the page must BE this person. A redirect to a different name means we guessed.
        same = _fold(title).lower() == _fold(name).lower()
        if thumb and qid and (curated or same):
            if curated or _wikidata_public_figure(qid):
                out = {"name": title, "img": thumb,
                       "role": (j.get("description") or "").strip()[:60]}
    try:
        json.dump(out or {}, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


def _name_candidates(title, desc=""):
    """Curated officials by the name a HEADLINE actually uses ("Putin"), plus full-name PERSON spans."""
    text = (title or "") + ". " + (desc or "")[:280]
    picks = []          # [(wiki_name, curated)]

    def _add(nm, cur):
        for existing, _c in picks:
            if _fold(existing).lower() == _fold(nm).lower():
                return
        picks.append((nm, cur))

    low = " " + re.sub(r"[^a-z ]", " ", _fold(text).lower()) + " "
    for key in _OFFICIAL_WIKI_KEYS:
        if (" " + key + " ") in low:
            _add(_OFFICIAL_WIKI[key], True)

    for cs, ce, lab in _person_spans(text):
        if lab != "PERSON":
            continue
        # spaCy hands back "Elon Musk's" — the possessive and all — so the lookup for "Elon Musk"
        # never happened and the picture fell all the way back to a photo of Iran.
        nm = re.sub(r"[’']s$", "", text[cs:ce].strip(" '\"’,.")).strip()
        nm = _HONORIFICS.sub("", nm)
        toks = nm.split()
        # a bare surname is ambiguous by construction — only a FULL name may be looked up
        if not (2 <= len(toks) <= 4):
            continue
        if not all(re.match(r"^[A-Z][A-Za-z'\-\.]*$", _fold(t)) for t in toks):
            continue
        _add(nm, False)
    return picks[:3]


# A global company is headquartered SOMEWHERE, and that beats the publisher every time. SHIPPED BUG:
# CNA (Singapore) carried Uber/Meta/Delivery Hero stories and every one of them was dotted on
# SINGAPORE, because the outlet fallback is the last thing standing when a headline names no place.
_ORG_COUNTRY = {
    "uber": "United States of America", "meta": "United States of America",
    "facebook": "United States of America", "google": "United States of America",
    "alphabet": "United States of America", "apple": "United States of America",
    "microsoft": "United States of America", "amazon": "United States of America",
    "tesla": "United States of America", "openai": "United States of America",
    "anthropic": "United States of America", "nvidia": "United States of America",
    "ibm": "United States of America", "boeing": "United States of America",
    "spacex": "United States of America", "bloomberg": "United States of America",
    "paramount": "United States of America", "warner": "United States of America",
    "netflix": "United States of America", "disney": "United States of America",
    "goldman sachs": "United States of America", "jpmorgan": "United States of America",
    "federal reserve": "United States of America", "the fed": "United States of America",
    "wall street": "United States of America", "pentagon": "United States of America",
    "delivery hero": "Germany", "siemens": "Germany", "volkswagen": "Germany", "bmw": "Germany",
    "rheinmetall": "Germany", "nestle": "Switzerland", "novartis": "Switzerland",
    "samsung": "South Korea", "hyundai": "South Korea", "tsmc": "Taiwan",
    "toyota": "Japan", "sony": "Japan", "nintendo": "Japan", "alibaba": "China",
    "huawei": "China", "tencent": "China", "bytedance": "China", "tiktok": "China",
    "aramco": "Saudi Arabia", "gazprom": "Russia", "rosneft": "Russia", "lukoil": "Russia",
    # NB: "Shell" the oil major is deliberately NOT here — "shell" is our most basic war verb
    # ("Russian forces SHELL Toretsk"), and an org table must never fight the security classifier.
    "hsbc": "United Kingdom", "barclays": "United Kingdom", "rolls-royce": "United Kingdom",
    "airbus": "France", "totalenergies": "France", "lvmh": "France",
    # Australian companies GeoNames also lists as US towns — blacklisted as cities, placed here
    "woodside": "Australia", "santos": "Australia", "bhp": "Australia", "rio tinto": "Australia",
    "qantas": "Australia", "telstra": "Australia", "optus": "Australia", "orica": "Australia",
    "european union": "Belgium", "european commission": "Belgium", "eu": "Belgium",
    "nato": "Belgium", "united nations": "United States of America",
    # An armed group HAS a theatre. "How jihadist groups like BOKO HARAM use AI" was dotted on
    # CAMBRIDGE, UK — scraped from a researcher quoted in the summary.
    "boko haram": "Nigeria", "iswap": "Nigeria", "al-shabaab": "Somalia", "al shabaab": "Somalia",
    "jnim": "Mali", "houthi": "Yemen", "houthis": "Yemen", "ansarallah": "Yemen",
    "hezbollah": "Lebanon", "hamas": "Palestine", "islamic jihad": "Palestine",
    "wagner": "Russia", "irgc": "Iran", "taliban": "Afghanistan", "isis-k": "Afghanistan",
}
_ORG_KEYS = sorted(_ORG_COUNTRY, key=len, reverse=True)


def _org_country(title):
    low = " " + re.sub(r"[^a-z ]", " ", _fold(title or "").lower()) + " "
    for k in _ORG_KEYS:
        if (" " + k + " ") in low:
            return _ORG_COUNTRY[k]
    return None


# Public figures who hold NO office, so the officials table must not contain them — putting Musk in
# _OFFICIAL_WIKI would let the nationality fallback read "South African-American businessman" and drag
# an Iran story to South Africa. These are for the HERO PICTURE only, by the name a headline uses.
_FAMOUS_WIKI = {
    "musk": "Elon Musk", "elon musk": "Elon Musk", "buffett": "Warren Buffett",
    "bezos": "Jeff Bezos", "zuckerberg": "Mark Zuckerberg", "gates": "Bill Gates",
    "altman": "Sam Altman", "thunberg": "Greta Thunberg", "epstein": "Jeffrey Epstein",
    "soros": "George Soros", "murdoch": "Rupert Murdoch", "kushner": "Jared Kushner",
    "hassabis": "Demis Hassabis", "huang": "Jensen Huang", "dimon": "Jamie Dimon",
    "ronaldo": "Cristiano Ronaldo", "messi": "Lionel Messi", "mbappe": "Kylian Mbappe",
    "pogacar": "Tadej Pogacar", "mcilroy": "Rory McIlroy", "maradona": "Diego Maradona",
    "navalny": "Alexei Navalny", "assange": "Julian Assange", "snowden": "Edward Snowden",
}
_FAMOUS_KEYS = sorted(_FAMOUS_WIKI, key=len, reverse=True)


def _hero_person(title, desc=""):
    """The PERSON a story is about, for the hero picture. Looser than the little faces under the
    headline (no public office required — Elon Musk holds none) but the gate that actually prevents a
    WRONG face is untouched: a full name, an EXACT Wikipedia title match, and Wikidata saying human.
    "Michael Brown scores twice" still gets nothing — that name is a disambiguation page."""
    cands = list(_name_candidates(title, desc))
    low = " " + re.sub(r"[^a-z ]", " ", _fold((title or "") + " " + (desc or "")[:200]).lower()) + " "
    # LEFTMOST WINS — the subject of the sentence. "BUFFETT severs donations to the GATES Foundation"
    # is a story about Buffett; matching on table order put Bill Gates's face on it.
    famous = []
    for k in _FAMOUS_KEYS:
        at = low.find(" " + k + " ")
        if at >= 0:
            nm = _FAMOUS_WIKI[k]
            if not any(_fold(c[0]).lower() == _fold(nm).lower() for c in cands):
                famous.append((at, nm))
    for _at, nm in sorted(famous, reverse=True):
        cands.insert(0, (nm, True))
    for name, curated in cands[:4]:
        cache = os.path.join(CACHE_DIR, "heropic_" + _slug(name) + ".json")
        if _fresh(cache, 30 * 86400):
            try:
                hit = json.load(open(cache, encoding="utf-8"))
                if hit:
                    return hit
                continue
            except Exception:
                pass
        out = None
        j = _wiki_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                       + urllib.parse.quote(name.replace(" ", "_")))
        if j and j.get("type") == "standard":
            img = ((j.get("originalimage") or {}).get("source")
                   or (j.get("thumbnail") or {}).get("source") or "")
            qid, wtitle = j.get("wikibase_item") or "", j.get("title") or ""
            same = _fold(wtitle).lower() == _fold(name).lower()
            if img and qid and (curated or same):
                human, _office = _wikidata_person(qid)
                if curated or human:
                    out = {"url": re.sub(r"/\d+px-([^/]*)$", r"/1280px-\1", img), "title": wtitle}
        try:
            json.dump(out or {}, open(cache, "w", encoding="utf-8"))
        except Exception:
            pass
        if out:
            return out
    return None


def _subject_country(title, desc=""):
    """The country of the PUBLIC OFFICIAL a story is about — used before we would ever fall back to
    the publisher's home country.
    SHIPPED BUG: RT ran two Lindsey Graham stories; with no place in the headline both fell back to
    the OUTLET and were dotted on RUSSIA. A US senator's story is US news whoever prints it.
    Reuses the person gate (Wikidata human + holds public office), so only officials can move a dot —
    and reads the country straight out of Wikipedia's own description: "American politician",
    "President of Ukraine since 2019"."""
    for card in _story_people(title, desc):
        role = _fold(card.get("role") or "").lower()
        if not role:
            continue
        words = re.findall(r"[a-z]+", role)
        for w in words:                                    # "AMERICAN lawyer and politician"
            co = DEMONYMS.get(w)
            if co and co in COUNTRY_COORDS:
                return co
        for size in (3, 2, 1):                             # "President of UNITED STATES"
            for i in range(len(words) - size + 1):
                co = COUNTRY_ALIASES.get(" ".join(words[i:i + size]))
                if co and co in COUNTRY_COORDS:
                    return co
    return None


def _story_people(title, desc=""):
    picks = _name_candidates(title, desc)
    if not picks:
        return []
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as ex:
            cards = list(ex.map(lambda p: _person_card(p[0], p[1]), picks))
    except Exception:
        cards = [_person_card(n, c) for n, c in picks]
    return [c for c in cards if c]


def _ner_vetoes(spans, cs, ce, weak, supported, located):
    """NER is useful but noisy — it tags a sentence-initial "Valencia" as a PERSON. Two signals make it
    trustworthy:
      * span WIDTH: if the entity covers more than this word it is a full name ("Lindsey Graham",
        "Tilly Norwood") -> certainly a person, veto outright.
      * CONTEXT: a lone capitalised name is only vetoed when the story gives no support for it being a
        place. "Valencia floods ... in eastern SPAIN" supports Valencia; "Bellingham scores ... past
        NORWAY" does not support Bellingham, Washington.
    `supported` = the country this name resolved to is actually named in the story."""
    for (s, e, lab) in spans:
        if cs < e and s < ce:                       # character overlap
            covers_more = (s < cs or e > ce)
            if lab == "PERSON":
                if covers_more:
                    return True          # a full name ("Lindsey Graham") — certainly a person
                if not supported and not located:
                    return True          # a lone capitalised name with nothing backing it up
            elif lab == "ORG" and weak and not supported and not located:
                return True
    return False


def _statement_country(words):
    """'Trump threatens to decimate Iran' -> United States (nothing has happened in Iran yet).
    Only fires when the official is the SUBJECT (near the start) and a saying-verb follows."""
    n = len(words)
    for j in range(0, min(4, n)):
        for size in (3, 2, 1):
            if j + size > n:
                continue
            co = _OFFICIAL_COUNTRY.get(" ".join(words[j:j + size]))
            if co:
                for k in range(j + size, min(j + size + 3, n)):
                    if words[k] in _SAY_VERBS:
                        return co
    return None


# An official who SPEAKS/TESTIFIES is acting from their own seat — the country they name is the topic,
# not the scene. "Hegseth testifies on Iran" and "The Iran war has cost the US, says Defense Secretary
# Hegseth" are both events in Washington, not Tehran. (Verbs of GOING somewhere — visit/travel/arrive —
# are deliberately excluded: there the named place IS the destination.)
_ACT_VERBS = _SAY_VERBS | {"testifies", "testified", "testify", "testifying",
                           "requests", "requested", "seeks", "sought", "briefs", "briefed",
                           "signs", "signed", "vetoes", "vetoed"}


def _actor_country(words):
    """A national OFFICIAL who is the actor of the sentence (speaks/testifies/signs), wherever they sit in
    the headline. Handles 'Hegseth testifies on Iran' (name then verb) AND '..., says Defense Secretary
    Pete Hegseth' (verb then name). Returns their country, or None. The caller only asks when the chosen
    place is a bare country with no locational context, so a real scene is never overruled."""
    n = len(words)
    for j in range(n):
        for size in (2, 1):
            if j + size > n:
                continue
            co = _OFFICIAL_COUNTRY.get(" ".join(words[j:j + size]))
            if not co:
                continue
            for k in range(j + size, min(j + size + 3, n)):          # name then acting verb
                if words[k] in _ACT_VERBS:
                    return co
            for k in range(max(0, j - 4), j):                        # trailing attribution: verb then name
                if words[k] in _ACT_VERBS:
                    return co
    return None


# A STATE ORGAN, not a place. Deliberately narrow: "officials"/"authorities" are NOT here, because
# "Israeli officials say Gaza strikes will continue" is news about Gaza.
_STATE_BODIES = {"ministry", "ministries", "department", "government", "cabinet", "presidency",
                 "embassy", "consulate", "chancellery", "parliament", "mission",
                 # a named MINISTER speaking is his ministry speaking: "Israeli Foreign Minister
                 # Gideon Saar says his country is ready to…" is news in Israel (it was dotting ROME)
                 "minister", "ministers", "chancellor", "premier", "envoy", "ambassador",
                 "spokesman", "spokeswoman", "spokesperson"}


def _national_body_actor(words):
    """A country's own MINISTRY acting is news at THAT country, however foreign the subject matter.
    SHIPPED BUG: "Türkiye's Foreign Ministry commemorates Srebrenica genocide" was dotted on BOSNIA —
    but the ceremony was held in Ankara; Srebrenica is what it was ABOUT. Same shape as ACTORS SINK.

    Requires all three, in order: a country/demonym subject, one of its state organs, and a
    speech/ceremony verb. The caller only asks when the best place has NO locational context, so a
    ministry REPORTING a real event elsewhere ("Russia's Defense Ministry says its forces captured
    Toretsk" — 'captured' marks Toretsk as the scene) never reaches here."""
    n = len(words)
    for j in range(0, min(6, n)):
        for size in (3, 2, 1):
            if j + size > n:
                continue
            gram = " ".join(words[j:j + size])
            co = COUNTRY_ALIASES.get(gram) or DEMONYMS.get(gram)
            if not co or co not in COUNTRY_COORDS:
                continue
            for b in range(j + size, min(j + size + 4, n)):
                if words[b] in _STATE_BODIES:
                    for k in range(b + 1, min(b + 4, n)):
                        if words[k] in _SAY_VERBS:
                            return co
    return None
# ── POLICY TARGETS ────────────────────────────────────────────────────────────────────────────
# A sanction, tariff or aid package is DEBATED AND VOTED where the body sits. The country it names
# is the TARGET, not the scene — nothing has happened there. "Senate looks to honor Graham with
# RUSSIA sanctions" is an event in Washington; it was dotting Moscow.
# This is the mirror of ACTORS SINK: a target is the OBJECT of the instrument, an actor is its
# subject, and NEITHER is the place the news happened.
_POLICY_NOUNS = {"sanctions", "sanction", "tariff", "tariffs", "embargo", "embargoes", "levies",
                 "penalties", "duties", "restrictions", "sanctioning", "blacklist", "ban", "bans",
                 "aid", "package", "bill", "legislation", "resolution", "measure", "measures",
                 "deal", "talks", "treaty", "accord", "waiver", "funding", "arms"}
_TARGET_PREPS = {"on", "against", "toward", "towards", "with"}   # "accession talks WITH Ukraine"

# Where a governing body physically SITS. Only consulted once a place is known to be a mere target,
# so a bare "Senate" in a story about someone else's parliament can never drag a dot to Washington.
_SEATS = {
    "senate":              (38.895, -77.036, "Washington, D.C.", "United States of America"),
    "congress":            (38.895, -77.036, "Washington, D.C.", "United States of America"),
    "capitol hill":        (38.890, -77.009, "Capitol Hill", "United States of America"),
    "white house":         (38.898, -77.037, "The White House", "United States of America"),
    # longer names are matched FIRST, so Britain's lower house is never read as America's
    "house of commons":    (51.500, -0.125, "House of Commons, London", "United Kingdom"),
    "house of lords":      (51.500, -0.125, "House of Lords, London", "United Kingdom"),
    "downing street":      (51.503, -0.128, "Downing Street", "United Kingdom"),
    "westminster":         (51.500, -0.125, "Westminster", "United Kingdom"),
    "house of representatives": (38.890, -77.009, "Washington, D.C.", "United States of America"),
    "house":               (38.890, -77.009, "Washington, D.C.", "United States of America"),
    "pentagon":            (38.871, -77.056, "The Pentagon", "United States of America"),
    "state department":    (38.895, -77.048, "State Department", "United States of America"),
    "treasury":            (38.899, -77.035, "US Treasury", "United States of America"),
    "european union":      (50.844, 4.383, "Brussels", "Belgium"),
    "european commission": (50.844, 4.383, "Brussels", "Belgium"),
    "eu":                  (50.844, 4.383, "Brussels", "Belgium"),
    "nato":                (50.878, 4.423, "NATO HQ, Brussels", "Belgium"),
    "united nations":      (40.750, -73.968, "United Nations, New York", "United States of America"),
    "security council":    (40.750, -73.968, "United Nations, New York", "United States of America"),
    "kremlin":             (55.752, 37.617, "Moscow", "Russia"),
    "duma":                (55.757, 37.615, "Moscow", "Russia"),
}
# An official acts from his own capital: "Trump signs Russia sanctions bill" -> Washington.
_CAPITAL_SEAT = {
    "United States of America": (38.895, -77.036, "Washington, D.C."),
    "Russia": (55.752, 37.617, "Moscow"),
    "Ukraine": (50.450, 30.523, "Kyiv"),
    "Israel": (31.781, 35.222, "Jerusalem"),
    "Iran": (35.689, 51.389, "Tehran"),
    "China": (39.904, 116.407, "Beijing"),
    "France": (48.857, 2.352, "Paris"),
    "United Kingdom": (51.507, -0.128, "London"),
    "Germany": (52.520, 13.405, "Berlin"),
    "Italy": (41.903, 12.496, "Rome"),
    "Turkey": (39.933, 32.860, "Ankara"),
    "India": (28.614, 77.209, "New Delhi"),
    "North Korea": (39.039, 125.762, "Pyongyang"),
    "Argentina": (-34.604, -58.382, "Buenos Aires"),
    "Brazil": (-15.794, -47.882, "Brasilia"),
    "Hungary": (47.498, 19.040, "Budapest"),
}


def _is_policy_target(h, words):
    """Is this country the OBJECT of a policy instrument rather than the scene of an event?
    Only whole countries are used this way. Two shapes count, and nothing else:
      "RUSSIA sanctions", "CHINA tariffs"   — the name modifies the instrument
      "sanctions ON Russia", "ban AGAINST"  — the name is the instrument's object
    It must NOT fire on "RUSSIA says it will respond to new sanctions": there Russia is the SUBJECT,
    and that story really is about Russia (a shipped test case guards it)."""
    if h[1] != "country" or not any(w in _POLICY_NOUNS for w in words):
        return False
    end = h[0] + len(str(h[7]).split())
    if end < len(words) and words[end] in _POLICY_NOUNS:
        return True
    prv = words[h[0] - 1] if h[0] > 0 else ""
    if prv in _TARGET_PREPS:
        return any(w in _POLICY_NOUNS for w in words[max(0, h[0] - 4):h[0] - 1])
    return False


def _seat_place(words):
    """The body that is acting, and where it actually sits. Institutions first, then named officials
    (who act from their own capital). Returns (lat, lng, label, country) or None."""
    n = len(words)
    for i in range(n):
        for size in (3, 2, 1):
            if i + size > n:
                continue
            s = _SEATS.get(" ".join(words[i:i + size]))
            if s:
                return s[0], s[1], s[2], s[3]
    for i in range(n):
        for size in (3, 2, 1):
            if i + size > n:
                continue
            co = _OFFICIAL_COUNTRY.get(" ".join(words[i:i + size]))
            if co and co in _CAPITAL_SEAT:
                la, ln, lbl = _CAPITAL_SEAT[co]
                return la, ln, lbl, co
    return None


_GEO_ACTION = {# an AIRSTRIKE marks its target as the scene. SHIPPED: "USAF AIRSTRIKES against Khorram
               # Abad" — spaCy tags "Khorram Abad" PERSON, and with no locational word in front of it
               # the NER veto DELETED the city, so the dot fell back to the whole province.
               "airstrike", "airstrikes", "airstrikes,", "bombardment", "bombardments",
               "explosion", "explosions", "blast", "blasts", "interception", "interceptions",
               "hitting", "striking", "targeting", "shelling", "bombing", "storming",
               "strike", "strikes", "struck", "hit", "hits", "attack", "attacks", "attacked", "bomb",
               "bombs", "bombed", "shell", "shells", "shelled", "shelling", "target", "targets",
               "targeted", "raid", "raids", "raided", "storm", "storms", "seize", "seizes", "seized",
               "capture", "captures", "captured", "enter", "enters", "entered", "invade", "invades",
               "invaded", "besiege", "besieged", "reach", "reaches", "reached", "batter", "batters"}

# Pretty labels for names whose tokenised (apostrophe-as-space) form would title-case to nonsense —
# "sana a" -> "Sana A". The apostrophe belongs back in the DISPLAY name, never in the match key.
_DISPLAY_NAMES = {
    "sana a": "Sana'a", "sanaa": "Sana'a", "ta izz": "Ta'izz", "taizz": "Ta'izz",
    "ma rib": "Ma'rib", "marib": "Ma'rib", "sa dah": "Saada",
    "sana a international airport": "Sana'a International Airport",
    "sanaa international airport": "Sana'a International Airport",
    "zaporizhzhia npp": "Zaporizhzhia NPP", "zaporizhzhia nuclear power plant": "Zaporizhzhia NPP",
    "npp": "NPP",
}


def _label_for(gram, country):
    if gram in _DISPLAY_NAMES:
        lbl = _DISPLAY_NAMES[gram]
    else:
        lbl = gram.title().replace(" Of ", " of ").replace(" El ", " el ").replace(" De ", " de ")
    if gram in _WATER_NAMES:
        return lbl                       # "Sea of Azov", not "Sea of Azov, Ukraine"
    short = _co_short(country)
    if lbl.lower() == short.lower():
        return lbl                       # a city-state is not "Singapore, Singapore"
    return lbl + ", " + short


def _resolve(gram, mentions):
    """Every reading of a name, scored. CONTEXT decides: 'Georgia' in a story that is otherwise about
    the US is the state; on its own it is the country. Returns (kind, lat, lng, country, label, prior)."""
    cands = []
    co = COUNTRY_ALIASES.get(gram)
    if co and co in COUNTRY_COORDS:
        lat, lng = COUNTRY_COORDS[co]
        cands.append(("country", lat, lng, co, _co_short(co), _COUNTRY_PRIOR))
    # PLURAL demonyms: "16 INDIANS killed in the Middle East" was dotted on the UNITED STATES because
    # only the singular "indian" was ever in the table. Every nationality is routinely pluralised.
    dm = DEMONYMS.get(gram)
    if not dm and len(gram) > 4 and gram.endswith("s"):
        dm = DEMONYMS.get(gram[:-1])
    if dm and dm in COUNTRY_COORDS and not co:
        lat, lng = COUNTRY_COORDS[dm]
        cands.append(("demonym", lat, lng, dm, _co_short(dm), _COUNTRY_PRIOR))
    for (lat, lng, c, prior) in CITY_CANDS.get(gram, ()):
        cands.append(("city", lat, lng, c, _label_for(gram, c), prior))
    if not cands:
        return None
    ctx = {co for (co, g) in mentions if g != gram}      # a name never vouches for itself
    in_ctx = [c for c in cands if c[3] in ctx]
    if in_ctx:
        # the story is about this country -> take the most SPECIFIC reading inside it
        in_ctx.sort(key=lambda c: (0 if c[0] == "city" else 1, -c[5]))
        best_ctx = in_ctx[0]
        # DOMINANT-CITY OVERRIDE. Context is a tie-breaker, not a licence to demote a world capital to
        # a same-named village. "Lebanon talks IN ROME" mentions US officials, so the US enters context
        # and "Rome" was resolved to Rome, GEORGIA (pop 36k) over Rome, Italy (2.8M). If a candidate in
        # ANOTHER country is dramatically more prominent (>=20x the population AND >=500k), it wins.
        # The 20x gate keeps the real disambiguations safe: Tripoli(LB) over Tripoli(LY), Valencia(ES)
        # over Valencia(VE) — those gaps are nowhere near 20x.
        best_all = max(cands, key=lambda c: c[5])
        if (best_all[3] != best_ctx[3] and best_all[0] == "city" and best_ctx[0] == "city"
                and best_all[5] >= 500000 and best_all[5] >= 20 * max(best_ctx[5], 1)):
            return best_all + (False,)
        return best_ctx + (True,)
    cands.sort(key=lambda c: -c[5])          # no context: strongest prior (country > state > big city)
    return cands[0] + (False,)


def _scan_places(text, spans, mentions):
    """Gazetteer n-gram scan (longest match first), NER veto on city hits, context-aware resolution."""
    toks = [(mm.group(0), mm.start(), mm.end()) for mm in re.finditer(r"[A-Za-z0-9]+", _fold(text or ""))]
    words = [t[0].lower() for t in toks]
    orig = [t[0] for t in toks]
    n = len(words)
    hits, i = [], 0
    while i < n:
        got = None
        for size in (5, 4, 3, 2, 1):
            if i + size > n:
                continue
            gram = " ".join(words[i:i + size])
            r = _resolve(gram, mentions)
            if not r:
                continue
            kind, lat, lng, country, label, prior, supported = r
            # A CURATED FACILITY OR WATER IS NEVER VETOED. NER calls "Omsk oil refinery" an ORG and
            # "Afipsky refinery" an ORG — that mislabel is the whole reason the gazetteer leads here.
            # These names are hand-curated and unambiguous; there is no surname to confuse them with.
            # SHIPPED BUG: "...and the AFIPSKY REFINERY in Krasnodar region" was deleted outright,
            # because no preposition happened to sit in front of it, and the dot fell back to a region.
            if kind == "city" and prior < _FACILITY_PRIOR:
                weak = (size == 1 and gram in _WEAK_CITIES)
                cs, ce = toks[i][1], toks[i + size - 1][2]
                # "attack ON Zaporizhzhia" / "in Kabul" is hard locational evidence. NER routinely
                # mislabels foreign city names as PERSON (Valencia, Zaporizhzhia) — do not let a lone
                # PERSON guess delete a place the sentence is explicitly pointing at.
                _p2 = words[max(0, i - 2):i]
                located_here = any(w in _GEO_PREP or w in _GEO_ACTION for w in _p2)
                if _ner_vetoes(spans, cs, ce, weak, supported, located_here):
                    continue
                if weak and (gram in _BAD_CITY_NAMES or not orig[i][:1].isupper()):
                    continue
                # SURNAME GUARD. NER is not a safety net — it simply MISSED "Jamieson Greer"
                # (tagged nothing), so the gazetteer happily read the surname as Greer, South
                # Carolina and dotted a US trade story on a town of 28,000. A minor town preceded by
                # another Capitalised word that is not itself a place is a FORENAME + SURNAME.
                # Only weak (small) names are at risk; "East Aleppo" or "in Manchester" are unaffected
                # because Aleppo/Manchester are big, and a preposition is lower-case.
                if weak and i > 0 and orig[i - 1][:1].isupper():
                    _prev = words[i - 1]
                    if (_prev not in _GEO_PREP and _prev not in _GEO_ACTION
                            and _prev not in CITY_CANDS and _prev not in COUNTRY_ALIASES
                            and _prev not in DEMONYMS and _prev not in _DIRECTIONS):
                        continue
                # ...and a forename FOLLOWED by a capitalised surname: "DAPHNE Caruana Galizia" is the
                # murdered journalist, not Daphne, Alabama. Two signals, either suffices:
                #   * a run of 3+ capitalised tokens (a full personal name), or
                #   * a naming/killing verb right before it ("plot to KILL Daphne").
                if weak:
                    _o1 = orig[i + 1] if i + 1 < n else ""
                    _o2 = orig[i + 2] if i + 2 < n else ""
                    _run3 = _o1[:1].isupper() and _o2[:1].isupper()
                    _prevw = words[i - 1] if i > 0 else ""
                    if (_run3 or _prevw in _NAME_VERBS) and _o1[:1].isupper() \
                            and words[i + 1] not in CITY_CANDS and words[i + 1] not in COUNTRY_ALIASES:
                        continue
                nxt = words[i + size] if i + size < n else ""
                prv = words[i - 1] if i > 0 else ""
                if nxt in _PERSON_VERBS and prv not in _GEO_PREP and prv not in _GEO_ACTION:
                    continue
            got = (i, size, kind, lat, lng, label, country, prior, gram)
            break
        if got:
            hits.append((got[0], got[2], got[3], got[4], got[5], got[6], got[7], got[8]))
            i += got[1]
        else:
            i += 1
    return hits, words


def _is_person_nationality(h, words):
    """A country OR demonym bolted onto a PERSON noun — 'VENEZUELAN man', 'COLOMBIAN migrants',
    'US national'. That is the person's passport, never the scene of the event. Works for demonyms
    too (unlike the actor test), because a nationality is not a place in ANY sense — it must be
    dropped whether or not a real scene is present. State actors are safe: 'forces'/'troops'/'army'
    are in _ACTOR_NOUNS, not _PERSON_NOUNS, so 'Ukrainian forces' is untouched."""
    if h[1] not in ("country", "demonym"):
        return False
    j = h[0] + len(str(h[7]).split())
    return j < len(words) and words[j] in _PERSON_NOUNS


# A WEAPON carries the nationality of whoever BUILT/LAUNCHED it — its origin, never the scene it hits.
# SHIPPED BUG: "An IRANIAN projectile, likely a one-way drone, impacted at Ali al-Salem Air Base,
# KUWAIT" dotted TEHRAN — "Iranian" (leftmost) won because we only dropped nationalities in front of
# PERSON nouns. A drone's flag is as un-locational as a passport: the event is where it LANDED.
_MATERIEL_NOUNS = {
    "projectile", "projectiles", "drone", "drones", "uav", "uavs", "missile", "missiles",
    "rocket", "rockets", "warhead", "warheads", "munition", "munitions", "aircraft", "warplane",
    "warplanes", "jet", "jets", "fighter", "fighters", "bomber", "bombers", "helicopter",
    "helicopters", "warship", "warships", "submarine", "submarines", "tank", "tanks",
    "interceptor", "interceptors", "shell", "shells", "artillery", "gunboat", "gunboats",
}


def _is_materiel_nationality(h, words):
    """A demonym/country attached to a WEAPON names where the weapon is FROM, not where it struck.
    'Iranian drone', 'Russian missile', "Iran's projectile" — drop it exactly like a person's passport
    so the actual impact scene ('...at X, Kuwait') can win. Even in the verb reading ('Iran shells
    Kuwait') dropping the actor is correct, because the scene it names should take the dot."""
    if h[1] not in ("country", "demonym"):
        return False
    j = h[0] + len(str(h[7]).split())
    nxt = words[j] if j < len(words) else ""
    if nxt == "s":                                   # possessive: "Iran's drone"
        nxt = words[j + 1] if j + 1 < len(words) else ""
    return nxt in _MATERIEL_NOUNS


def _km(a_lat, a_lng, b_lat, b_lng):
    p = math.pi / 180.0
    x = 0.5 - math.cos((b_lat - a_lat) * p) / 2 + math.cos(a_lat * p) * math.cos(b_lat * p) * \
        (1 - math.cos((b_lng - a_lng) * p)) / 2
    return 12742 * math.asin(math.sqrt(max(0.0, x)))


# A PORT IS ON THE COAST. "Russian Military Strikes UKRAINIAN PORT" named no port, so the only place
# left in the post was the Black Sea — and the dot landed in open water, hundreds of miles from any
# quay, flying a TURKISH flag (the Black Sea's nominal country). The converse of "ships cannot burn on
# land": a port cannot be in the middle of a sea. Until the specific port is named, put it on that
# country's principal port, which is at least on the right coastline.
_PRINCIPAL_PORT = {
    "Ukraine": (46.482, 30.723, "Odesa (port, unspecified)"),
    "Russia": (44.722, 37.789, "Novorossiysk (port, unspecified)"),
    "Israel": (32.826, 35.001, "Haifa (port, unspecified)"),
    "Iran": (27.183, 56.277, "Bandar Abbas (port, unspecified)"),
    "Yemen": (14.802, 42.940, "Hodeidah (port, unspecified)"),
    "Lebanon": (33.901, 35.518, "Beirut (port, unspecified)"),
    "Sudan": (19.617, 37.216, "Port Sudan (port, unspecified)"),
    "Syria": (35.531, 35.791, "Latakia (port, unspecified)"),
    "Georgia": (41.646, 41.640, "Poti (port, unspecified)"),
    "Romania": (44.173, 28.652, "Constanta (port, unspecified)"),
    "Turkey": (40.976, 29.077, "Istanbul (port, unspecified)"),
    "Poland": (54.520, 18.545, "Gdansk (port, unspecified)"),
    "Netherlands": (51.949, 4.140, "Rotterdam (port, unspecified)"),
}
_PORT_WORDS = ("port", "ports", "harbour", "harbor", "seaport", "docks")


def _generic_port_country(words):
    """The story says a PORT was hit but never says WHICH. Whose port is it?"""
    for i, w in enumerate(words):
        if w not in _PORT_WORDS:
            continue
        for j in (i - 1, i - 2):                       # "Ukrainian port", "Ukraine's port"
            if j < 0:
                continue
            co = DEMONYMS.get(words[j]) or COUNTRY_ALIASES.get(words[j])
            if co in _PRINCIPAL_PORT:
                return co
    return None


def _is_facility(h):
    """A curated refinery/port/airbase/water — the most precise hit there is.
    NOTE the kind check: a COUNTRY carries _COUNTRY_PRIOR (10^9), which is *larger* than
    _FACILITY_PRIOR, so a bare `prior >= _FACILITY_PRIOR` test silently matches countries and
    demonyms too — that promoted "ISRAELI" over Gaza and dotted the wrong state."""
    return h[1] == "city" and h[6] >= _FACILITY_PRIOR


# A water named ATTRIBUTIVELY names a COASTLINE, not the water. "Black Sea PORTS" are ports ON the
# Black Sea — at a quay, on land. SHIPPED BUG: "Russia strikes Ukrainian drone industry and BLACK SEA
# PORTS" put the dot in open water in the middle of the sea, while the story's own summary named the
# actual ports (Odessa and Yuzhny). The converse of "ships cannot burn on land".
_WATER_ATTRIB = {"port", "ports", "coast", "coastline", "shore", "shores", "shoreline", "basin",
                 "fleet", "grain", "corridor", "corridors", "route", "routes", "shipping", "trade",
                 "littoral", "rim", "waters", "states", "countries", "nations", "region", "regions",
                 "neighbours", "neighbors", "resort", "resorts", "terminal", "terminals"}


def _nxt_word(h, words):
    i = h[0] + len(str(h[7]).split())
    return words[i] if i < len(words) else ""


def _is_nationality(h, words):
    """A country bolted onto a PERSON or a SHIP'S FLAG. Not a place in any sense.
    SHIPPED BUG: "'US NATIONAL' arrested in India" dotted the US, and "Russia struck the
    TANZANIA-FLAGGED cargo vessel off Odessa" dotted TANZANIA — a flag of convenience is the
    least locational fact in existence."""
    return h[1] == "country" and (_nxt_word(h, words) in _PERSON_NOUNS
                                  or _nxt_word(h, words) in ("flagged", "born", "based", "owned"))


def _is_attrib_water(h, words):
    """"BLACK SEA ports", "Baltic Sea states" — the water qualifies something else and is not itself
    the scene."""
    return h[7] in _WATER_NAMES and _nxt_word(h, words) in _WATER_ATTRIB


# a strike word that is a VERB here, not a noun
_STRIKE_VERBS = ("strike", "strikes", "attack", "attacks", "hit", "hits", "raid", "raids",
                 "targets", "shells", "bombs", "launches", "fires", "pounds")
# what follows a strike NOUN ("Gaza strikes WILL continue") rather than a strike VERB
_AFTER_STRIKE_NOUN = ("will", "would", "could", "may", "might", "are", "were", "have", "had",
                      "continue", "resume", "resumed", "killed", "kill", "hit", "left", "caused",
                      "and", "or", "on", "in", "near", "over", "against", "targeting")


def _is_actor_h(h, words):
    """Does this name WHO DID IT rather than WHERE it happened?"""
    if h[1] == "demonym":
        return True
    nxt = h[0] + len(str(h[7]).split())
    if nxt < len(words) and words[nxt] == "s":         # "Russia" + "s" == "Russia's"
        return True
    if h[1] != "country" or nxt >= len(words):
        return False
    # "U.S. ATTACK targeted ...", "Israeli FORCES raid ..." — a country in front of one of these
    # names who DID it, not where it happened.
    if words[nxt] in _ACTOR_NOUNS:
        return True
    if words[nxt] in _STRIKE_VERBS:
        # "U.S. STRIKES ON Rask" — a target preposition after the strike word: only the attacker.
        if nxt + 1 < len(words) and words[nxt + 1] in ("on", "against", "near", "over"):
            return True
        # "RUSSIA strikes Ukrainian drone industry" — the SENTENCE SUBJECT doing the striking.
        # Bare "strike(s)" can never be an _ACTOR_NOUN, because in "Gaza strikes will continue" it is
        # a NOUN and the strikes are ON Gaza — so require the country to open the sentence AND the
        # next word to be a real object rather than the auxiliary that marks the noun reading.
        if h[0] == 0 and (nxt + 1 >= len(words) or words[nxt + 1] not in _AFTER_STRIKE_NOUN):
            return True
    # A NATIONALITY, not a place. "'US NATIONAL' arrested in India" happens in INDIA; the ship in
    # "Russia struck the TANZANIA-flagged cargo vessel" is not in Tanzania — a flag of convenience
    # is the least locational fact there is. Same rule that keeps the Colombian flag off a Maine
    # shooting, applied to the DOT instead of the flags.
    return words[nxt] in _PERSON_NOUNS or words[nxt] in ("flagged", "born", "based", "owned")


def _genuine_scenes(hits, words):
    """The hits that could actually BE the scene of the event — not the actor, not a person's
    nationality, not what a sanction is aimed at, and not a water named attributively."""
    return [h for h in hits
            if not _is_actor_h(h, words) and not _is_nationality(h, words)
            and not _is_policy_target(h, words) and not _is_attrib_water(h, words)]


def _pick_place(hits, words):
    """'in/at/near X' and 'hits X' mark the event location; leftmost wins (the subject's own place).
    Then CONTAINMENT: if the winner is a whole country but the text also names a city INSIDE that
    country, use the city — 'hitting the Syzran oil refinery in Russia's Samara region' is an event in
    Syzran, not a dot on Moscow."""
    if not hits:
        return None

    def _nxt(h):
        return _nxt_word(h, words)

    located, other = [], []
    for h in hits:
        prev2 = words[max(0, h[0] - 2):h[0]]
        loc = any(w in _GEO_PREP or w in _GEO_ACTION for w in prev2)
        # NEITHER A POSSESSIVE NOR A NATIONALITY MAY ENTER THE `located` POOL — that pool wins
        # outright, so anything in it beats everything else before the sink logic is even consulted.
        #   "in RUSSIA'S Bashkortostan"        — the preposition governs the phrase; its head is
        #                                        Bashkortostan, and Russia is merely the owner.
        #   "STRUCK the TANZANIA-flagged ship" — 'struck' points at the SHIP, not at Tanzania.
        # Both of these put a non-place in `located`, where it was the only candidate and won.
        if loc and (_nxt(h) == "s" or _is_nationality(h, words)):
            loc = False
        (located if loc else other).append(h)
    # A demonym names the ACTOR, not the scene ("UKRAINIAN drones strike the port of Azov" happens in
    # Azov). A POSSESSIVE does the same: "RUSSIA'S attack on Zaporizhzhia" happens in Zaporizhzhia,
    # even though "injured in Russia's..." reads like a location. Actors sink; then leftmost; then city.
    def _is_actor(h):
        return _is_actor_h(h, words)

    # TARGETS SINK too — a country named only as what a sanction/tariff/bill is AIMED AT is not the
    # scene. If the headline also names a real place, that place wins outright.
    #
    # ...but an actor may ONLY sink below a GENUINE SCENE: a place with locational context, or a
    # city/facility/water (things events happen AT). It must never sink below a bare country that the
    # story merely MENTIONS, or the dot lands on whatever country wandered into the sentence last.
    # SHIPPED BUG: "IRAN'S IRGC released footage of launches towards U.S. bases" dotted the UNITED
    # STATES, and "Ahmadinejad attended a ceremony ... reports claiming he was an ISRAELI asset"
    # dotted ISRAEL. Iran sank, and the only thing left standing was the country it was aimed at or
    # gossiped about. When nothing is a scene, THE ACTOR IS THE SCENE — a state does its business at
    # home unless the story says otherwise.
    def _is_scene(h):
        if _is_attrib_water(h, words):
            return False            # "Black Sea ports" is a coastline, not the sea
        p2 = words[max(0, h[0] - 2):h[0]]
        return any(w in _GEO_PREP or w in _GEO_ACTION for w in p2) or h[1] == "city"

    scene_exists = any(_is_scene(h) for h in hits
                       if not _is_actor(h) and not _is_policy_target(h, words))

    def _sink(h):
        # a nationality sinks even when there is NO scene at all — a passport is never a location, and
        # a weapon's flag ('Iranian drone') is no more locational than one
        if (_is_policy_target(h, words) or _is_nationality(h, words)
                or _is_materiel_nationality(h, words) or _is_attrib_water(h, words)):
            return 1
        return 1 if (_is_actor(h) and scene_exists) else 0

    pool = located or other
    pool.sort(key=lambda h: (_sink(h), h[0], 0 if h[1] == "city" else 1))
    best = pool[0]
    # CONTAINMENT — a story that names a big area AND a specific place inside it happens at the
    # specific place: "...over occupied CRIMEA ... above the SOVETSKY district" -> Sovetsky;
    # "Ukraine's attack ... in LPR" + dateline LUGANSK -> Lugansk. Facilities and waters are already
    # as specific as it gets, so they are never "upgraded" away.
    def _is_area(h):
        return h[1] in ("country", "demonym") or h[7] in _AREA_NAMES

    if _is_area(best):
        if best[1] in ("country", "demonym"):
            # A SEA IS NOT A TOWN INSIDE A COUNTRY. Waters are registered as "city" hits and carry a
            # nominal country, so "Russia strikes … BLACK SEA ports" containment-upgraded Russia to
            # the Black Sea (whose nominal country IS Russia) and dropped the dot in open water.
            # Containment exists to find a specific town inside a named area — never a whole sea.
            inside = [h for h in hits if h[1] == "city" and h[5] == best[5]
                      and h[7] not in _AREA_NAMES and h[7] not in _WATER_NAMES
                      and not _is_attrib_water(h, words)]
        else:
            inside = [h for h in hits if h[1] == "city" and h[7] not in _AREA_NAMES
                      and _km(best[2], best[3], h[2], h[3]) < 500]
        if inside:
            inside.sort(key=lambda h: (0 if any(w in _GEO_PREP or w in _GEO_ACTION
                                                for w in words[max(0, h[0] - 2):h[0]]) else 1, h[0]))
            best = inside[0]
    # FACILITY UPGRADE — a named facility is the most precise thing a story can hand us, and it must
    # not lose to a city merely because the CITY got the preposition: in "hits the AFIPSKY REFINERY
    # in Krasnodar region" the event is at the refinery, and Krasnodar is just where the refinery is.
    # This is the "pin the exact refinery" goal. Guarded to the same country / 400km so a facility
    # named in passing about somewhere else can never hijack the dot.
    if not _is_facility(best):
        # If the best we have is a bare COUNTRY, any named facility beats it outright — a country is
        # at most a container, and often just the actor. ("US halts removal of refuelers from BEN
        # GURION AIRPORT" was dotted on the US.) The distance guard only matters once we are already
        # on a real town, where jumping to a far-off facility WOULD be wrong.
        loose = best[1] in ("country", "demonym")
        facs = [h for h in hits if _is_facility(h)
                and (loose or h[5] == best[5] or _km(best[2], best[3], h[2], h[3]) < 400)]
        if facs:
            best = sorted(facs, key=lambda h: h[0])[0]
    return best


_WIRE_MARK = re.compile(r"/[A-Z]{2,6}/|\([A-Za-z .]{2,20}\)|\b[A-Z][a-z]{2,8}\s+\d{1,2}\b")
_DATELINE = re.compile(r"^([A-Z][A-Za-z.'\-]{2,20}(?:\s+[A-Z][A-Za-z.'\-]{2,20})?)\s*[,(]")


def _dateline_place(desc, mentions):
    """Wire copy states the event location first: "LUGANSK, July 12. /TASS/.", "BEIRUT (Reuters) -".
    That dateline is the single most authoritative location a story carries — far better than guessing
    from a headline that only says "Ukraine's attack ... in LPR"."""
    d = _fold(desc or "").lstrip()
    if not d or not _WIRE_MARK.search(d[:80]):
        return None
    m = _DATELINE.match(d)
    if not m:
        return None
    r = _resolve(m.group(1).lower().strip(), mentions)
    return r if (r and r[0] == "city") else None


def _geolocate(title, sourcecountry, desc="", url=""):
    """Best location for an event. Context (other countries named + the article's own section) decides
    between readings of an ambiguous name. If the headline names nowhere we read the story's summary
    before ever falling back to the outlet's home country."""
    title = title or ""
    mentions = _context_mentions(title + " " + (desc or ""), url)
    dl = _dateline_place(desc, mentions)
    hits, words = _scan_places(title, _person_spans(title), mentions)
    # A person's NATIONALITY is not the event location. Drop "Venezuelan man"/"Colombian migrants"
    # from the TITLE candidates. If a real place remains ("Colombian man in MAINE"), keep it. If the
    # title then names NO place, prefer the summary's actual scene ("...in Georgia"), and only then
    # fall to the URL/subject ladder. SHIPPED BUG: "VENEZUELAN man dies in ICE custody" dotted Caracas.
    if hits:
        real = [h for h in hits if not _is_person_nationality(h, words) and not _is_materiel_nationality(h, words)]
        if real:
            hits = real
        else:
            if desc:
                dh, dw = _scan_places(desc[:400], _person_spans(desc[:400]), mentions)
                dh = [h for h in dh if not _is_person_nationality(h, dw) and not _is_materiel_nationality(h, dw)]
                if dh:
                    b = _pick_place(dh, dw)
                    return b[2], b[3], b[4], b[5]
            hits = []                     # title held only nationalities -> use the fallback ladder
    # THE HEADLINE NAMES NO SCENE — ONLY WHO DID IT, OR A COASTLINE. The story's own summary almost
    # always names the actual place, so read it before settling for the actor's country.
    # SHIPPED BUG: "RUSSIA strikes Ukrainian drone industry and BLACK SEA PORTS" dropped a dot in open
    # water in the middle of the sea, while its very first line read "…port infrastructure in ODESSA
    # and Yuzhny". The dot must be where the event happened, and a port is not in the sea.
    if hits and desc and not _genuine_scenes(hits, words):
        dh, dw = _scan_places(desc[:400], _person_spans(desc[:400]), mentions)
        dscenes = _genuine_scenes(dh, dw) if dh else []
        if dscenes:
            b = _pick_place(dscenes, dw)
            if b is not None:
                return b[2], b[3], b[4], b[5]
    if hits:
        best = _pick_place(hits, words)
        # The headline gave only a broad area, but the wire dateline names the actual town. Trust the
        # dateline only when it agrees with the story (same country, or a country the story names) —
        # otherwise a Reuters story about Russia filed from LONDON would get dotted on London.
        _ctx = {co for (co, g) in mentions}
        if dl and (best[1] in ("country", "demonym") or best[7] in _AREA_NAMES):
            if dl[3] == best[5] or dl[3] in _ctx:
                return dl[1], dl[2], dl[4], dl[3]
        # A sanction/tariff/bill is in play and the best we have is either the country it is AIMED AT
        # (nothing has happened there) or a bare country centroid. The body that is ACTING is a real,
        # far more precise scene — "Senate ... Russia sanctions" is news in Washington, and the UK
        # Commons backing them is news at Westminster, not somewhere in the middle of England.
        # A genuine located scene ("protesters IN BERLIN rally against Russia sanctions") is a city
        # and never reaches this, so it always outranks the seat.
        if any(_is_policy_target(h, words) for h in hits) and best[1] == "country":
            seat = _seat_place(words)
            if seat:
                return seat
        located = any(w in _GEO_PREP or w in _GEO_ACTION
                      for w in words[max(0, best[0] - 2):best[0]])
        # A named FACILITY is a scene, full stop — never let "X said…" overrule it. Zelensky
        # announcing a strike on the Afipsky refinery is news AT THE REFINERY, not in Kyiv.
        # A NAMED PORT beats this; an UNNAMED one must still not float in open water.
        if best[7] in _WATER_NAMES or best[1] in ("country", "demonym"):
            pc = _generic_port_country(words)
            if pc:
                la, ln, lbl = _PRINCIPAL_PORT[pc]
                return la, ln, lbl, pc
        # A WATER'S "COUNTRY" IS ARBITRARY. The Black Sea is filed under Turkey, so a Russian strike on
        # a Ukrainian port flew a TURKISH flag. Take the country from the story instead, and keep the
        # water as the place.
        if best[7] in _WATER_NAMES:
            _ctx2 = [co for (co, g) in mentions if co in COUNTRY_COORDS]
            if _ctx2 and best[5] not in _ctx2:
                return best[2], best[3], best[4], _ctx2[0]
        if not located and not _is_facility(best) and best[1] in ("country", "demonym"):
            # A named official SPEAKING/TESTIFYING is news in their OWN country — the country they name is
            # the topic. "Hegseth testifies on Iran" / "..., says Defense Secretary Hegseth" -> United States.
            ac = _actor_country(words)
            if ac and ac in COUNTRY_COORDS:
                la, ln = COUNTRY_COORDS[ac]
                return la, ln, _co_short(ac), ac
        if not located and not _is_facility(best):
            # A ministry/government ACTING is news at its own seat — the foreign place it names is
            # the SUBJECT, not the scene. Nothing here is "located", so no real scene is at stake.
            co = _national_body_actor(words)
            if co:
                if co in _CAPITAL_SEAT:
                    la, ln, lbl = _CAPITAL_SEAT[co]
                    return la, ln, lbl, co
                if co in COUNTRY_COORDS:
                    la, ln = COUNTRY_COORDS[co]
                    return la, ln, _co_short(co), co
            sc = _statement_country(words)
            if sc and sc in COUNTRY_COORDS:
                la, ln = COUNTRY_COORDS[sc]
                return la, ln, _co_short(sc), sc
        return best[2], best[3], best[4], best[5]
    if dl:
        return dl[1], dl[2], dl[4], dl[3]
    # THE FALLBACK LADDER, in order of how much each signal actually knows. Getting this order wrong
    # is itself a bug: putting the org check above the URL sent a Singapore court story ("Shanmugam …
    # donate BLOOMBERG damages") back to the United States.
    #   1. the URL SECTION — the desk that filed it. The single most reliable thing left.
    uc = _url_country(url)
    if uc and uc in COUNTRY_COORDS:
        lat, lng = COUNTRY_COORDS[uc]
        return lat, lng, _co_short(uc), uc
    #   2. an ORG named in the TITLE — beats anything scraped from the summary, because the summary is
    #      where a quoted academic lives: "jihadist groups like BOKO HARAM use AI" was dotted on
    #      CAMBRIDGE, UK. The title is the story; the summary is commentary on it.
    oc = _org_country(title)
    if oc and oc in COUNTRY_COORDS:
        lat, lng = COUNTRY_COORDS[oc]
        return lat, lng, _co_short(oc), oc
    #   3. the story's own summary.
    if desc:
        d = desc[:400]
        dhits, dwords = _scan_places(d, _person_spans(d), mentions)
        if dhits:
            best = _pick_place(dhits, dwords)
            return best[2], best[3], best[4], best[5]
    # THE ARTICLE'S OWN SECTION, before the publisher's home country. The URL is the desk that filed
    # the story — /us-news/, /australia-news/ — and it is FAR better evidence than where the outlet's
    # office happens to be. SHIPPED BUG: the outlet fallback dotted a Hunter Biden story on IRAN
    # (Guardian /us-news/), an Albanese speech on the UK (Guardian /australia-news/), and a US Fed
    # story on TURKEY (Anadolu). We had the section all along and only used it as a tie-breaker.
    uc = _url_country(url)
    if uc and uc in COUNTRY_COORDS:
        lat, lng = COUNTRY_COORDS[uc]
        return lat, lng, _co_short(uc), uc
    # then WHO the story is about — a US senator's story is US news whoever prints it.
    pc = _subject_country(title, desc)
    if pc and pc in COUNTRY_COORDS:
        lat, lng = COUNTRY_COORDS[pc]
        return lat, lng, _co_short(pc), pc
    oc = _org_country(title)                     # ...and a Meta story is not Singapore news
    if oc and oc in COUNTRY_COORDS:
        lat, lng = COUNTRY_COORDS[oc]
        return lat, lng, _co_short(oc), oc
    sc = GDELT_COUNTRY.get(sourcecountry, sourcecountry)
    if sc in COUNTRY_COORDS:
        lat, lng = COUNTRY_COORDS[sc]
        return lat, lng, _co_short(sc), sc
    return None


# A PRIVATE INDIVIDUAL. A demonym in front of one of these is that person's nationality, not a
# country that is party to the event. SHIPPED BUG: "ICE fatally shoots 26-year-old COLOMBIAN MAN in
# Maine" flew the COLOMBIAN flag over a US story — Colombia had nothing to do with it.
# State actors are deliberately absent: an "Israeli soldier" or "Ukrainian forces" DO make their
# country a party to the event, so soldier/troops/forces/police/minister must never be listed here.
_PERSON_NOUNS = {
    "man", "men", "woman", "women", "boy", "girl", "teen", "teenager", "child", "children",
    "national", "nationals", "citizen", "citizens", "migrant", "migrants", "immigrant",
    "immigrants", "refugee", "refugees", "asylum", "resident", "residents", "tourist", "tourists",
    "student", "students", "worker", "workers", "driver", "passenger", "passengers", "suspect",
    "suspects", "victim", "victims", "detainee", "detainees", "father", "mother", "family",
    "couple", "grandmother", "grandfather", "native", "expat", "expats", "national's",
}


def _involved_countries(title, country=""):
    """Which countries are PARTIES to this event — the flags shown on the card.
    The country the event happened in always leads; a person's nationality never counts."""
    low = " " + re.sub(r"\s+", " ", re.sub(r"[^a-z ]", " ", _fold(title or "").lower())).strip() + " "
    out = []

    def _add(c):
        if c and c not in out:
            out.append(c)

    if country:
        _add(country)                                   # where it happened is always a party
    for city in _CITY_KEYS:
        if (" " + city + " ") in low:
            _add(CITY_COORDS[city][2])
    for name in _COUNTRY_ALIAS_KEYS:
        if (" " + name + " ") in low:
            _add(COUNTRY_ALIASES[name])
    for dem in _DEMONYM_KEYS:
        if (" " + dem + " ") not in low:
            continue
        m = re.search(r"\b" + re.escape(dem) + r"\b\s+([a-z]+)", low)
        if m and m.group(1) in _PERSON_NOUNS:
            continue                                    # "Colombian man" — a nationality, not a party
        _add(DEMONYMS[dem])
    return out[:4]


def _meta_content(html, keys):
    for k in keys:
        for pat in (
            r'<meta[^>]+(?:property|name|itemprop)=["\']' + re.escape(k) + r'["\'][^>]*?content=["\']([^"\']+)["\']',
            r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*?(?:property|name|itemprop)=["\']' + re.escape(k) + r'["\']',
        ):
            m = re.search(pat, html, re.I | re.S)
            if m:
                return _htmlmod.unescape(m.group(1)).strip()
    return ""


_PARA_BAD = ("cookie", "subscribe", "sign up", "newsletter", "advertisement",
             "all rights reserved", "©", "read more", "follow us", "terms of",
             "privacy policy", "whatsapp", "copylink", "copy link", "share this",
             "on facebook", "on twitter", " on google", "add al jazeera", "published on",
             "photo/file", "getty images", "/ap photo", "reuters/", "click here",
             "sign in to", "log in", "download the app", "most read", "related stories",
             "you may also", "recommended", "advertisement", "skip to", "listen to this")


# ── SHARP WIRE PROSE ─────────────────────────────────────────────────────────────────────────────
# Agency copy opens with a dateline and pads every sentence with attribution. "MELITOPOL, July 16.
# /TASS/. Two civilians were killed..." — the first six words are filing metadata, not news. Worse,
# the lead we showed was often "According to his information, the outskirts of Volnovakha... came
# under attack": a dependent clause referring to a person the reader has not met yet. Strip the
# throat-clearing and the fact lands in the first words, which is the whole job of a lead.
_DATELINE_CUT = re.compile(
    r"^\s*[A-Z][A-Za-z.'\-]{1,22}(?:[ /][A-Z][A-Za-z.'\-]{1,22}){0,2}\s*,\s*"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2}\s*\.?\s*"
    r"(?:/[A-Z]{2,8}/\.?\s*)?")
_WIRE_TAG = re.compile(
    r"^\s*\(?(?:Reuters|AP|AFP|TASS|Xinhua|Interfax|RIA Novosti|RIA|dpa|ANSA|EFE|PA Media|Anadolu)\)?"
    r"\s*[-–—:]\s*")
# the other half of the wire's habit: "BEIRUT (Reuters) - ", "WASHINGTON, July 3 (AP) — "
_DATELINE_PAREN = re.compile(
    r"^\s*[A-Z][A-Za-z.'\- ]{1,24}?"
    r"(?:,\s*(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s*\d{1,2})?\s*"
    r"\((?:Reuters|AP|AFP|TASS|Xinhua|Interfax|RIA Novosti|RIA|dpa|ANSA|EFE|PA Media|Anadolu|"
    r"Bloomberg|CNN|BBC)\)\s*[-–—:]\s*")
# a sentence that opens by citing its own sourcing is telling you nothing yet
_FILLER_OPEN = re.compile(
    r"^\s*(?:according to (?:his|her|its|their|the)\s+(?:information|data|report|words|statement|"
    r"account|estimates?)|according to (?:the )?(?:report|agency|statement|source|channel|ministry)|"
    r"it (?:was|is) (?:reported|noted|said|stated|specified|added|indicated) that|"
    r"as (?:was )?(?:reported|noted|stated) (?:earlier|previously|above)|"
    r"(?:he|she|they) (?:added|noted|specified|stressed|emphasized|emphasised|said) that|"
    r"reportedly|earlier it (?:was|had been) reported that)\s*,?\s*", re.I)


def _sharpen(t):
    """Wire copy, tightened: no dateline, no agency tag, no attributive preamble. The fact first."""
    t = re.sub(r"\s+", " ", (t or "")).strip()
    if not t:
        return ""
    t = _DATELINE_PAREN.sub("", t, count=1)
    t = _DATELINE_CUT.sub("", t, count=1)
    t = _WIRE_TAG.sub("", t, count=1)
    for _ in range(2):                      # "According to the report, it was noted that X"
        n = _FILLER_OPEN.sub("", t, count=1)
        if n == t:
            break
        t = n
    t = t.strip(" ,;:—–-").strip()
    if t and t[:1].islower():
        t = t[:1].upper() + t[1:]
    return t


# A lead must stand on its own. These open mid-thought — they refer back to something the reader
# has not been told, so they can never be the first thing on the card.
_DANGLING = re.compile(
    r"^\s*(?:he|she|they|it|this|that|these|those|his|her|their|the (?:official|agency|report|"
    r"statement|source|channel|ministry|spokesman|spokesperson))\b", re.I)


def _standalone(t):
    """Can this sentence be the opening line of a story on its own?"""
    t = (t or "").strip()
    if len(t) < 40 or len(t.split()) < 8:
        return False
    return not _DANGLING.match(t)


def _good_para(t):
    if len(t) < 60:
        return False
    low = t.lower()
    if any(b in low for b in _PARA_BAD):
        return False
    if len(t.split()) < 10:          # too short to be a real sentence
        return False
    if not re.search(r"[.!?]", t):   # real prose ends sentences
        return False
    letters = sum(c.isalpha() for c in t)
    if letters < len(t) * 0.55:      # mostly symbols/links -> junk
        return False
    return True


def _extract_article(html):
    image = _meta_content(html, ["og:image", "twitter:image", "twitter:image:src"])
    desc = _meta_content(html, ["og:description", "twitter:description", "description"])
    site = _meta_content(html, ["og:site_name"])
    published = _meta_content(html, ["article:published_time", "og:article:published_time",
                                     "datePublished", "publishdate", "date"])
    tm = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    title = _htmlmod.unescape(re.sub(r"\s+", " ", tm.group(1)).strip()) if tm else ""
    # remove non-article chrome so share widgets / captions / nav don't leak into the body
    body = re.sub(r"(?is)<(script|style|noscript|svg|form|button|nav|aside|header|footer|figure|figcaption|template|iframe)[^>]*>.*?</\1>", " ", html)
    am = re.search(r"<article[^>]*>(.*?)</article>", body, re.I | re.S)
    scope = am.group(1) if am else body
    paras, seen = [], set()
    for p in re.findall(r"<p[^>]*>(.*?)</p>", scope, re.I | re.S):
        t = _clean_post(p)
        if not _good_para(t):
            continue
        t = _sharpen(t)                       # dateline + agency tag + attributive padding out
        if not t:
            continue
        key = t[:60].lower()
        if key in seen:
            continue
        seen.add(key)
        paras.append(t)
        if len(paras) >= 14:
            break
    # THE LEAD MUST STAND ON ITS OWN. og:description is often a mid-article fragment ("According to
    # his information, the outskirts of Volnovakha... came under attack") — it opens by pointing at
    # someone the reader has not met. Sharpen it; if it still cannot open a story, use the first
    # paragraph that can.
    desc = _sharpen(desc)
    if desc and not _standalone(desc):
        desc = next((p for p in paras if _standalone(p)), desc)
    return {"title": title, "desc": desc,
            "image": image if _good_img(image) else "",
            "paragraphs": paras, "published": published, "site": site}


# ── CLIP PROXY ───────────────────────────────────────────────────────────────────────────────────
# MEASURED: Telegram throttles each CONNECTION, not the account. One sequential stream from
# cdn*.telesco.pe runs at ~1 Mbps, while six parallel range requests to the same file total ~4.4 Mbps
# — 3.8x. A browser's <video> can only ever open ONE sequential stream, so it gets the slow lane and
# playback outruns the buffer: the clip froze to rebuffer roughly every second.
# So we fetch the file ourselves, in parallel chunks, and hand it to the page from 127.0.0.1. The
# video element then reads a complete local file at LAN speed and cannot stall. Cached on disk, so a
# replay (or a second viewer of the same clip) is instant and costs Telegram nothing.
_CLIP_DIR = os.path.join(CACHE_DIR, "clips")
_CLIP_LOCK = threading.Lock()
_CLIP_INFLIGHT = {}


def _clip_path(url):
    return os.path.join(_CLIP_DIR, hashlib.sha1(url.encode("utf-8")).hexdigest() + ".mp4")


def _clip_prune(budget=400 * 1024 * 1024):
    """Keep the clip cache bounded — oldest out first. The wire only looks back 24h, so a clip from
    yesterday will never be asked for again."""
    try:
        files = []
        for n in os.listdir(_CLIP_DIR):
            fp = os.path.join(_CLIP_DIR, n)
            if os.path.isfile(fp):
                st = os.stat(fp)
                files.append((st.st_mtime, st.st_size, fp))
        cutoff = time.time() - 24 * 3600
        total = sum(f[1] for f in files)
        files.sort()                                    # oldest first
        for mt, sz, fp in files:
            if total <= budget and mt >= cutoff:
                break
            try:
                os.remove(fp)
                total -= sz
            except Exception:
                pass
    except Exception:
        pass


def _clip_fetch(url):
    """Download one clip with parallel range requests. Returns a local path, or "" on failure."""
    path = _clip_path(url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        return path
    with _CLIP_LOCK:
        ev = _CLIP_INFLIGHT.get(url)
        if ev is None:
            ev = _CLIP_INFLIGHT[url] = threading.Event()
            mine = True
        else:
            mine = False
    if not mine:                      # someone else is already fetching it — wait for them
        ev.wait(120)
        return path if os.path.exists(path) else ""
    try:
        os.makedirs(_CLIP_DIR, exist_ok=True)
        hdr = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
        req = urllib.request.Request(url, headers=dict(hdr, Range="bytes=0-0"))
        with urllib.request.urlopen(req, timeout=20) as r:
            cr = r.headers.get("Content-Range") or ""
        total = int(cr.split("/")[-1]) if "/" in cr else 0
        if total <= 0:                                    # no range support -> plain download
            with urllib.request.urlopen(urllib.request.Request(url, headers=hdr), timeout=90) as r:
                data = r.read()
        else:
            CH = 512 * 1024
            spans = [(i, min(i + CH - 1, total - 1)) for i in range(0, total, CH)]

            def _grab(sp):
                rq = urllib.request.Request(url, headers=dict(hdr, Range="bytes=%d-%d" % sp))
                for _ in range(3):
                    try:
                        with urllib.request.urlopen(rq, timeout=45) as rr:
                            return rr.read()
                    except Exception:
                        time.sleep(0.4)
                raise IOError("chunk failed")
            with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
                data = b"".join(ex.map(_grab, spans))     # map keeps chunk order
        tmp = path + ".part"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)                             # never publish a half-written file
        _clip_prune()
        return path
    except Exception:
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return ""
    finally:
        with _CLIP_LOCK:
            _CLIP_INFLIGHT.pop(url, None)
        ev.set()


def _serve_http():
    """Serve the app over http://127.0.0.1 so YouTube clips embed/play IN the app
    (file:// origins are blocked by YouTube). Returns a URL, or None to fall back to file://."""
    try:
        import http.server
        import socketserver
        directory = os.path.dirname(APP_HTML)
        fname = os.path.basename(APP_HTML)

        class _QuietHandler(http.server.SimpleHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if not self.path.startswith("/clip?"):
                    return http.server.SimpleHTTPRequestHandler.do_GET(self)
                try:
                    q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                    url = (q.get("u") or [""])[0]
                    # only ever proxy Telegram's own CDN — never an arbitrary URL from the page
                    host = urllib.parse.urlparse(url).netloc
                    if not (host.endswith(".telesco.pe") or host.endswith(".t.me")):
                        self.send_error(403)
                        return
                    path = _clip_fetch(url)
                    if not path or not os.path.exists(path):
                        self.send_error(504)
                        return
                    self._stream(path)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass                                   # the user scrubbed or closed the clip
                except Exception:
                    try:
                        self.send_error(500)
                    except Exception:
                        pass

            def _stream(self, path):
                size = os.path.getsize(path)
                rng = self.headers.get("Range") or ""
                start, end = 0, size - 1
                m = re.match(r"bytes=(\d*)-(\d*)", rng)
                if m and (m.group(1) or m.group(2)):
                    if m.group(1):
                        start = int(m.group(1))
                    if m.group(2):
                        end = int(m.group(2))
                    end = min(end, size - 1)
                    start = min(start, end)
                    self.send_response(206)
                    self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
                else:
                    self.send_response(200)
                self.send_header("Content-Type", "video/mp4")
                self.send_header("Accept-Ranges", "bytes")
                self.send_header("Content-Length", str(end - start + 1))
                self.send_header("Cache-Control", "private, max-age=86400")
                self.end_headers()
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        b = f.read(min(262144, left))
                        if not b:
                            break
                        self.wfile.write(b)
                        left -= len(b)

        handler = functools.partial(_QuietHandler, directory=directory)
        # A STABLE PORT keeps the origin (http://127.0.0.1:PORT) identical across relaunches — and
        # localStorage is PER-ORIGIN, so binding a random port silently wiped read-state, starred
        # countries and cached profiles on every restart. Try a few fixed ports (reusing the address so a
        # quick relaunch, with the old socket still in TIME_WAIT, doesn't fail); an ephemeral port is only
        # a last resort (and it loses persistence, so it should essentially never be reached).
        class _Srv(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True
        httpd = None
        for _p in (49731, 49732, 49733, 8137, 0):
            try:
                httpd = _Srv(("127.0.0.1", _p), handler)
                break
            except OSError:
                httpd = None
        if httpd is None:
            return None
        port = httpd.server_address[1]
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return "http://127.0.0.1:%d/%s" % (port, urllib.parse.quote(fname))
    except Exception:
        return None


def main():
    if not os.path.exists(APP_HTML):
        webview.create_window("Meridian", html="<body style='font-family:Segoe UI;padding:40px;color:#333'>"
                              "<h2>Meridian content not found</h2><p>Expected: <code>" + APP_HTML + "</code></p></body>")
        webview.start()
        return

    api = Api()
    # A per-launch cache-buster (?v=<time>) forces WebView2 to load the CURRENT meridian-relief.html every
    # start instead of a stale cached copy — otherwise edits (and removed features) can silently linger. The
    # query string doesn't change the origin, so localStorage/persistence is untouched.
    _served = _serve_http()
    if _served:
        url = _served + ("&" if "?" in _served else "?") + "v=" + str(int(time.time()))
    else:
        url = "file:///" + APP_HTML.replace("\\", "/") + "?v=" + str(int(time.time()))
    # Blank title-bar TEXT (the window keeps its bar + minimize/maximize/close and drag). The in-page
    # MERIDIAN header identifies the app, and the globe stays as the window/taskbar icon — so the OS
    # title bar no longer repeats "Meridian" right above the app's own header.
    webview.create_window(
        "",
        url=url, js_api=api,
        width=1500, height=950, min_size=(1000, 650), background_color="#e9edf1",
    )
    # PERF: persist the WebView cache (map tiles, CDN libraries, images) across relaunches, in a LOCAL
    # folder (never OneDrive), so a relaunch loads them from disk instead of re-downloading everything.
    # private_mode=False keeps the store on disk; the try/except tolerates older pywebview builds.
    _store = os.path.join(os.environ.get("LOCALAPPDATA") or BASE_DIR, "Meridian", "webview")
    try:
        os.makedirs(_store, exist_ok=True)
    except Exception:
        pass
    try:
        webview.start(private_mode=False, storage_path=_store)
    except TypeError:
        webview.start()


if __name__ == "__main__":
    main()
