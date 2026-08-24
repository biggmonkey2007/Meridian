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
import gzip
import urllib.request
import urllib.parse

# Let WebView2 use the GPU (and fall back to software if ever needed) so WebGL/globe runs.
os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = "--ignore-gpu-blocklist --enable-unsafe-swiftshader --autoplay-policy=no-user-gesture-required" + (" --remote-debugging-port=9222" if os.environ.get("MERIDIAN_DEBUG") else "")

try:
    import webview                       # desktop shell only; the feed SERVER imports this module headless
except Exception:                        # (no pywebview / no GUI backend on a VPS) — that's fine, main() isn't run there
    webview = None

# Paths work both as a plain script AND as a bundled .exe (PyInstaller). When frozen, read-only resources
# (the HTML UI) are unpacked to sys._MEIPASS, while everything WRITABLE — the caches, the user-editable
# channels.txt, an optional key — must live in %LOCALAPPDATA%\Meridian so it persists and isn't wiped when
# the temp bundle is cleaned up. As a script, both are just the folder next to this file.
if getattr(sys, "frozen", False):
    RES_DIR  = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(sys.executable)))
    DATA_DIR = os.path.join(os.environ.get("LOCALAPPDATA") or os.path.expanduser("~"), "Meridian")
else:
    RES_DIR  = os.path.dirname(os.path.abspath(__file__))
    # writable data (caches, the 30-day summary cache) can be redirected to a persistent volume on the feed
    # server via MERIDIAN_DATA, while read-only resources (gazetteer, HTML) still load from the repo dir.
    DATA_DIR = os.environ.get("MERIDIAN_DATA") or RES_DIR
BASE_DIR  = RES_DIR                                       # back-compat alias for resource lookups
APP_HTML  = os.path.join(RES_DIR, "meridian-relief.html")
KEY_FILE  = os.path.join(DATA_DIR, "gemini_key.txt")
CACHE_DIR = os.path.join(DATA_DIR, "cache")
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

# ── VERSION + AUTO-UPDATE ─────────────────────────────────────────────────────────────────────────
# Single source of truth for the app version (installer + updater both read it).
APP_VERSION = "1.4.52"
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


# THIN-CLIENT / SCALE. When this points at a hosted feed (e.g. "https://feed.example.com"), the app stops
# building the world map itself and just FETCHES the server-built JSON — one fast, CDN-cacheable request
# that serves every user identically. That is what lets the same backend hold millions of users (and feed
# an iOS/Android app) instead of every copy independently hammering GDELT/Wikidata/Telegram. Empty =
# self-contained local build (today's behaviour), so the app always works even with no server.
FEED_BASE_DEFAULT = ""


def _feed_base():
    try:
        v = (os.environ.get("MERIDIAN_FEED_BASE") or "").strip()
        if v:
            return v
        cfg = os.path.join(DATA_DIR, "feed_base.txt")   # drop-in, no rebuild — mirrors update_repo.txt
        if os.path.exists(cfg):
            v = open(cfg, encoding="utf-8").read().strip()
            if v:
                return v
    except Exception:
        pass
    return FEED_BASE_DEFAULT


def _ver_tuple(v):
    """'v1.2.3' / '1.2.3' -> (1,2,3) for comparison; junk -> (0,)."""
    nums = re.findall(r"\d+", re.sub(r"^v", "", (v or "").strip(), flags=re.I))
    return tuple(int(x) for x in nums[:4]) or (0,)


def _is_newer(remote, local):
    return _ver_tuple(remote) > _ver_tuple(local)

GEMINI_MODEL = "gemini-flash-latest"   # a ROLLING alias Google keeps pointed at the current free Flash model
                                       # (so a version retirement like 2.0->2.5->3.x can't 404 us). Self-healing.


def load_gemini_key():
    """The Google Gemini API key — a SECOND free AI alongside Groq (its free tier allows ~1,500 requests/day,
    enough to summarise a whole feed). From env GEMINI_API_KEY, else gemini_key.txt OR gemini.txt in DATA_DIR
    (gitignored). Absent -> "" and every Gemini path stays off."""
    k = (os.environ.get("GEMINI_API_KEY") or "").strip()
    if k:
        return k
    for fn in ("gemini_key.txt", "gemini.txt"):
        try:
            p = os.path.join(DATA_DIR, fn)
            if os.path.exists(p):
                v = open(p, encoding="utf-8").read().strip()
                if v:
                    return v
        except Exception:
            pass
    return ""


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:80]


def _clip(s, n=360):
    """Trim to <= n chars at a WORD boundary — never mid-word. No ellipsis (the UI adds one if the text
    still ends mid-sentence)."""
    s = s or ""
    if len(s) <= n:
        return s
    w = s.rfind(" ", 0, n)
    return (s[:w] if w > n * 0.6 else s[:n]).rstrip()


def _clip_sentence(s, n=460):
    """Like _clip, but end on a WHOLE sentence so a card never trails off '…' mid-thought. Cut at the last
    ./!/? before n (keeping a closing quote/paren); only if there's no sentence end in range fall back to a
    word boundary (and _end_stop then gives it a period)."""
    s = (s or "").strip()
    if len(s) <= n:
        return s
    head = s[:n]
    m = re.search(r"^[\s\S]*[.!?][\"'”’)\]]*(?=\s|$)", head)
    if m and len(m.group(0)) > n * 0.5:
        return m.group(0).strip()
    w = head.rfind(" ")
    return (head[:w] if w > n * 0.6 else head).rstrip()


def _share_id(url, title=""):
    """A short, stable, URL-safe id for a story's shareable page — the SAME story always maps to the same
    id (keyed by the article URL, or the title when there's none), so a re-shared link keeps its emblem."""
    return hashlib.sha1(((url or title or "").strip().lower()).encode("utf-8")).hexdigest()[:12]


SUMMARY_MODEL = os.environ.get("SUMMARY_MODEL", "gpt-4o-mini")
# FRESH RESWEEP ON EVERY UPDATE. Bump this ONE constant on every shipped change to the news/AI pipeline and
# the next launch throws away all stale data and re-does everything from scratch: the feed is rebuilt live
# (re-fetch the wire, re-geolocate with the new rules, re-apply the importance gate) and every AI product —
# summary, location (WHERE) and importance (SCOPE) — is regenerated. It's folded into the feed-cache stamp
# and the summary/aiwhere cache keys, so a fix is visible on the next launch instead of self-healing over
# a later cycle. (The per-feature vers below still exist for targeted invalidation; this is the big hammer.)
_DATA_VER = "d54"   # d54: resweep so a truncated wire headline never ends on "and."/"…" (dangling connector
                    #      trimmed), and summaries round-robin Groq+Gemini so more dots get a real brief.
                    # d53: resweep so the culture/entertainment fluff filter drops non-events (a "British
                    #      podcasters" NYT feature was a UK dot). Cache-hits summaries, no re-cost.
                    # d52: resweep to UNDO the mega-merge — a physical event no longer vacuums co-located
                    #      STATEMENTS (a Kyiv drone dot had absorbed 24 "Zelensky says…" posts + wrong sources);
                    #      _collapse_colocated now needs BOTH dots physical. Cache-hits summaries, no re-cost.
                    # d51: resweep so leader/official STATEMENTS dot their CAPITAL (Putin -> Moscow), the
                    #      'approx' flag stops firing on legit country-level national dots (only region/water
                    #      centroids now), and the 'logistics/infrastructure' clip-filler no longer ties a
                    #      statement to a strike. Cache-hits summaries/AI-where, so no re-summarize cost.
                    # d50: resweep so headlines re-clean keeping a SOURCE attribution tail ("… — platoon
                    #      commander", "… — Zelensky") that was being stripped as if it were a "— Reuters"
                    #      byline, and a missing space after a comma is repaired. Only publisher bylines drop.
                    # d49: resweep so dots carry a geo_confidence field and the self-learning gazetteer starts
                    #      pinning AI-named towns (Deir Seryan, Bayout El Siyad) to real coords via Nominatim,
                    #      remembering each forever. Cache-hits summaries/AI-where, so no re-summarize cost.
                    # d48: resweep so this round's build-time rules take effect — unrelated stories at a
                    #      capital SEAT no longer collapse into one mega-dot (Washington: trade deal + Brazil
                    #      tariffs + 5 ambassador clips were becoming ONE), a bare "Republic" is no longer a US
                    #      town (a Türkiye statement dots Turkey), and channel meta/debunk notes are dropped.
                    # d47: resweep so this round's build-time rules take effect — a legal ruling dots its
                    #      jurisdiction ("UK judge rules" -> UK, not Palestine), routine sports results drop
                    #      (only finals/titles/medals stay), and the merge area-gate checks BOTH places so a
                    #      village-vs-region pair ("Ali al-Taher" vs "Bayout El Siyad") stays two dots.
                    # d46: resweep so the new merge rule takes effect — two DIFFERENT-town strikes that fall
                    #      on the same region centroid ("Southern Lebanon") no longer collapse into one dot
                    #      (new area = new dot). Cache-hits summaries/AI-where, so no re-summarize cost.
                    # d45: resweep so a leader RETURNING from abroad dots their own country (Cameroon, not
                    #      Switzerland), and briefs regenerate under the compressed prompt.
                    # d44: resweep so retrospective/history features drop off the map (ABC "how a health study
                    #      shaped modern medicine") and cutoff briefs re-bake clean.
                    # d43: resweep so a US official's statement about a foreign country dots the US seat (Bessent on
                    #      Iran -> Washington, not Tehran).
                    # d42: resweep so near-miss/explainer/blame non-events drop, the Georgia US-state flag clears,
                    #      and mismatched wire clips detach.
                    # d41: resweep so EU-leader statements re-place on Brussels (von der Leyen was dotting Russia),
                    #      Vucic/Zakharova/Kallas seats apply, and the sea-label / attribution-tail cleanups land.
                    # d40: resweep so feeds rebuild with GEMINI now in the chain — the ~60% of dots Groq's daily
                    #      cap left unsummarised get a real brief on the next build instead of the raw teaser.
                    # d39: resweep so a non-violent espionage/surveillance story recolours from red 'security' to
                    #      'politics' (it scored security only on the word 'mercenary').
                    # d38: resweep so a story dotted on its accused BACKER re-places on the real scene ("UAE funded
                    #      plot… MAB urge UK government" -> the UK, not the UAE) via the new backer-place rule.
                    # d37: resweep so an admin's OPINION post drops off the map (_tg_reliable now screens editorialising)
                    #      and 'Wall Street Journal' stories re-place on their subject instead of the NYC financial district.
                    # d36: resweep so the copyright-hardened brief regenerates and the bare-"Fed" recogniser re-places
                    #      Fed rate stories on the US (an Anadolu-sourced one had dotted Turkey), not the source country.
                    # d35: resweep so the broadened AI dedup folds reworded same-event dots, and its LEARNED verdicts
                    #      apply on COLD START (cache-only) so duplicates don't reappear until the background pass runs.
_SUM_PROMPT_VER = "21"  # 21: prompt COMPRESSED ~2,200->~750 tokens (same functions) so the free tiers cover far
                        #     more of the feed. 20: original-work copyright hardening; 17: longer in-depth briefs; 16 added
                        #     neutral source-attribution of contested claims (state/partisan wires, Telegram)
_AIWHERE_VER = "aw6"    # bump to invalidate the AI location+scope the summary pass emits (keyed by title)
_PORT_VER = "p2"        # bump to invalidate cached port profiles (throughput/vessel figures refresh weekly anyway)
_LEADER_VER = "l9"      # l9: re-resolve everyone with fresh photos (l6-l8 were no-ops — a duplicate _LEADER_VER
                        # later in the file had shadowed this one; now split into _LEADER_NAME_VER)


def _aiwhere_path(title):
    # DELIBERATELY NOT keyed by _DATA_VER. The AI's location for a dot is stable regardless of feed-content
    # bumps, so tying it to _DATA_VER wiped every AI pinpoint on EVERY update — the cold-start feed then fell
    # back to rules-only until the background AI re-ran, which is why locations kept regressing after a fix.
    # Now the AI WHERE persists across data-version bumps; bump _AIWHERE_VER only when the location logic itself
    # changes.
    return os.path.join(CACHE_DIR, "aiwhere_" + hashlib.sha1(
        (_AIWHERE_VER + "\n" + (title or "")).encode("utf-8")).hexdigest()[:16] + ".json")


def _ai_where(title):
    """The location the AI named while writing this story's brief (one call did both) — a plain place name
    ('City, Country'/'Country') the caller grounds through the gazetteer, or "" if not summarised yet. Read
    only (no AI call): `_summarize` writes it, `_locate` reads it, so on the next build the dot moves to the
    AI's pinpoint. Keyed by TITLE so `_locate` (which lacks the article body) can look it up."""
    p = _aiwhere_path(title)
    if _fresh(p, 30 * 86400):
        try:
            return json.load(open(p, encoding="utf-8")).get("p", "")
        except Exception:
            pass
    return ""


def _ai_scope(title):
    """How far this story's consequences reach — 'global'|'regional'|'national'|'local'|'' — as the AI rated
    it while writing the brief (same call, same cache file as _ai_where, no extra request). The world map
    shows global/regional/national and HIDES 'local' (true-but-minor stories: a beach eroding, a local
    crime); the starred-country feed still carries everything. "" = not summarised yet -> treated as shown."""
    p = _aiwhere_path(title)
    if _fresh(p, 30 * 86400):
        try:
            return (json.load(open(p, encoding="utf-8")).get("sc", "") or "").lower()
        except Exception:
            pass
    return ""


def _summary_cfg():
    """(api_key, endpoint, model). Just drop your key in summary_key.txt (DATA_DIR) or set SUMMARY_API_KEY —
    a Groq key (starts with 'gsk_') auto-routes to Groq's free API; anything else defaults to OpenAI. Override
    the endpoint/model with SUMMARY_API_URL / SUMMARY_MODEL if you use a different provider. No key -> we fall
    back to a local model (see _local_llm); failing that, summaries are off (safe attributed-lead + link)."""
    key = (os.environ.get("SUMMARY_API_KEY") or "").strip()
    if not key:
        try:
            p = os.path.join(DATA_DIR, "summary_key.txt")
            if os.path.exists(p):
                key = open(p, encoding="utf-8").read().strip()
        except Exception:
            pass
    is_groq = key.startswith("gsk_")                         # Groq keys are 'gsk_...' -> use Groq's free endpoint + model
    url = (os.environ.get("SUMMARY_API_URL")
           or ("https://api.groq.com/openai/v1/chat/completions" if is_groq
               else "https://api.openai.com/v1/chat/completions")).strip()
    # Groq retired the Llama-3.1/3.3 line (the old default 404s now — "model_not_found" — which silently
    # killed EVERY summary). openai/gpt-oss-20b is the current fast, capable Groq model; it's a reasoning
    # model, so _llm_complete asks for low reasoning effort and gives it token headroom.
    model = (os.environ.get("SUMMARY_MODEL")
             or ("openai/gpt-oss-20b" if is_groq else "gpt-4o-mini")).strip()
    return key, url, model


_LOCAL_LLM = None   # probe result, cached for the process: (url, model) once found, or False if none


def _local_llm():
    """The FREE, UNLIMITED summarizer: a locally-running Ollama (ollama.com). No API key, no rate limit, no
    per-call cost — the model runs on this machine (or, at scale, on the feed server). If Ollama is up with a
    model pulled, summaries 'just work'; otherwise we return None and the app stays on the safe lead + link.
    Probe once per process (a localhost GET that fails instantly when Ollama isn't installed)."""
    global _LOCAL_LLM
    if _LOCAL_LLM is not None:
        return _LOCAL_LLM or None
    _LOCAL_LLM = False
    host = (os.environ.get("OLLAMA_HOST") or "http://127.0.0.1:11434").strip().rstrip("/")  # 127.0.0.1, not localhost (skips the slow IPv6 ::1 attempt)
    if "://" not in host:
        host = "http://" + host
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))               # bypass any system proxy for the localhost probe
        j = json.loads(opener.open(host + "/api/tags", timeout=1.2).read().decode("utf-8", "replace"))
        names = [(m.get("name") or "") for m in (j.get("models") or []) if m.get("name")]
        if not names:
            return None
        pref = ("llama3.2", "qwen2.5", "llama3.1", "gemma2", "phi", "mistral", "qwen", "llama3", "gemma", "llama")
        pick = next((n for p in pref for n in names if n.lower().startswith(p)), names[0])
        _LOCAL_LLM = (host + "/v1/chat/completions", pick)
        return _LOCAL_LLM
    except Exception:
        return None


def _llm_available():
    """Is there ANY free LLM to call — a Groq/OpenAI key, or a local Ollama? Gates every optional AI
    feature (summaries, the geolocation fallback) so they stay purely additive: no LLM -> no cost, no
    behaviour change."""
    return bool(_summary_cfg()[0]) or bool(load_gemini_key()) or bool(_local_llm())


# Google Gemini speaks an OpenAI-COMPATIBLE endpoint, so the same _llm_one path serves it — just a different
# base URL and a Bearer key. This is the second free AI: Groq is fast but capped at ~200k tokens/day; Gemini's
# free tier allows ~1,500 requests/day, plenty to summarise a whole feed's overflow.
_GEMINI_OPENAI_URL = "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"


def _llm_providers():
    """The ordered hosted-LLM chain to try: the PRIMARY (Groq/OpenAI from _summary_cfg) FIRST — fast and free —
    then GEMINI, the free backup that picks up the overflow once Groq's small daily token cap is spent. Each
    entry is (name, key, url, model). Empty when no hosted key is set (caller may then fall back to Ollama)."""
    out = []
    key, url, model = _summary_cfg()
    if key:
        out.append(("primary", key, url, model))
    gk = load_gemini_key()
    if gk:
        out.append(("gemini", gk, _GEMINI_OPENAI_URL, GEMINI_MODEL))
    return out


def _llm_one(name, key, url, model, system, user, max_tokens, temperature):
    """One completion against ONE provider. Returns the assistant text (stripped) or "" on any error
    (including a rate-cap 429). Never raises — an empty return is the signal to try the next provider."""
    timeout = 45 if key == "local" else 25
    payload = {"model": model, "temperature": temperature, "max_tokens": max_tokens,
               "messages": [{"role": "system", "content": system},
                            {"role": "user", "content": user}]}
    # REASONING MODELS (Groq's gpt-oss): they burn output tokens on hidden reasoning FIRST, so a tight
    # max_tokens leaves an EMPTY answer. Ask for low reasoning effort and lift the ceiling so the visible
    # answer always fits. (Only gpt-oss honours these; Gemini and others ignore them.)
    if "gpt-oss" in model:
        payload["reasoning_effort"] = "low"
        payload["max_tokens"] = max(max_tokens, 1200) + 700
        timeout = max(timeout, 40)
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key,
               "Accept": "application/json", "User-Agent": "Mozilla/5.0"}
    for attempt in range(2):                 # Gemini's free tier is ~20 req/MIN — a burst 429s; wait the backoff once
        try:
            req = urllib.request.Request(url, data=body, headers=headers)
            j = json.loads(urllib.request.urlopen(req, timeout=timeout).read().decode("utf-8", "replace"))
            msg = (j.get("choices") or [{}])[0].get("message", {}) or {}
            return (msg.get("content") or "").strip()
        except urllib.error.HTTPError as ex:
            if getattr(ex, "code", None) == 429 and attempt == 0:
                try:
                    m = re.search(r"retry in ([0-9.]+)s", ex.read().decode("utf-8", "replace"))
                    wait = min(float(m.group(1)) + 0.5, 12) if m else 6
                except Exception:
                    wait = 6
                time.sleep(wait)
                continue                     # one retry after the rate-limit window
            return ""
        except Exception:
            return ""
    return ""


_llm_rr = [0]   # round-robin cursor across providers (see `spread` below)


def _llm_complete(system, user, max_tokens=300, temperature=0.3, model=None, prefer=None, spread=False):
    """ONE chat completion over the free LLM path, with automatic FAILOVER. Tries providers in order —
    primary (Groq/OpenAI), then Gemini (a free backup) — and returns the FIRST non-empty answer, so a Groq
    daily-cap 429 (which comes back empty) rolls straight to Gemini instead of losing the brief. `model`
    overrides the model on whichever provider serves the call. `prefer` (e.g. 'gemini') moves that provider
    to the FRONT for THIS call — used to get an INDEPENDENT second opinion on a dot's location from a
    different model family — while still allowing fallback to the others. With no hosted key it falls back to
    a local Ollama. Returns "" only if EVERY provider fails. Never raises. Shared by summaries, dedup and geo
    so there is one UA quirk and one timeout policy."""
    provs = _llm_providers()
    if prefer:
        provs.sort(key=lambda p: 0 if p[0] == prefer else 1)     # stable: preferred first, rest keep order
    elif spread and len(provs) > 1:
        # ROUND-ROBIN across providers so BOTH free daily budgets are spent, not just Groq's small one — this
        # ~doubles how many summaries get an AI brief before anything caps (the user's "use both AIs
        # interchangeably"). Failover still applies: if the chosen leader is capped/empty, it rolls to the next.
        n = _llm_rr[0] % len(provs)
        _llm_rr[0] = (_llm_rr[0] + 1) % 100000
        provs = provs[n:] + provs[:n]
    if not provs:                                                # no hosted key -> the free local Ollama
        loc = _local_llm()
        if not loc:
            return ""
        provs = [("local", "local", loc[0], loc[1])]
    for name, key, url, pmodel in provs:
        m = model or pmodel
        if not m:
            continue
        out = _llm_one(name, key, url, m, system, user, max_tokens, temperature)
        if out:
            return out
    return ""


_END_PUNCT = ".!?\"'’”)]…"   # a brief that ends on one of these reads as a finished thought


# A brief that ends in a source TRUNCATION marker + a dangling attribution ("…and caused explosion, Civil
# Defense says…") is NOT finished — but its last char is a "." (from "…"), which made _finish_brief think it
# WAS. Strip that tail first. This is the backend twin of the frontend renderBrief `_cut`: the recurring
# "articles cut off with …" bug lived here because the summary can echo a truncated wire teaser.
_ATTR_TAIL_RE = re.compile(
    r",\s+(?:according to\s+[\w'’.\- ]{2,45}"
    r"|(?:the\s+)?[\w'’.\- ]{2,45}?\s+(?:has|have|had|reports?|reported|says?|said|tells?|told|learns?|learned"
    r"|writes?|wrote|adds?|added|notes?|noted|confirms?|confirmed|announces?|announced|claims?|claimed))\s*$", re.I)


def _finish_brief(s):
    """Guarantee a brief ends whole — never mid-word/mid-sentence (the model can stop at the token cap). Only
    the LAST line can be cut, so we mend just that: if it holds a sentence-ender, trim to it; if it has none,
    it's a dangling fragment (an incomplete final bullet) -> drop that one line and keep the complete lines
    above it. We keep the trim only when a real brief survives, so a short whole brief is never gutted."""
    s = (s or "").rstrip()
    _s2 = _ATTR_TAIL_RE.sub("", re.sub(r"\s*(?:\.{2,}|…)+\s*$", "", s)).rstrip()   # drop a trailing "…" + attribution
    if _s2 and _s2 != s:
        s = _s2
    if not s or s[-1] in _END_PUNCT:
        return s
    lines = s.split("\n")
    last = lines[-1]
    m = list(re.finditer(r"[.!?][\"'’”)\]]*", last))
    if m:
        lines[-1] = last[:m[-1].end()].rstrip()     # cut the dangling fragment off the final line
    else:
        lines = lines[:-1]                           # whole final line is an incomplete fragment -> drop it
    out = "\n".join(lines).rstrip()
    return out if len(out) >= 15 else s


def _summarize(title, text, source="", depth=False):
    """Meridian's OWN copyright-free summary — 2-3 original sentences generated from the facts (facts aren't
    copyrightable; the wording is newly written, not copied). Cached 30 days per story. Returns "" when no
    LLM key is configured or on any error, so the caller falls back to the safe attributed lead + link.
    `source` is the reporting outlet (TASS, a Telegram channel, Reuters) — handed to the model so a contested
    claim from a state/partisan wire is ATTRIBUTED ('Russia's TASS says…'), never stated as neutral fact.
    `depth` = the outlet reports at length (NYT, WaPo, Reuters…), so the brief may run a little longer to
    carry its quotes/figures — it reads more of the article and lifts the paragraph cap."""
    text = (text or "").strip()
    source = (source or "").strip()
    if not (title or text) or not _llm_available():
        return ""
    text = text[:7000 if depth else 4500]
    # CACHE KEY — deliberately NOT keyed by _DATA_VER. A brief depends only on the PROMPT (_SUM_PROMPT_VER),
    # the depth flag, the source outlet, and the story text — never on the feed-content version. Keying it by
    # _DATA_VER (which bumps on almost every update) threw away EVERY cached brief on each bump and forced a
    # full ~190-story, ~500k-token re-summarize that blows Groq's 200k-tokens/DAY free cap — the real reason
    # summaries kept hitting the wall. Same fix as the AI-WHERE cache (_aiwhere_path dropped _DATA_VER): key on
    # the inputs that actually change the output, so briefs PERSIST across content bumps and regenerate ONLY
    # when the prompt changes (bump _SUM_PROMPT_VER for that — it already forces a one-time regen on its own).
    cache = os.path.join(CACHE_DIR, "sum_" + hashlib.sha1((_SUM_PROMPT_VER + "\n" + ("D" if depth else "") + source + "\n" + title + "\n" + text).encode("utf-8")).hexdigest()[:16] + ".json")
    if _fresh(cache, 30 * 86400):
        try:
            return _drop_redundant_bullets(_drop_empty_bullets(_fix_speaker_colon(
                json.load(open(cache, encoding="utf-8")).get("s", ""))))
        except Exception:
            pass
    # COMPRESSED PROMPT (2026-08-21): the old prompt ran ~2,200 tokens of instructions on EVERY call, so the free
    # tiers (Groq 200k tokens/day; Gemini 20 req/min) covered only a fraction of a ~190-dot feed and the rest fell
    # back to the raw teaser. This tight version (~750 tokens) keeps every FUNCTION — original-work copyright,
    # neutral attribution, the shape, and the WHERE + SCOPE metadata lines — at ~1/3 the tokens, so far more of the
    # feed gets a real brief. (Bump _SUM_PROMPT_VER whenever this changes.)
    system = ("You are a senior wire editor (AP/Reuters quality) writing tight, ORIGINAL, copyright-free news "
              "briefs in the Axios 'Smart Brevity' style — clean punctuation, no source or channel name in the "
              "prose, every line composed from scratch in your own words (never the source's), shaped to the "
              "story rather than a fixed template.")
    prompt = ("Write an ORIGINAL news brief of the story below for a world-news map — YOUR OWN composition built "
              "from the FACTS, never a rewrite or paraphrase. Write for a sharp 8th-grade reader: short plain "
              "words, short active sentences. It must ADD to the headline; if there is nothing to add, write ONE "
              "sentence or none — never pad. Explain in a few plain words any group/party/agency/official/place a "
              "newcomer wouldn't know ('UNIFIL, the UN peacekeeping force in Lebanon').\n"
              "SHAPE: a 1-3 sentence prose lede that says what happened; then, ONLY if the story is rich enough, "
              "1-2 more short paragraphs of the next most important detail; then optionally 1-2 bullets ('- ...') "
              "for a hard specific the prose did NOT already give (a bullet may open with a 1-2 word bold label "
              "like '**Scale:**'). Add a final '- Why it matters: ...' bullet ONLY if the stakes aren't obvious. "
              "No dateline ('TEHRAN —'), no heading, no title, no source tag in the prose. Report what verifiably "
              "happened; don't hedge a plain fact with 'reportedly', and don't point out what the source omits.\n"
              + ("IN-DEPTH SOURCE: the text below is rich — you MAY run up to ~5 short paragraphs to carry its key "
                 "quotes and hard figures, still tight.\n" if depth else "")
              + "COPYRIGHT (a legal requirement, ABSOLUTE): facts are free — build the brief from them in wording "
              "and sentence order that are entirely your OWN. NEVER copy 4+ consecutive words of the source and "
              "never mirror its phrasing. You may reproduce a PERSON'S own distinctive words (<=15) inside real "
              "quotation marks, attributed to the speaker — never the article's own prose. Include one short "
              "paragraph of your OWN general-knowledge context. Re-express even a bare source description from "
              "scratch. This keeps the brief fair-use across the US, EU, UK, Canada and Australia.\n"
              "NEUTRAL & ATTRIBUTED: treat any contested claim, accusation, casualty figure or loaded framing as "
              "a CLAIM — attribute it to WHO said it and name the outlet when the story is one side's telling "
              "('Moscow claims', 'according to Russia's TASS', 'Ukraine's army says'). Keep loaded epithets "
              "('regime', 'terrorist', 'martyr', 'liberated') out of your OWN voice; apply this to EVERY source "
              "equally. When the story IS a person's statement, write it as attributed reported speech naming the "
              "speaker, their distinctive words in quotation marks.\n"
              "Then output TWO metadata lines, each on its own line, NOT part of the brief:\n"
              "WHERE: the ONE place the event PHYSICALLY happened — 'City, Country', else 'Country', else NONE. "
              "NONE for a broad multi-place analysis/round-up. A sea/gulf/strait/river ONLY when the event itself "
              "happens ON or OVER it. A named facility -> the CITY it sits in. NEVER a place named only for "
              "comparison/distance/backdrop, where someone merely REACTED, a person's nationality, or the home "
              "country of an org/charity/company merely named ('CARITAS Canada' -> the project's country).\n"
              "SCOPE: global | national | regional | local — by CONSEQUENCE, not drama. global=a war/strike "
              "between states, a major-power or minister decision or consequential statement, a market-moving "
              "policy, a major disaster. national=changes one country (its government, a national election/policy, "
              "a coup, a mass-casualty attack). regional=shifts a region. local=a minor local or human-interest "
              "story with no wider consequence.\n\n"
              "SOURCE OUTLET: " + (source or "unknown") + "\n"
              "HEADLINE: " + (title or "") + "\n\nSOURCE TEXT:\n" + text)
    s = _llm_complete(system, prompt, max_tokens=(1000 if depth else 620), temperature=0.3, spread=True)   # depth: ~5 short paras; spread across BOTH providers so the daily budgets stack
    # Keep the line/bullet STRUCTURE (Axios format) — collapse only intra-line runs of spaces/tabs, trim
    # each line, and cap blank runs at one. (A blanket \s+->' ' would flatten the bullets.)
    s = s.replace("\r", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = "\n".join(ln.strip() for ln in s.split("\n"))
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    s = _PROMO_URL.sub(" ", s)          # a URL never belongs in the brief (belt-and-braces; the source text is cleaned too)
    s = re.sub(r"(?im)^\s*(?:source|link|via|read)\s*:?\s*$", "", s).strip()   # a dangling "Source:" left after the url was cut
    s = _LEAD_DATELINE.sub("", s)       # belt-and-braces: drop a wire dateline if the model opened with one anyway
    # Pull the WHERE + SCOPE lines back OUT of the brief and cache them (keyed by title) — one AI call gave us
    # the brief, the location (for _locate) AND the importance (for the world-map gate), no extra request.
    where, scope = "", ""
    mw = re.search(r"(?im)^\s*WHERE:\s*(.+?)\s*$", s)
    if mw:
        where = re.sub(r'^[\s"\'.*\-]+|[\s"\'.\-]+$', "", mw.group(1)).strip()
        if not (3 <= len(where) <= 60) or where.upper() == "NONE":
            where = ""
    ms = re.search(r"(?im)^\s*SCOPE:\s*([a-z]+)\s*$", s)
    if ms:
        _sc = ms.group(1).lower()
        scope = _sc if _sc in ("global", "regional", "national", "local") else ""
    s = re.sub(r"(?im)^\s*(WHERE|SCOPE):.*$", "", s).strip()   # strip both metadata lines from the brief
    s = re.sub(r"\n{3,}", "\n\n", s).strip()
    s = _finish_brief(s)                                        # never leave a brief cut off mid-sentence
    s = _fix_speaker_colon(s)                                   # "Bennett: Qatar is…" -> "Bennett says Qatar is…"
    s = _drop_empty_bullets(s)                                  # drop filler bullets ("- Damage: unknown")
    s = _drop_redundant_bullets(s)                              # drop bullets that just restate the lede
    if where or scope:
        try:
            json.dump({"p": where, "sc": scope}, open(_aiwhere_path(title), "w", encoding="utf-8"))
        except Exception:
            pass
    if s:
        try:
            json.dump({"s": s}, open(cache, "w", encoding="utf-8"))
        except Exception:
            pass
    return s


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


# vxTwitter / fixvx repost chrome. These bots re-embed an X/Twitter post as
#   [reposter's throwaway take]  \n  <x.com link>  \n  vxTwitter / fixvx  <emoji+count reactions>
#   \n  <Author (@handle)>  \n  <the actual tweet>  <emoji+count reactions>
# The reposter's take is NOT the story ("This is a war crime dawg" went on the map); the embedded tweet is.
_TG_EMO = ("\U0001F000-\U0001FAFF\U00002600-\U000027BF\U00002B00-\U00002BFF"
           "\U00002190-\U000021FF\U0000FE0F\U0000200D\U000020E3")
_TG_REACTIONS = re.compile(r"(?:[" + _TG_EMO + r"]+\s?\d+\s*){1,}")           # "💋 88 📩 36", "👺35 🤣11🖤9"
_TG_EMBED_MARK = re.compile(r"(?i)\b(vx\s*twitter|fx\s*twitter|fix(?:up|v)x|nitter)\b")
_TG_URL_ONLY = re.compile(r"(?i)^\W*(https?://)?(www\.)?(x|twitter|t|fxtwitter|vxtwitter|fixupx)\.(com|co)/\S*\W*$")
_TG_EMBED_AUTHOR = re.compile(r"^[\w .,'’\-]{2,40}\s*\(@?[A-Za-z0-9_]*\)\W*$")   # "Jungle Journey (@handle)" byline
_TG_XLINK = re.compile(r"(?im)^\W*(https?://)?(www\.)?(x|twitter|vxtwitter|fxtwitter|fixupx)\.com/\S+")
_TG_AD = re.compile(r"(?i)(rainbet|non-kyc|casino|sportsbook|promo code|use code|deposit bonus|betting|\bt\.me/\+|📲|referral)")


# Image/agency credit tags a wire leaves inline: "[Photo/AA]", "[Screengrab/AA]", "[File photo]",
# "[Iranian Presidency / Handout – Anadolu Agency]", "[Getty Images]", "[REUTERS]", "(AFP)". A bracket is a
# credit when it holds a slash OR a media/agency word — strip it. Plain editorial brackets ("[sic]", a
# clarifying "[Gaza]") have neither, so they survive.
_CREDIT_BRACKET = re.compile(
    r"[\[(][^\])]*(?:/|photo|screengrab|screenshot|video|footage|handout|getty|reuters|"
    r"\bafp\b|\bap\b|\bepa\b|anadolu|\baa\b|file|image|imagery|courtesy|graphic|infographic|"
    r"illustration|archive|social ?media|stringer|\bpool\b)[^\])]*[\])]", re.I)
# The decorative tag these channels stamp on the FRONT of a post: a country-flag emoji ("🇾🇪 - …"), any other
# emoji/symbol ("🤝", "⚡", "🔴"), and/or a short country code ("SA — ", "IL/US — "). It's decoration, not
# content — the story already flies its country's flag in the header — so strip it. Rendered as raw text a
# flag's two regional-indicator letters leak in as "ye - ", and "🤝 SA —" leaves "SA —" behind.
_LEAD_FLAG  = re.compile(r"^(?:[\U0001F1E6-\U0001F1FF]\s*){1,6}[\s\-–—:|•·]*")
_LEAD_EMOJI = re.compile(r"^(?:[\U0001F000-\U0001FAFF☀-➿⬀-⯿←-⇿︀-️‍⃣]\s*)+")
_LEAD_CC    = re.compile(r"^[A-Z]{2,3}(?:[ /][A-Z]{2,3}){0,3}\s*[-–—:|•·]\s+")   # a country code + dash the emoji tag left
# A reposter's attribution STAMP on the front of a quoted statement — "President Trump via Truth Social:",
# "Donald Trump on Truth Social —", "Netanyahu via Telegram:". The header already names the source and the
# speaker, so this preface is just clutter in the body. Strip a leading "<name/title> via|on <platform>:".
# Anchored to a KNOWN platform after via/on, so ordinary prose ("A report on climate change:") is untouched.
_LEAD_VIA = re.compile(
    r"^\s*['\"“”]?[A-Z][^:>\n]{0,60}?\s(?:via|on)\s+"
    r"(?i:truth\s*social|telegram|twitter|facebook|instagram|threads|rumble|gab|parler|weibo|tiktok|youtube|x)"
    r"\s*[:\-–—]\s+")


def _strip_lead_flag(t):
    t = t or ""
    stripped = _LEAD_EMOJI.sub("", _LEAD_FLAG.sub("", t)).lstrip(" \t")
    if stripped != t:                       # an emoji/flag tag was removed -> a trailing country code is part of it
        stripped = _LEAD_CC.sub("", stripped)
    stripped = stripped.lstrip(" \t-–—:|•·")
    stripped = _LEAD_VIA.sub("", stripped)  # drop a reposter's "Name via Platform:" attribution stamp
    return stripped


# A source's trailing truncation stamp — "… who died last month. Hegseth [...]", "(…)" — means the outlet cut
# the text mid-thought. Drop the stamp AND the incomplete fragment it clipped, so the body ends on a whole
# sentence instead of a dangling '[...]'. Only touches text that actually carries the stamp.
_TRUNC_TAIL = re.compile(r"\s*[\[(]\s*(?:\.{2,}|…)\s*[\])]\s*$")


def _strip_trunc(t):
    t = (t or "").rstrip()
    if _TRUNC_TAIL.search(t):
        return _finish_brief(_TRUNC_TAIL.sub("", t).rstrip())   # cut back to the last complete sentence
    return t


# Role words that mark the text before a colon as a SPEAKER's label, not a topic tag. A statement that opens
# "Former Israeli PM Naftali Bennett: Qatar is…" reads as a raw label; turning the colon into reported speech
# ("… Bennett says Qatar is…") is cleaner while keeping the quote verbatim.
_TITLE_WORDS = {"pm", "president", "minister", "secretary", "chancellor", "premier", "spokesman",
                "spokeswoman", "spokesperson", "ambassador", "envoy", "general", "chief", "leader",
                "official", "officials", "senator", "governor", "mayor", "king", "queen", "prince",
                "pope", "ayatollah", "chairman", "chairwoman", "commander", "ceo", "director", "diplomat",
                "adviser", "advisor", "aide", "representative", "congressman", "congresswoman", "lawmaker",
                "judge", "justice", "admiral", "colonel", "captain", "sheikh", "emir", "sultan", "premier"}
_SPEAKER_COLON = re.compile(r"^\s*([^:\n]{2,70}?)\s*:\s+(?=[\"'“(\w])")
_TO_OUTLET = re.compile(r"\s+to\s+[A-Z][\w'’&.\-]*(?:\s+[A-Z][\w'’&.\-]*){0,2}$")   # "Trump TO AXIOS" -> the outlet
# A quote that begins with one of these reads better lowercased after "says" ("says we will…"); a proper noun
# ("says Qatar is…") is not here, so it keeps its capital. "I" is deliberately absent — it stays capitalised.
_COMMON_LOWER = {"we", "our", "they", "their", "them", "it", "its", "this", "that", "there", "he", "she",
                 "you", "your", "his", "her", "my", "the", "a", "an", "but", "and", "so", "if", "as", "at"}


def _fix_speaker_colon(text):
    """A verbatim statement that opens 'Speaker Title Name: <quote>' reads as a raw label. Turn the colon into
    natural reported speech — 'Speaker Title Name says <quote>' — keeping the quote itself verbatim. Fires only
    when the pre-colon attribution is short AND is a speaker: it names a ROLE (PM/President/Minister/Spokesman…)
    or a KNOWN official. So 'BREAKING:', 'Gaza:', 'Note:' and a plain topic label are left alone."""
    m = _SPEAKER_COLON.match(text or "")
    if not m:
        return text
    attrib = m.group(1).strip()
    if not re.search(r"[A-Za-z0-9]$", attrib):        # attribution must end on a word (a name), not punctuation
        return text
    words = re.findall(r"[A-Za-z]+", attrib.lower())
    if not (1 <= len(words) <= 9):
        return text
    if not (any(w in _TITLE_WORDS for w in words) or _onrecord_statement_country(text) is not None):
        return text
    attrib = _TO_OUTLET.sub("", attrib).strip() or attrib     # "Trump to Axios" -> "Trump" (outlet is the Source)
    rest = text[m.end():]
    lead = re.match(r"[A-Za-z]+", rest)
    if lead and lead.group(0).lower() in _COMMON_LOWER:       # lowercase a common opener; proper nouns keep caps
        rest = rest[0].lower() + rest[1:]
    return attrib + " says " + rest


# A bullet that only says a fact is missing carries NO information — "- Damage: The extent is unknown",
# "- What's next: The refinery's status is unclear". It's pure padding, so drop it. A bullet with any real
# specific (a number) is always kept, so "3 units damaged, extent still unclear" survives.
_EMPTY_BULLET_RE = re.compile(
    r"(?i)\b(?:unclear|unknown|not\s+(?:yet\s+|immediately\s+)?(?:been\s+)?(?:clear|disclosed|released|"
    r"confirmed|specified|determined|available|known|reported|provided|announced|revealed|stated|given|"
    r"established|verified)|remains?\s+(?:to\s+be\s+seen|unclear|unknown)|yet\s+to\s+be\s+(?:determined|"
    r"confirmed|announced|seen|known|released|disclosed|established)|no\s+(?:further|additional)?\s*details?|"
    r"not\s+been\s+made\s+public|is\s+not\s+known|are\s+not\s+known|could\s+not\s+be\s+(?:confirmed|"
    r"determined|verified|reached)|no\s+word\s+on|has\s+not\s+(?:said|commented))\b")


def _drop_empty_bullets(s):
    """Strip bullets that carry no news — a fact declared missing ('the extent is unknown', 'status unclear')
    is filler that just pads the brief. A bullet holding any real specific (a number) is always kept."""
    if not s or "\n" not in s:
        return s
    out = []
    for ln in s.split("\n"):
        st = ln.strip()
        if st[:1] in "-•*" and len(st) > 1 and st[1] in " \t":
            body = re.sub(r"^[-•*]\s*", "", st)
            body = re.sub(r"^\*\*[^*]{1,26}\*\*\s*:?\s*", "", body)      # drop a bold "Label:" prefix
            if _EMPTY_BULLET_RE.search(body) and not re.search(r"\d", body):
                continue                                                # a no-information bullet -> drop it
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _bullet_body(st):
    """The content of a bullet line, without its marker and any bold 'Label:' prefix."""
    body = re.sub(r"^[-•*]\s*", "", st)
    return re.sub(r"^\*\*[^*]{1,26}\*\*\s*:?\s*", "", body)


def _fact_tokens(text):
    """The FACTS in a line — every number, plus its distinctive words. Two lines carrying the same facts have
    the same tokens; a line that adds a new number/name has an extra one."""
    return set(re.findall(r"\d+", text or "")) | (_sigwords(text or "") - _GENERIC_WORDS)


def _drop_redundant_bullets(s):
    """A bullet must ADD to the prose, not restate it. 'Casualties: 16 killed and 23 wounded' under a lede that
    already said 'killing at least 16 and wounding 23' is a pure restatement — drop it. A bullet is kept the
    moment it carries a fact (a number or distinctive word) the lede did NOT: only >=80%-already-in-the-lede
    bullets go. 'Why it matters:' is interpretive, not a fact restatement, so it is always kept."""
    if not s or "\n" not in s:
        return s
    lines = s.split("\n")
    lede = set()
    for ln in lines:                                        # the prose ABOVE the first bullet is the lede
        st = ln.strip()
        if st[:1] in "-•*" and len(st) > 1 and st[1] in " \t":
            break
        if st:
            lede |= _fact_tokens(st)
    if not lede:
        return s
    out = []
    for ln in lines:
        st = ln.strip()
        if st[:1] in "-•*" and len(st) > 1 and st[1] in " \t" and not re.match(r"^[-•*]\s*\*\*why", st, re.I):
            bt = _fact_tokens(_bullet_body(st))
            if bt and len(bt & lede) / len(bt) >= 0.8:      # ~all its facts already said above -> restatement
                continue
        out.append(ln)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


def _end_stop(t):
    """Give a wire line a terminal full stop when it ends mid-air. Many channel posts ship a headline with
    no end punctuation ('… settlers attack civilians in Nablus and Bethlehem'); a lone period reads far
    sharper than a fragment. Only when the last line ends on a word or closing quote — not ':' (a speaker
    label), not already '.?!…,;' — and is a real sentence (>=4 words), so 'Lavrov:' and one-word lines stay."""
    tr = (t or "").rstrip()
    if not tr:
        return t
    # ALREADY TERMINATED: a sentence-ender optionally wrapped by a closing quote/bracket ('…is open and
    # operating."'). Adding another '.' shipped the double-stop '…operating.".' — leave it alone.
    if re.search(r"[.!?…][\"'’”)\]]*$", tr):
        return tr
    last = tr.rsplit("\n", 1)[-1]
    if len(last.split()) < 4 or not re.search(r"[A-Za-z0-9”’\"']$", last):
        return t
    return tr + "."


def _cap_first(t):
    """Capitalize the first letter of a wire line. Channel posts are often pulled mid-thought and start
    lowercase ('an ethnic Tajik commander…'), which reads like a broken fragment; a leading capital makes it a
    proper sentence. Only touches a lowercase letter in the first two characters (past an opening quote), so
    'iPhone'/'eBay' mid-text and already-capitalized lines are untouched."""
    if not t:
        return t
    m = re.search(r"[A-Za-z]", t)
    if m and m.start() <= 1 and t[m.start()].islower():
        i = m.start()
        return t[:i] + t[i].upper() + t[i + 1:]
    return t


def _start_at_sentence(t):
    """A wire 'description' is frequently the TAIL of a sentence whose head became the headline — it opens
    mid-thought and lower-case ('against Iran earlier this year. TJP reports…'), which reads unfinished. If it
    starts lower-case AND a whole sentence follows, drop the dangling fragment and begin at that first COMPLETE
    sentence; if there's nothing substantial to fall back to, just capitalize the opening so it's not a runt."""
    if not t:
        return t
    if not re.match(r"['\"“‘(]?\s*[a-z]", t):     # doesn't open mid-sentence -> leave it
        return _cap_first(t)
    sm = re.search(r"[.!?][\"'”’)\]]*\s+(?=[A-Z0-9\"'“])", t)   # end of the dangling first sentence
    if sm:
        rest = t[sm.end():].strip()
        if len(rest) >= 40:                        # enough real sentence(s) left to stand alone
            return rest
    return _cap_first(t)


def _tg_clean(text):
    t = re.sub(r"<br\s*/?>", "\n", text)
    t = re.sub(r"</p>", "\n", t)
    t = re.sub(r"<[^>]+>", "", t)
    t = _htmlmod.unescape(t)
    t = _CREDIT_BRACKET.sub(" ", t)
    t = re.sub(r"[ \t]+", " ", t)
    # vxTwitter/fixvx repost: everything up to and including the x.com link is the reposter's comment —
    # the news is the embedded tweet that follows. Only when a provider marker confirms the embed, so a
    # normal post that merely links to X keeps its own text.
    if _TG_EMBED_MARK.search(t):
        m = _TG_XLINK.search(t)
        if m:
            t = t[m.end():]
    lines = [ln.strip() for ln in t.split("\n")]
    out = []
    for ln in lines:
        if not ln:
            continue
        ln = _TG_REACTIONS.sub(" ", ln).strip()                       # strip "💋 88 📩 36" reaction runs
        ln = _TG_EMBED_MARK.sub(" ", ln).strip(" /|·—–-")             # strip "vxTwitter / fixvx"
        if not ln:
            continue
        if re.fullmatch(r"@[A-Za-z0-9_]+", ln):
            continue
        if _TG_URL_ONLY.match(ln) or _TG_EMBED_AUTHOR.match(ln):
            continue
        if re.match(r"(?i)^(read (here|more)|subscribe|join our|follow us)\b.*", ln):
            continue
        # Telegram's own "this post can't be shown here" chrome
        ln = re.sub(r"(?i)\s*please open telegram to view this post\s*", " ", ln)
        ln = re.sub(r"(?i)\s*view in telegram\s*", " ", ln).strip()
        if not ln:
            continue
        if ln.startswith("💧") or _TG_AD.search(ln):
            continue
        if not re.search(r"[A-Za-z0-9]", ln):                         # left as stray emoji/punctuation -> noise
            continue
        out.append(ln)
    return _cap_first(_end_stop(_fix_stray_quotes(_strip_trunc(_fix_speaker_colon(_strip_lead_flag(re.sub(r"\n{2,}", "\n", "\n".join(out)).strip()))))))


def _tg_page(ch, before=None):
    """Fetch ONE page of a channel's public preview — the most recent posts, or (with ?before=<msg_id>) the
    page just OLDER than a given message, to walk back in time. Returns (posts, oldest_msg_id); UNCAPPED."""
    url = "https://t.me/s/" + ch + (("?before=" + str(before)) if before else "")
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
        h = urllib.request.urlopen(req, timeout=12).read().decode("utf-8", "replace")
    except Exception:
        return [], None
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
    out, oldest_id = [], None
    for chunk in re.split(r'(?=<div class="tgme_widget_message[ "])', h):
        dp = re.search(r'data-post="([^"]+)"', chunk)
        if not dp:
            continue
        post = dp.group(1)
        try:
            _mid = int(post.rsplit("/", 1)[-1])
            oldest_id = _mid if oldest_id is None else min(oldest_id, _mid)   # for ?before= paging
        except Exception:
            pass
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
    return out, oldest_id


def _tg_fetch(ch, hours=24, max_pages=6):
    """EVERY post a channel made in the last `hours`, not an arbitrary count. Telegram's public preview shows
    only ~16-20 posts per page (a few hours on a busy channel), so we PAGE BACK through it (?before=<id>)
    until the oldest post we've seen is past the window — this is what stops a busy channel from pushing an
    important story off the map before it's ever scraped. `max_pages` bounds a firehose channel so the scrape
    can't stall; deduped by message id. (Was a flat `out[-16:]` cap that silently dropped the oldest posts.)"""
    cutoff = time.time() - hours * 3600
    seen, all_posts, before = set(), [], None
    for _ in range(max_pages):
        page, oldest = _tg_page(ch, before)
        if not page:
            break
        for p in page:
            mid = p["link"].rsplit("/", 1)[-1]
            if mid not in seen:
                seen.add(mid)
                all_posts.append(p)
        # stop once this page has reached past the time window, or there's no older page to ask for
        if oldest is None or min((p["ts"] for p in page), default=0) < cutoff:
            break
        before = oldest
    return [p for p in all_posts if p["ts"] >= cutoff]


def _css_url(raw):
    """background-image:url(...) — Telegram quotes it plain ('…'), escaped (&#39;…&#39;) or not at all."""
    u = _htmlmod.unescape((raw or "").strip())
    return u.strip("'\" \t")


def _post_media(p):
    """The source post's OWN media — the whole album + any playable clip — as media-strip items. Stored on
    the event at BUILD TIME so it survives even after the post scrolls out of Telegram's ~20-post preview
    window (event_media re-matches the LIVE buffer, so an aged-out post's pictures would otherwise vanish)."""
    ch = p.get("title") or p.get("channel") or "Telegram"
    base = {"channel": ch, "link": p.get("link") or "", "time": p.get("time") or ""}
    items = []
    if p.get("video"):                                   # a playable clip Telegram will actually serve
        items.append(dict(base, video=p["video"], photo="", thumb=p.get("thumb") or "", dur=p.get("dur") or ""))
    seen = set()
    for ph in (p.get("photos") or ([p["photo"]] if p.get("photo") else [])):
        if ph and ph.startswith("http") and ph not in seen:
            seen.add(ph)
            items.append(dict(base, photo=ph, video="", thumb=ph))
    return items[:6]


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
_PROMO_LEAD   = re.compile(r"^\s*(?:breaking|just\s?in|update|developing|exclusive|alert|flash|watch|new|now|urgent|live|hot|latest)\s*[-:–—]+\s*", re.I)
# Aggregator BOILERPLATE that is not a story at all — Google News' channel blurb ("Comprehensive up-to-date
# news coverage, aggregated from sources all over the world by Google News") lands as an article's og:desc and
# was shown verbatim as the brief. Treat any text carrying it as EMPTY so the card summarizes the real body.
_JUNK_DESC = re.compile(
    r"comprehensive,?\s+up-?to-?date\s+news\s+coverage|aggregated\s+from\s+sources\s+all\s+over\s+the\s+world"
    r"|\bby\s+google\s+news\b|view\s+full\s+coverage\s+on\s+google\s+news|read\s+full\s+coverage", re.I)
_PROMO_TAIL   = re.compile(
    r"[\s\-–—|]*follow\s+(?:@[\w.]+|us)\b.*$"                                  # "Follow @Handle …" / "Follow us …"
    r"|[\s\-–—|]*(?:subscribe|join our (?:channel|telegram|whatsapp))\b.*$"    # channel plugs
    r"|[\s\-–—|]*for\s+more\s+(?:news|updates?|stories|info|coverage)\b.*$"    # "… for more news"
    r"|[\s\-–—|.]*read\s+(?:the\s+)?full\s+(?:article|story|report|coverage|piece|version)\b.*$"   # "Read Full Article at RT.com"
    r"|[\s\-–—|.]*(?:read\s+(?:the\s+)?(?:article|story|original|more)|continue\s+reading)"        # "Read more at cnn.com" / "Read the original at …"
    r"(?:\s*[:@]|\s+(?:at|on|via|here|below)\b|\s*[»›→]|\s*$).*$"                                   # …but only as a real CTA (source pointer or end), not prose "read the report"
    r"|[\s\-–—|]*(?:read(?:\s+more)?|watch|more|link|source|via|details?|full\s+story)\s*:\s*$",  # a label + colon left dangling after the URL was cut
    re.I | re.S)
_PROMO_HANDLE = re.compile(r"(?<![\w@])@[A-Za-z]\w{2,}")                            # stray "@InsiderPaper"
# A BARE OUTLET DOMAIN dropped into the prose is a source stamp, not news. SHIPPED: a Disclose.tv post read
# "…grade-point averages. Disclose.tv University of Michigan will stop…" — the channel name sat mid-sentence.
# Capitalised first letter (a brand) + a real TLD, standalone, no http (URLs are handled above); so an
# ordinary "booking.com" (lowercase) and "U.S." are left alone.
_BARE_SOURCE = re.compile(r"(?<!\S)[A-Z][A-Za-z0-9-]{1,20}\.(?:tv|com|net|org|io|news|co)(?![\w/])")
# A BYLINE/CREDIT is not news: "Authored by Guy Birchall via The Epoch Times", "Written by … for …",
# "Story by …". Strip it wherever it sits. A Capitalised NAME must follow "by", so ordinary prose ("a study
# authored by the team") is left alone.
_BYLINE = re.compile(
    r"(?m)(?:^|(?<=[.\s]))(?i:authored|written|reported|produced|compiled|edited|republished|reposted|story)\s+"
    r"(?i:by)\s+[A-Z][\w'’-]+(?:\s+[A-Z][\w'’-]+){0,3}"        # a Capitalised NAME (no '.', so 'Smith.' can't bleed
    r"(?:\s+(?i:via|for|at|from)\s+[A-Z][\w'’&-]+(?:\s+[\w'’&-]+){0,4})?\s*[.,;:·—\-]*")   # into the next sentence
# A TRAILING SOURCE ATTRIBUTION — "…, Axios reports.", "…, sources say.", "…, officials said.", "…, according
# to Reuters." — is wire furniture, not the news. Strip it from the END. A COMMA is required first, so ordinary
# prose ("the president said.") is left alone.
_TRAIL_ATTRIB = re.compile(
    r",\s*(?:(?:[A-Z][\w.&'’-]+(?:\s+[A-Z][\w.&'’-]+){0,3}\s+)?(?:reports?|reported|says?|said|confirms?|"
    r"confirmed|adds?|added|notes?|noted)|sources?\s+(?:say|said|tell|told|reported)|"
    r"(?:the\s+)?officials?\s+(?:say|said|added)|according\s+to\s+[^.,]{2,40})\s*\.?\s*$")   # NO re.I: the
# A BARE OUTLET NAME the wire left in the copy — "…kills 9 and injures 6 AP News.", "…DW has more.", "DW
# has more. Fire in…" — is furniture, not news. Curated list so a real proper noun is never chopped; matched
# both at the END (trailing byline) and as an INLINE self-promo ("DW has more."), the latter common in a
# MERGED dot where two outlets' teasers were concatenated.
_OUTLET_NAMES_RE = (r"AP\s*News|Associated\s+Press|Reuters|BBC(?:\s+News)?|CNN|DW|Deutsche\s+Welle|AFP|"
                    r"Bloomberg|Al\s*Jazeera|Fox\s+News|NBC\s+News|CBS\s+News|ABC\s+News|Sky\s+News|Anadolu|"
                    r"TASS|RT|Xinhua|CGTN|NPR|PBS|Politico|Axios|The\s+Guardian|New\s+York\s+Times|NYT|"
                    r"Washington\s+Post|WSJ|Wall\s+Street\s+Journal|Times\s+of\s+Israel|The\s+Hindu|Press\s+TV|"
                    r"Tasnim|Fars(?:\s+News)?|IRNA|Mehr(?:\s+News)?|Kyodo|Yonhap|SCMP|South\s+China\s+Morning\s+Post|"
                    r"Rappler|Premium\s+Times|The\s+Punch|Vanguard|Al\s+Arabiya")
_OUTLET_MORE = re.compile(r"\s*[.,;:–—-]*\s*(?:" + _OUTLET_NAMES_RE + r")\s+has\s+more\b[.\s]*", re.I)
_TRAIL_OUTLET = re.compile(r"\s*[\s,.;:–—-]+(?:" + _OUTLET_NAMES_RE + r")\s*[.\s]*$")
# WIRE DATELINE: "TEHRAN – ", "WASHINGTON — ", "BEIRUT, Lebanon — ", "NEW DELHI (Reuters) — ". A brief should
# just START, not open with a place-stamp. Only strips an ALL-CAPS leading place (>=3 caps) + optional
# ", Country" + optional "(Agency)" + a spaced dash — so a Title-cased sentence ("Trump — the president —")
# and "TEHRAN-based" (no space after the hyphen) are both left alone.
_LEAD_DATELINE = re.compile(
    r"^\s*[A-Z]{3,}[A-Z.'’&-]*(?:[ ][A-Z][A-Za-z.'’&-]+){0,3}"
    r"(?:\s*,\s*[A-Z][A-Za-z.'’-]+(?:[ ][A-Za-z.'’-]+){0,2})?"
    r"(?:\s+\([^)]{1,40}\))?\s*[–—-]\s+")
def _strip_promo(t):
    t = _htmlmod.unescape(t or "")
    t = _PROMO_LEAD.sub("", t)
    t = _LEAD_DATELINE.sub("", t)       # after the promo lead, so "BREAKING - TEHRAN — …" loses both stamps
    t = _PROMO_URL.sub(" ", t)          # bare links first, so a "READ: <url>" collapses to a strippable "READ:"
    prev = None
    while prev != t:                    # trailing promo stacks: "… READ: <url>  Follow @x for more news"
        prev = t
        t = _PROMO_TAIL.sub("", t)
    t = _PROMO_HANDLE.sub("", t)
    t = _BYLINE.sub(" ", t)             # "Authored by Guy Birchall via The Epoch Times ," -> gone
    t = _BARE_SOURCE.sub(" ", t)        # "…averages. Disclose.tv University of Michigan…" -> drop the stamp
    return re.sub(r"\s{2,}", " ", t).strip(" \t\r\n-–—|:,")


# A quotation mark immediately followed by a SPACE ('" Letter grades…') is not a real quote-open — it's a
# stray artifact. And a lone, unpaired double-quote reads as a typo. Fix both so copy is clean.
def _fix_stray_quotes(t):
    t = (t or "").strip()
    t = re.sub(r'^\s*["“”«»]\s+', "", t)                 # a leading quote + space is stray -> drop it
    t = re.sub(r'\s+["“”«»]\s*$', "", t)                 # a trailing dangling quote -> drop it
    if t.count('"') == 1:                                # a single, unpaired straight quote left anywhere -> drop
        t = t.replace('"', "")
    return t.strip()


def _to_last_sentence(t):
    """A wire description truncated MID-SENTENCE is cut back to the last COMPLETE sentence, so the card never
    shows a chopped stub with a tacked-on period ('…oil is down today a.', '…probably am.'). Feeds routinely
    hand us a clean first sentence followed by a chopped one ('…nuclear weapon. "I know that oil is down
    today a'); we keep the whole part and drop the chopped tail. Only when there is NO earlier whole sentence
    to fall back to do we keep the fragment as-is — the baked AI 'In brief' is what fixes those. Length-
    independent, unlike _clip_sentence, which only trims when the text runs past its char budget."""
    t = (t or "").strip()
    if not t:
        return t
    m = re.search(r"^[\s\S]*[.!?][\"'”’)\]]*(?=\s|$)", t)   # greedy: the LAST sentence-ender that a space/end follows
    if not m:
        return t                                  # no whole sentence anywhere -> single fragment, keep (last resort)
    whole = m.group(0).strip()
    tail = t[m.end():].strip()
    if tail and len(whole) >= 40:                 # real chopped tail after a substantial sentence -> drop the tail
        return whole
    return t


def _sharpen_desc(text, n=460):
    """The summary shown under an article, made professional: promo/handles gone, inline image/agency
    credits stripped ('… [Abu Adem Muhammed – Anadolu Agency]'), and a terminal full stop when it ends
    mid-air ('… researchers say' -> '… researchers say.'). RSS descriptions skipped these — only Telegram
    text was cleaned — so wire copy reached the card raw. End-stop BEFORE the clip so a complete short
    description keeps its period; a truly truncated one loses it and the UI adds an ellipsis instead."""
    if _JUNK_DESC.search(text or ""):
        return ""                           # aggregator boilerplate ("…by Google News") is not a story
    t = _CREDIT_BRACKET.sub(" ", _strip_promo(text or ""))
    # LEADING-JUNK LOOP — a promo word can hide BEHIND a leading emoji/flag/dash ("🇮🇷🇴🇲 ⚡ — NEW: …"): the
    # first _strip_promo saw the emoji at ^ and skipped "NEW:", then the flag strip peeled the emoji off and
    # left "NEW:" stranded. Peel flag+emoji+dash and re-strip the promo lead until nothing more comes off, so
    # NO emoji, flag, "IROM"-style regional-indicator letters, dash, or promo word can open the card. Ever.
    prev = None
    while prev != t:
        prev = t
        t = _PROMO_LEAD.sub("", _LEAD_DATELINE.sub("", _strip_lead_flag(t)))
    t = re.sub(r"\s{2,}", " ", t).strip()
    t = _fix_speaker_colon(t)               # "Former Israeli PM Bennett: Qatar…" -> "…Bennett says Qatar…"
    t = _strip_trunc(t)                     # "…last month. Hegseth [...]" -> "…last month." (no dangling stamp)
    t = _fix_stray_quotes(t)                # '" Letter grades…' -> 'Letter grades…' (stray quote gone)
    t = _OUTLET_MORE.sub(". ", t)                    # "DW has more. Fire in…" -> "…. Fire in…" (merged-teaser furniture)
    t = _TRAIL_ATTRIB.sub("", t).strip(" ,;:–—-")   # "…, Reuters reports." -> drop the trailing attribution
    t = _TRAIL_OUTLET.sub("", t).strip(" ,;:–—-")   # "…injures 6 AP News." -> drop a bare trailing outlet byline
    t = _start_at_sentence(t)                        # never open on a lower-case sentence fragment
    t = re.sub(r"\s*(\.\.\.+|…)\s*$", "", t).rstrip()   # drop a teaser's trailing "..." (ZeroHedge etc.)
    t = _to_last_sentence(t)                         # a mid-sentence truncation -> cut back to the last WHOLE sentence
    return _end_stop(_clip_sentence(t, n))           # then length-clip; end on a COMPLETE sentence, never mid-thought


# Prepositions / articles / coordinating conjunctions that essentially NEVER validly end a sentence (each
# demands an object, or is an article) — so a period after one is a stray dot and a trailing one is a
# truncation. Deliberately EXCLUDES words that can legitimately close a sentence ("game over", "moving on",
# "what it's about", "brought under"). Used by _tg_headline.
_DANGLE_WORDS = "in|on|at|of|to|the|a|an|and|or|nor|with|from|by|into|onto|upon|per|via|amid|toward|towards"


# A trailing '.' on one of these is an abbreviation, NOT a sentence end — so the first-sentence cut must not
# stop there ("wait until Mr. Trump…", "the U.S. said…", "Gen. Qaani…", "St. Petersburg…").
_ABBREV = {"mr", "mrs", "ms", "dr", "prof", "rev", "hon", "st", "mt", "jr", "sr", "vs", "no", "fig",
           "gen", "sen", "rep", "gov", "lt", "col", "sgt", "cpl", "capt", "cmdr", "maj", "adm", "brig",
           "pres", "sec", "supt", "det", "esq", "messrs", "inc", "ltd", "co", "corp", "dept", "est",
           "approx", "vol", "ave", "blvd", "rd"}


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
    # A period straight after a preposition/article/conjunction is NEVER a real sentence end — it's a
    # translation/OCR artifact ("...Kudrinskaya Street in. Moscow") that fooled the first-sentence cut into
    # a dangling "...Street in." (ends on punctuation, yet reads mid-thought). Drop the stray dot so the
    # sentence reads on ("...Street in Moscow, near the US embassy").
    line = re.sub(r"\b(" + _DANGLE_WORDS + r")\.(?=\s|$)", r"\1", line, flags=re.I)
    # Keep only the FIRST sentence: a Telegram post's later sentences are context the poster tacked on —
    # they must neither become the headline nor let a passing clause hijack a clip (this also extracts a
    # clip's SUBJECT). Cut RIGHT AFTER the first sentence's end punctuation, ALLOWING a closing quote/paren
    # after it (".”  ?"  .)) — otherwise a quoted title like ...Modernity.” spilled the headline into the
    # next sentence and got a mid-thought "…". NEVER a mid-word cut: if the line has no sentence end at
    # all, keep it whole and let the UI shrink the font. The whole line is searched (not just [:260]) so a
    # long-but-complete first sentence is kept in full rather than clipped.
    cut = -1
    for mm in re.finditer(r"(?<=[\w)\"'’”])[.!?]+[\"'’”)\]]*(?=\s|$)", line):
        if mm.end() < 60:
            continue
        # NOT a sentence end if the '.' belongs to an abbreviation or an initial. SHIPPED: "…wait until Mr.
        # Trump takes office" was cut at "Mr." — a truncated, meaningless headline.
        _wm = re.search(r"([A-Za-z]+)$", line[:mm.start()])
        _lw = _wm.group(1) if _wm else ""
        if _lw.lower() in _ABBREV or (len(_lw) == 1 and _lw.isupper()):
            continue
        cut = mm.end()                  # FIRST complete sentence, incl. its closing quote
        break
    if cut >= 60:
        line = line[:cut].strip()
    elif len(line) > 120:
        # No sentence end and the text is long -> the source TRUNCATED it (Telegram/RSS previews cut
        # mid-word: "…and Western offici"). Never ship a mid-word headline: fall back to the last clause
        # break, else the last whole word, within a headline length, and mark it continued with "…".
        seg = line[:180]
        mcl = max(seg.rfind(", "), seg.rfind("; "))
        if mcl >= 90:
            line = seg[:mcl].rstrip(" ,;:–—-.") + "."
        else:
            sp = seg.rfind(" ", 0, 172)
            line = (seg[:sp] if sp >= 90 else seg[:172]).rstrip(" ,;:–—-.") + "."
    # Never leave a headline ending on a dangling function word ("...held talks in.", "...struck by") —
    # a source that truncated mid-phrase. Drop the trailing word (and any comma/period it carried).
    line = re.sub(r"[\s,;:]+(?:" + _DANGLE_WORDS + r")\s*[.!?]*$", "", line, flags=re.I).strip()
    line = _TRAIL_ATTRIB.sub("", line).strip(" ,;:–—-")     # "…conflict with Armenia, Axios reports." -> drop the tag
    if line and line[0].islower():
        line = line[0].upper() + line[1:]
    return line.strip()


# UNVERIFIED / hedged — a rumour, never a fact. Always dropped from the map, attributed or not.
_TG_RUMOR = re.compile(
    r"\b(reportedly|allegedly|alleged|claim|claims|claimed|rumou?r|rumou?rs|unconfirmed|"
    r"purportedly|apparently|appears?\s+to|seems?\s+to|possible|possibly|speculat\w*)\b", re.I)
# FUTURE-tense / threat wording. Speculation on its OWN ("attack imminent", "missiles could strike Tel
# Aviv") — but the SAME words carry an on-record official statement ("Iran vows to respond", "Kremlin
# warns"). So this is only disqualifying when the post is NOT an attributed statement (see _tg_is_statement).
_TG_FUTURE = re.compile(
    r"\b(could|would|might|may|locked and loaded|aimed at|threaten\w*|warns?|warned|vow\w*|"
    r"plan(?:s|ning)?\s+to|set to|expected to|likely to|about to|preparing to|prepares? to|"
    r"imminent|brace[sd]?\s+for|fear\w*|if\s+(?:iran|russia|china|israel|the\s+us)|"
    r"would\s+(?:strike|attack)|to\s+strike)\b", re.I)
# CHANNEL META — a post ABOUT the wire itself, not an event: debunking recycled/old/fake footage, or
# explaining the channel's own coverage decisions. "Ansarullah is recycling 2-6 year old clips … republishing
# them as new … hence Rerum Novarum's lack of coverage … Note, there are no Abrams tanks in western Yemen."
# True, useful housekeeping — but it is not news and must never become a map dot. Two shapes: (1) editorial
# self-reference ("our/…'s lack of coverage", "we won't cover"), (2) a media-authenticity debunk (a recycle/
# old/fake/staged word next to clips/footage/video). Guarded so a real "Norway recycles bottles" story (no
# media noun nearby) and plain "video shows the strike" (no debunk word) pass through untouched.
_TG_META = re.compile(
    r"\black of coverage\b|\bour coverage\b"
    r"|\bwe (?:are not|will not|won'?t|do not|don'?t) (?:cover|posting|report\w*|be (?:cover|post|report)\w*)\b"
    r"|\b(?:recycl\w+|repost\w+|republish\w+|re-?upload\w+|old|years?[-\s]old|staged|faked?|doctored|"
    r"misattributed|mislabel\w+|unrelated)\b[^.\n]{0,30}\b(?:clips?|footage|videos?|images?|photos?|posts?)\b"
    r"|\b(?:clips?|footage|videos?|images?|photos?)\b[^.\n]{0,24}\b(?:are|is|were|was)\b[^.\n]{0,20}"
    r"\b(?:recycl\w+|old|years?[-\s]old|staged|faked?|doctored|misattributed|mislabel\w+|unrelated|"
    r"reposted|republished|re-?uploaded)\b", re.I)
# An ATTRIBUTED official statement: a named speaker label ("Lavrov:", "Iran's Foreign Ministry:", kept by
# _tg_headline as "Speaker: …") OR a saying/announcing verb. These are the statements the map was dropping.
_TG_STMT_LABEL = re.compile(r"^\s*[\"'“]?[A-Z][\w.'’&/-]*(?:[ ,][\w.'’&/-]+){0,6}\s*:\s+\S")
_TG_STMT_VERB = re.compile(
    r"\b(said|says|say|stated|states|state|announce[sd]?|announces|announcing|declare[sd]?|declares|"
    r"told|tells|vow[eds]*|vows|warn[eds]*|warns|confirm[eds]*|confirms|urge[sd]*|urges|"
    r"demand[eds]*|demands|reject[eds]*|rejects|accus[eds]*|accuses|insist[eds]*|insists|"
    r"pledge[sd]*|pledges|threaten[eds]*|threatens|slam[meds]*|slams|calls?\s+(?:on|for))\b", re.I)


def _tg_is_statement(text):
    """True when the post is an on-record statement — a speaker label ('Lavrov: …') or a saying verb
    ('Iran vows', 'Kremlin warns'). Such a post is NEWS even when it is future-tense or a threat."""
    h = (text or "").strip()
    return bool(_TG_STMT_LABEL.match(h) or _TG_STMT_VERB.search(h))


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
    r"enjoy your (day|evening|weekend|night)|good to be back|"
    # the admin talking about THEMSELVES / off-topic — a personal aside, not a report
    r"genuinely\s+(just\s+)?wanted|i\s+just\s+wanted\s+to|just\s+wanted\s+to\s+(see|share|show|say|post)|"
    r"off[-\s]?topic|unrelated\s+(to|but|,)|personal\s+(note|aside|opinion)|"
    r"not\s+(really\s+)?news\s*(but|,)|this\s+is\s?n'?t\s+news|"
    r"anyone\s+else\s+(notice|think|feel|see)|can\s+we\s+(just\s+)?(talk about|appreciate)|"
    r"sorry\s+for\s+the\s+(spam|off[-\s]?topic)|on\s+a\s+personal\s+note|"
    # casual first-person OPINION — the admin editorialising, not reporting ("I really thought…", "fair enough tbh")
    r"\btbh\b|\bngl\b|\bimo\b|\bimho\b|\bsmh\b|\blmao\b|\blol\b|"
    r"fair\s+enough|not\s+gonna\s+lie|hot\s+take|unpopular\s+opinion|"
    r"i\s+really\s+(thought|think|reckon|figured|hoped|expected)|if\s+you\s+ask\s+me|"
    r"just\s+my\s+(opinion|two\s+cents|thoughts)|call\s+me\s+(crazy|cynical)|personally[,\s]+i\b|"
    r"in\s+my\s+(honest\s+)?opinion|let'?s\s+be\s+(honest|real))\b", re.I)


def _tg_is_chatter(text):
    """The admin talking TO the audience rather than reporting an event — a greeting, a sign-off, a
    thank-you, or channel self-promotion. Filtered from the wire; the firehose keeps everything else
    (including speculative breaking posts, which `_tg_reliable` drops only for MAP dots)."""
    h = (text or "").strip()
    if not h:
        return True
    return bool(_TG_CHATTER.search(h) or _TG_HOUSEKEEPING.search(h))


def _tg_reliable(headline):
    """OSINT/wire posts are noisy — drop channel self-talk, unverified RUMOURS, and bare SPECULATION.
    But an ATTRIBUTED official statement is kept even when it's future-tense or a threat ('Iran vows to
    respond', 'Lavrov: we would retaliate') — those on-record statements are exactly the news the map was
    silently dropping. Only UNattributed future-tense/threat wording ('attack imminent') is speculation."""
    h = (headline or "").strip()
    if len(h) < 22:
        return False
    if h.endswith("?"):
        return False
    if _tg_is_chatter(h):
        return False                                       # self-promo, greetings, AND first-person OPINION/
                                                           # editorialising ("In my opinion, this is a poor
                                                           # decision…") — the admin's take is never a map dot.
                                                           # Attributed statements ("Lavrov: …") aren't chatter.
    if _TG_META.search(h):
        return False                                       # channel meta: debunking old/recycled footage or
                                                           # explaining its own coverage — housekeeping, not news
    if _TG_RUMOR.search(h):
        return False                                       # unverified rumour -> out, attributed or not
    if _TG_FUTURE.search(h) and not _tg_is_statement(h):
        return False                                       # speculation only when NOT an on-record statement
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


# A PROMOTIONAL / SCAM / CHANNEL-PLUG post is not news — a crypto-"signals" ad, a "JOIN AND READ HERE"
# WhatsApp invite, a get-rich pump. These strong signals rarely appear in a real news post, so a match drops
# the WHOLE post (not just cleans it). Kept tight: a legit "US SEC approves a Bitcoin ETF" story has none of
# an invite link, a "trading signals" plug, or pump language.
_SPAM_RE = re.compile(
    r"chat\.whatsapp\.com|t\.me/joinchat|t\.me/\+|wa\.me/"                              # group-invite links
    r"|\b(?:btc|crypto|bitcoin|forex|fx|trading|market|stock)\s+signals?\b"             # "BTC market signals"
    r"|\bjoin\s+(?:this|our|the|and\s+read)\b[^.\n]{0,60}?"
    r"(?:platform|group|channel|community|signals?|crypto|bitcoin|forex|trading|vip|telegram|whatsapp)"
    r"|before\s+(?:everyone|anyone)\s+else\s+catches"                                    # "…before everyone else catches on"
    r"|\b(?:100x|1000x|10x)\b|to\s+the\s+moon|get\s+rich|financial\s+freedom"
    r"|guaranteed\s+(?:profit|returns?|income)|risk-?free\s+(?:profit|returns?)"
    r"|\bdm\s+(?:me|us)\b[^.\n]{0,40}(?:join|invest|signals?|profit|earn)"
    r"|(?:sign\s?up|register)\b[^.\n]{0,40}(?:free|bonus|signals?|earn|profit)"
    r"|promo\s*code|referral\s+(?:code|link)|use\s+code\s+[A-Z0-9]{4,}",
    re.I)


def _is_spam(text):
    """A promotional / scam / channel-plug post (crypto-signals ad, WhatsApp-invite pump) — drop it entirely."""
    return bool(_SPAM_RE.search(text or ""))


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
            if _is_spam(p.get("text") or ""):
                continue                                # a crypto-signals ad / WhatsApp-invite pump — not news
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
                "desc": _clip(p.get("text") or "", 400),
                "sourcecountry": "",
                "geo_text": (p.get("text") or ""),   # the full post — read as the story's BODY (desc) when the headline names no scene ("…in southern Lebanon")
                "_src": p.get("title") or p.get("channel") or "Telegram",
                "_media": _post_media(p),            # the post's OWN album/clip, kept so it can't age out of the strip
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


# ---- WHO'S INVOLVED: a fair, plain-language glossary of the groups and bodies that recur in world news.
# Written to INFORM a reader who meets the name cold — who they are, where, and whose side — NOT to label:
# no "terrorist", no cheerleading, just the facts a wire assumes you already know. A story's own text is
# scanned for these so the reader can tap a name they don't recognise. Each entry: (canonical, aliases, def).
# `aliases` are lowercase spellings a wire might use; matched whole-word so "AP" or "map" never false-fire.
_GLOSSARY = [
    ("Ansar Allah (the Houthis)", ("ansarallah", "ansar allah", "ansarullah", "houthi", "houthis"),
     "A Zaydi Shia movement that controls much of northern Yemen, including the capital Sanaa. Aligned with "
     "Iran, it fought a Saudi-led coalition to a stalemate and has attacked Red Sea shipping since the Gaza war."),
    ("Hamas", ("hamas",),
     "The Palestinian Islamist movement that has governed the Gaza Strip since 2007 — both a political party "
     "and an armed wing. It is backed politically by Iran and Qatar and is the main rival to Fatah."),
    ("Hezbollah", ("hezbollah", "hizbollah", "hizbullah", "hesbollah"),
     "A Lebanese Shia political party and armed movement, funded and armed by Iran. It holds seats in "
     "Lebanon's parliament and is one of the most powerful forces in the country."),
    ("Palestinian Islamic Jihad", ("palestinian islamic jihad", "islamic jihad", "pij"),
     "A smaller Iran-backed Palestinian armed group based mainly in Gaza — separate from, and generally more "
     "hardline than, Hamas."),
    ("Islamic Revolutionary Guard Corps", ("irgc", "revolutionary guard", "revolutionary guards", "quds force"),
     "A branch of Iran's armed forces answering directly to the Supreme Leader, with wide military, economic "
     "and intelligence reach. Its Quds Force runs operations and arms allied groups abroad."),
    ("Taliban", ("taliban",),
     "The Islamist movement that has ruled Afghanistan since 2021, enforcing a strict reading of Islamic law "
     "and sharply curtailing women's rights."),
    ("Islamic State", ("islamic state", "isis", "isil", "daesh"),
     "A transnational Sunni jihadist group that seized parts of Iraq and Syria in 2014 and declared a "
     "'caliphate'. Territorially defeated by 2019, it still runs insurgent cells and regional affiliates."),
    ("al-Qaeda", ("al-qaeda", "al qaeda", "alqaeda"),
     "The transnational Sunni jihadist network founded by Osama bin Laden and behind the 2001 attacks on the "
     "United States. It now operates mostly through regional affiliates."),
    ("Wagner Group", ("wagner group", "wagner"),
     "A Russian private military company used by the Kremlin for deniable operations in Ukraine, Syria and "
     "several African states."),
    ("Hayat Tahrir al-Sham", ("hayat tahrir al-sham", "tahrir al-sham", "hts"),
     "The dominant Islamist faction in Syria's northwest, rooted in a former al-Qaeda affiliate it later "
     "broke from. It led the 2024 offensive that toppled the Assad government."),
    ("Kurdistan Workers' Party (PKK)", ("pkk",),
     "A Kurdish militant group that waged a decades-long insurgency against Turkey seeking Kurdish autonomy "
     "and rights, and in 2025 began a move to disarm."),
    ("Boko Haram", ("boko haram",),
     "A Nigerian jihadist group that has waged an insurgency across the Lake Chad region since 2009."),
    ("al-Shabaab", ("al-shabaab", "al shabaab", "al-shabab", "shabaab"),
     "An al-Qaeda-aligned jihadist group fighting the internationally-backed government of Somalia."),
    ("Fatah", ("fatah",),
     "The secular Palestinian party that dominates the Palestinian Authority and the West Bank; the main "
     "rival to Hamas."),
    ("Palestinian Authority", ("palestinian authority",),
     "The internationally-recognised body that governs parts of the occupied West Bank, led by the Fatah party."),
    ("Muslim Brotherhood", ("muslim brotherhood",),
     "A transnational Sunni Islamist movement founded in Egypt in 1928; influential across the Arab world and "
     "outlawed by several governments."),
    ("Kataib Hezbollah", ("kataib hezbollah", "kata'ib hezbollah"),
     "An Iran-backed Iraqi Shia armed group, part of Iraq's state-linked Popular Mobilization Forces."),
    ("Axis of Resistance", ("axis of resistance",),
     "The name Iran and its allies use for their network of aligned states and armed groups — Hezbollah, "
     "Hamas, the Houthis and Iraqi militias — opposed to Israel and the United States."),
    ("Israel Defense Forces (IDF)", ("israel defense forces", "israeli defense forces", "idf"),
     "Israel's national military."),
    ("NATO", ("nato",),
     "A military alliance of 32 North American and European countries whose members pledge to defend one "
     "another from attack."),
    ("IAEA", ("iaea", "international atomic energy agency"),
     "The United Nations' nuclear watchdog, which inspects nuclear sites to verify they stay peaceful."),
    ("Hezbollah al-Nujaba / Iraqi militias", ("popular mobilization forces", "pmf", "hashd al-shaabi"),
     "State-linked, largely Shia armed groups in Iraq, many of them close to Iran, folded into the official "
     "security forces after the fight against Islamic State."),
    ("Rapid Support Forces (RSF)", ("rapid support forces",),   # not bare "RSF" — also Reporters Without Borders
     "A Sudanese paramilitary force fighting the national army in a devastating civil war since 2023, grown "
     "out of the earlier Janjaweed militias."),
    ("Syrian Democratic Forces (SDF)", ("syrian democratic forces",),   # not bare "SDF" — also Japan's Self-Defense Forces
     "A Kurdish-led, US-backed armed coalition that controls much of northeastern Syria."),
    ("M23", ("m23",),
     "A mainly Tutsi armed group in the eastern Democratic Republic of Congo, widely reported to be backed "
     "by neighbouring Rwanda."),
    ("Polisario Front", ("polisario",),
     "The movement seeking independence for Western Sahara from Morocco, which controls most of the territory."),
    ("European Union", ("european union",),
     "A political and economic union of 27 European countries with a single market and, for most members, a "
     "shared currency, the euro."),
    ("United Nations", ("united nations", "un security council", "un general assembly"),
     "The world body of 193 member states, founded in 1945 to keep the peace, set international law and "
     "coordinate humanitarian aid."),
    ("African Union", ("african union",),
     "A continental body of 55 African states that coordinates political, economic and security policy."),
    ("Arab League", ("arab league",),
     "A bloc of 22 Arab states that coordinates political and economic ties across the Middle East and North Africa."),
    ("BRICS", ("brics",),
     "A bloc of major emerging economies — Brazil, Russia, India, China, South Africa and newer members — that "
     "positions itself as a counterweight to the Western-led order."),
    ("OPEC", ("opec",),
     "The Organization of the Petroleum Exporting Countries, a group of major oil producers that coordinates "
     "output to steer world crude prices."),
    ("International Monetary Fund", ("international monetary fund", "imf"),
     "A UN-linked lender of 190 member states that provides emergency loans to countries in financial trouble, "
     "usually with conditions attached."),
    ("World Health Organization", ("world health organization", "world health organisation"),
     "The United Nations' health agency, which coordinates the response to disease outbreaks and sets global "
     "health standards."),
    ("International Criminal Court", ("international criminal court",),
     "A permanent court in The Hague that prosecutes individuals for genocide, war crimes and crimes against "
     "humanity."),
    ("Palestine Liberation Organization (PLO)",
     ("palestine liberation organization", "palestine liberation organisation", "plo"),
     "The body recognised internationally as the representative of the Palestinian people, dominated by Fatah; "
     "it signed the 1990s Oslo Accords with Israel."),
    ("Popular Front for the Liberation of Palestine (PFLP)",
     ("popular front for the liberation of palestine", "pflp"),
     "A secular, Marxist Palestinian faction — smaller than Hamas or Fatah — with both a political and an armed wing."),
    ("UNRWA", ("unrwa",),
     "The UN agency that provides schooling, health care and aid to registered Palestinian refugees across the "
     "Middle East."),
    ("UN Refugee Agency (UNHCR)", ("unhcr",),
     "The United Nations agency that protects and assists refugees and people displaced by war and persecution."),
    ("Pakistani Taliban (TTP)", ("tehrik-i-taliban", "tehrik-e-taliban", "pakistani taliban", "ttp"),
     "A militant movement waging an insurgency against the Pakistani state — separate from, though allied with, "
     "the Afghan Taliban."),
    ("Haqqani network", ("haqqani",),
     "A powerful faction within the Afghan Taliban, long based along the Afghanistan–Pakistan border."),
    ("Lashkar-e-Taiba", ("lashkar-e-taiba", "lashkar-e-taleba"),
     "A Pakistan-based armed group focused on the disputed Kashmir region, blamed for the 2008 Mumbai attacks."),
    ("Jaish-e-Mohammed", ("jaish-e-mohammed", "jaish-e-muhammad"),
     "A Pakistan-based armed group active in the Kashmir conflict with India."),
    ("JNIM", ("jnim", "jama'at nusrat al-islam"),
     "An al-Qaeda-linked coalition of jihadist groups waging an insurgency across Mali and the wider Sahel."),
    ("Islamic State – West Africa (ISWAP)", ("iswap",),
     "The Islamic State's West African branch, active around the Lake Chad basin after splitting from Boko Haram."),
    ("Islamic State – Khorasan (ISIS-K)", ("isis-k", "islamic state khorasan", "iskp"),
     "The Islamic State's affiliate in Afghanistan and the surrounding region, and an enemy of the Afghan Taliban."),
    ("ELN", ("eln",),
     "Colombia's National Liberation Army, a leftist guerrilla group founded in the 1960s and still in on-off "
     "peace talks with the government."),
    ("FARC", ("farc",),
     "The Revolutionary Armed Forces of Colombia, a former leftist guerrilla army that signed a 2016 peace deal; "
     "some dissident factions fight on."),
    ("Tren de Aragua", ("tren de aragua",),
     "A criminal gang that grew out of a Venezuelan prison and spread across Latin America alongside migration."),
    ("Sinaloa Cartel", ("sinaloa cartel",),
     "One of Mexico's largest and oldest drug-trafficking organisations."),
    ("Jalisco New Generation Cartel (CJNG)", ("cjng", "jalisco new generation"),
     "A fast-growing, especially violent Mexican drug cartel and a main rival of the Sinaloa Cartel."),
    ("Peshmerga", ("peshmerga",),
     "The armed forces of the Kurdistan Region of northern Iraq."),
    ("Amal Movement", ("amal movement",),
     "A Lebanese Shia political party and former militia, long allied with Hezbollah."),
    ("G7", ("g7", "group of seven"),
     "A bloc of seven major advanced economies — the US, UK, Canada, France, Germany, Italy and Japan — that "
     "coordinates economic and foreign policy."),
    ("G20", ("g20", "group of twenty"),
     "A forum of 19 major economies plus the EU and the African Union, together covering most of the world's output."),
    ("Gulf Cooperation Council (GCC)", ("gulf cooperation council", "gcc"),
     "A bloc of six Gulf Arab monarchies — Saudi Arabia, the UAE, Qatar, Kuwait, Bahrain and Oman."),
    ("Shanghai Cooperation Organisation (SCO)", ("shanghai cooperation", "sco"),
     "A Eurasian political and security bloc led by China and Russia; its members include India, Pakistan and Iran."),
    ("ECOWAS", ("ecowas",),
     "The Economic Community of West African States — a 15-nation regional bloc that also mediates political crises."),
    ("ASEAN", ("asean",),
     "The Association of Southeast Asian Nations, a 10-member bloc for regional economic and political cooperation."),
    ("Interpol", ("interpol",),
     "The international police organisation that helps member countries share information and track suspects across borders."),
    ("OPCW", ("opcw",),
     "The global watchdog that verifies the destruction of chemical weapons and investigates their use."),
    ("Organisation of Islamic Cooperation (OIC)",
     ("organisation of islamic cooperation", "organization of islamic cooperation", "oic"),
     "A bloc of 57 mostly Muslim-majority states that coordinates shared political and religious positions."),
    ("Iran nuclear deal (JCPOA)", ("jcpoa", "iran nuclear deal"),
     "The 2015 agreement under which Iran limited its nuclear programme in exchange for sanctions relief; the "
     "US withdrew in 2018."),
    ("Abraham Accords", ("abraham accords",),
     "US-brokered 2020 agreements that normalised relations between Israel and several Arab states, among them "
     "the UAE and Bahrain."),
    ("Schengen Area", ("schengen",),
     "A zone of European countries that have abolished passport checks at their shared internal borders."),
    ("Yemeni National Resistance Forces", ("yemeni national resistance forces", "national resistance forces"),
     "A Yemeni armed force on the Red Sea coast led by Tareq Saleh, nephew of a former president — UAE-backed "
     "and fighting the Houthis."),
    ("pro-Hadi forces", ("pro-hadi", "hadi government"),
     "Yemeni units loyal to the government of former president Abd-Rabbu Mansour Hadi, the side recognised "
     "internationally against the Houthis (Hadi handed power to a leadership council in 2022)."),
    ("Southern Transitional Council (STC)", ("southern transitional council",),
     "A UAE-backed movement seeking self-rule for south Yemen, at times allied with and at times opposed to "
     "the internationally recognised government."),
    ("Tigray People's Liberation Front (TPLF)", ("tigray people's liberation front", "tplf"),
     "The party governing Ethiopia's northern Tigray region, which fought a 2020–2022 war with the federal government."),
    ("Al-Aqsa Martyrs' Brigades", ("al-aqsa martyrs", "aqsa martyrs"),
     "An armed offshoot linked to the Palestinian Fatah movement, active mainly in the occupied West Bank."),
    ("Kurdistan Regional Government (KRG)", ("kurdistan regional government",),
     "The self-governing authority of the Kurdish region of northern Iraq."),
]
_GLOSSARY_RE = [(canon, defn, aliases,
                 re.compile(r"(?<![\w'-])(?:" + "|".join(re.escape(a) for a in aliases) + r")(?![\w'-])", re.I))
                for (canon, aliases, defn) in _GLOSSARY]


def _glossary_terms(text, limit=8):
    """Which glossary groups/bodies does this story name? Returns [{term, def, aliases}] in the glossary's own
    order (most-central first), deduped, capped. `aliases` lets the client bold the exact surface form it finds
    inline. Whole-word match so 'AP', 'map', 'Hamasa' never trip a definition."""
    low = _fold(text or "")
    out = []
    for canon, defn, aliases, rx in _GLOSSARY_RE:
        if rx.search(low):
            out.append({"term": canon, "def": defn, "aliases": list(aliases)})
            if len(out) >= limit:
                break
    return out


# Beyond the curated list, DETECT capitalized proper-name phrases that end in an organisation word ("Yemeni
# National Resistance Forces", "Southern Transitional Council") and let the AI define them on the fly — this is
# what scales the definer past a hand-written list toward "every group a story names". Bounded to real org/group
# shapes, and the AI is told to answer NONE whenever it isn't sure, so nothing is bolded on a guess.
_ORG_SUFFIX = (r"(?:Forces|Front|Army|Movement|Militia|Militias|Brigade|Brigades|Battalion|Coalition|Alliance|"
               r"Council|Cartel|Federation|Guard|Guards|Corps|Command|Faction|Junta|League|Authority|"
               r"Directorate|Organisation|Organization|Congress|Network|Party|Bloc|Union|Group|Assembly|"
               r"Committee|Society|Association|Caliphate|Insurgency|Syndicate|Collective|Vanguard|Regiment)")
# The org word may sit in the MIDDLE of the name, with a "of/for <Place/Cause>" tail: "Muslim Association OF
# BRITAIN", "Movement FOR the Liberation of Palestine", "Congress OF South African Trade Unions". Capture that
# tail so the WHOLE name is defined, not a truncated "Muslim Association". SHIPPED BUG: MAB detected as just
# "Muslim Association". The tail is optional, so plain "Cockroach Janta Party" still matches.
_ORG_PHRASE_RE = re.compile(
    r"\b((?:[A-Z][\w'’.\-]+\s+){1,5}" + _ORG_SUFFIX +
    r"(?:\s+(?:of|for)(?:\s+the)?\s+[A-Z][\w'’.\-]+(?:\s+[A-Z][\w'’.\-]+){0,3}){0,2})\b")
_DEFINE_VER = "t2"   # t2: definitions now come from Wikipedia first (free, no LLM) — invalidate old empty/LLM ones


# ACRONYMS a general reader already knows — never worth a definition. Everything else in caps (DFAT, NTUC,
# IRGC, DPRK, UNIFIL) is exactly what the user asked to define.
_COMMON_ACRONYMS = {
    "US", "USA", "UK", "UN", "EU", "NATO", "AP", "BBC", "CNN", "CEO", "CFO", "COO", "CTO", "GDP", "GPS", "AI",
    "IT", "TV", "PM", "DNA", "FBI", "CIA", "NSA", "NASA", "WHO", "IMF", "WTO", "OPEC", "EV", "USD", "EUR", "GBP",
    "ID", "OK", "UAE", "LLC", "INC", "LTD", "FAQ", "VIP", "PDF", "URL", "ATM", "PIN", "SUV", "RSVP", "AKA", "ETA",
    "DIY", "CEO", "MP", "MPS", "VP", "AG", "DA", "PR", "HR", "QA", "RD", "IPO", "GMT", "UTC", "AM", "PM",
    "COVID", "AIDS", "HIV", "ISS", "UFO", "SOS", "FYI", "ASAP", "NGO", "NGOS", "GPS", "APEC", "ASEAN", "BRICS",
    "G7", "G20", "OK", "TBD", "CCTV",
}


def _detect_org_phrases(text, covered, limit=4):
    """Names a general reader won't know — CANDIDATES for an on-the-fly AI definition: a capitalised
    'Proper Name + org word' phrase (Cockroach Janta Party, Dnepr Volunteer Corps), OR a bare ACRONYM (DFAT,
    NTUC, IRGC) that isn't a common one. The user's clue: multiple capitals / an abbreviation almost always
    marks something to define. Deduped; anything the curated glossary already covers is dropped."""
    seen, out = set(), []
    for m in _ORG_PHRASE_RE.finditer(text or ""):
        phrase = re.sub(r"\s+", " ", m.group(1)).strip(" ,.;:")
        phrase = re.sub(r"^(?:The|A|An)\s+", "", phrase)     # a sentence-initial "The" got swept in — drop it
        low = phrase.lower()
        if len(phrase.split()) < 2 or low in seen:
            continue
        if any(low == c or low in c or c in low for c in covered):
            continue
        seen.add(low)
        out.append(phrase)
        if len(out) >= limit:
            break
    # bare ACRONYMS (all-caps, 3-6 letters) the reader won't know — DFAT, NTUC, UNIFIL, DPRK, SCMP
    for m in re.finditer(r"\b([A-Z][A-Z&]{2,5})\b", text or ""):
        ac = m.group(1)
        low = ac.lower()
        if low in seen or ac in _COMMON_ACRONYMS or low in covered:
            continue
        if any(low == c or (len(low) >= 4 and low in c) for c in covered):
            continue
        seen.add(low)
        out.append(ac)
        if len(out) >= limit + 3:
            break
    return out


# A PERMANENT, growing database of learned term definitions, kept in DATA_DIR so it SURVIVES a cache clear —
# a name the AI defines once is ours forever and never costs a second call. Curated _GLOSSARY always wins;
# this is the accumulated long tail "in our pocket". Loaded once; appended under a lock (define_term runs in a
# thread pool). Over time the most-seen entries can be promoted by hand into the curated list above.
_LEARNED_PATH = os.path.join(DATA_DIR, "learned_terms.json")
_LEARNED_LOCK = threading.Lock()
try:
    _LEARNED = json.load(open(_LEARNED_PATH, encoding="utf-8")) if os.path.exists(_LEARNED_PATH) else {}
    if not isinstance(_LEARNED, dict):
        _LEARNED = {}
except Exception:
    _LEARNED = {}


def _learn_term(name, definition):
    key = re.sub(r"\s+", " ", (name or "").strip()).lower()
    if not key or not definition:
        return
    with _LEARNED_LOCK:
        if _LEARNED.get(key) == definition:
            return
        _LEARNED[key] = definition
        try:
            tmp = _LEARNED_PATH + ".tmp"
            json.dump(_LEARNED, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
            os.replace(tmp, _LEARNED_PATH)
        except Exception:
            pass


class Api:
    """Exposed to the page as window.pywebview.api.*"""

    def has_ai(self):
        return bool(load_gemini_key())

    def ping(self):
        return {"ok": True, "ai": bool(load_gemini_key())}

    def article_terms(self, title, desc=""):
        """The CURATED groups/bodies this story names, each with a fair, even-handed one-line explainer — the
        inline definer's fast path (no network). Returns [{term, def, aliases}]; empty when it names none we
        curate. The AI long tail is article_ai_terms, fetched separately so this stays instant."""
        try:
            return _glossary_terms((title or "") + " . " + (desc or ""))
        except Exception:
            return []

    def define_term(self, name):
        """A neutral one-line definition of a named group/body, for the inline definer's AI long tail. Cached 30
        days. Returns '' when there's no summarizer, or when the model isn't sure what the name refers to (it is
        told to answer NONE) — so an unknown name is simply left un-bolded rather than given an invented meaning."""
        name = re.sub(r"\s+", " ", (name or "").strip())
        if len(name) < 4:
            return ""
        learned = _LEARNED.get(name.lower())
        if learned:
            return learned                       # permanent DB — already in our pocket, no call
        cache = os.path.join(CACHE_DIR, "term_" + hashlib.sha1((_DEFINE_VER + "\n" + name.lower()).encode("utf-8")).hexdigest()[:16] + ".json")
        if _fresh(cache, 30 * 86400):            # short-term cache also remembers a NONE, so we don't re-ask
            try:
                return json.load(open(cache, encoding="utf-8")).get("d", "")
            except Exception:
                pass
        # FREE baseline FIRST — Wikipedia (no LLM budget), so a named org is defined even when the summariser is
        # rate-capped, and definitions no longer compete with summaries for the daily token cap. Facts only.
        out = _wiki_define(name)
        # The LLM only ENRICHES — a neutral one-liner when Wikipedia has no page AND a summariser is available.
        if not out and _llm_available():
            system = ("You are a neutral reference work, like an encyclopedia. Define who or what a named group or "
                      "body is in ONE plain, factual sentence — what kind of organisation it is, where it operates, "
                      "and its role or main affiliation. Be strictly even-handed: no opinion, no praise, and never a "
                      "loaded label such as 'terrorist', 'regime' or 'extremist'. If you are not confident what the "
                      "name refers to, answer with exactly: NONE")
            prompt = ("Define this name in one neutral sentence for a reader who doesn't know it: \"" + name + "\".\n"
                      "If you are not sure what it refers to, reply with exactly NONE.")
            out = re.sub(r"\s+", " ", (_llm_complete(system, prompt, max_tokens=90, temperature=0.2) or "").strip())
            if len(out) < 15 or out.upper().rstrip(".").startswith("NONE"):
                out = ""
        try:
            json.dump({"d": out}, open(cache, "w", encoding="utf-8"))
        except Exception:
            pass
        if out:
            _learn_term(name, out)               # keep it forever in the permanent database
        return out

    def article_ai_terms(self, title, desc=""):
        """The DETECTED (non-curated) org/group names in a story, each AI-defined neutrally — the long tail that
        scales the definer past the hand-written list. Additive to article_terms; slower (a cached AI call per
        new name), so the client fetches it after the instant curated pass."""
        try:
            text = (title or "") + " . " + (desc or "")     # definitions now come from Wikipedia first, so this
            covered = set()                                  # runs even when the LLM budget is spent
            for t in _glossary_terms(text):
                for a in t.get("aliases", []):
                    covered.add(a.lower())
            phrases = _detect_org_phrases(text, covered)
            if not phrases:
                return []
            out = []
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                for name, d in zip(phrases, ex.map(self.define_term, phrases)):
                    if d:
                        out.append({"term": name, "def": d, "aliases": [name.lower()]})
            return out
        except Exception:
            return []

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
            # BIAS the search toward the leader actually SPEAKING (official/news footage), not sensational
            # commentary channels — "<name> speech OR interview OR remarks OR address OR press conference". Fetch
            # a few extra so the recency filter (last week, then month) still has candidates. Cached 2 days.
            sq = query.strip() + " speech OR interview OR remarks OR address OR press conference OR statement"
            fetch = min(14, max(n + 6, 10))
            opts = {"quiet": True, "skip_download": True, "extract_flat": False,
                    "noplaylist": True, "no_warnings": True, "socket_timeout": 15,
                    "ignoreerrors": True, "playlist_items": "1-%d" % fetch}
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info("https://www.youtube.com/results?search_query=" + urllib.parse.quote(sq) + "&sp=CAI%253D", download=False)
            out = []
            for e in ((info or {}).get("entries") or []):
                if e and e.get("id"):
                    out.append({"id": e.get("id"), "title": e.get("title") or "",
                                "channel": e.get("channel") or e.get("uploader") or "",
                                "dur": e.get("duration"),
                                "ts": e.get("timestamp") or e.get("release_timestamp")})
            out.sort(key=lambda c: (c.get("ts") or 0), reverse=True)
            _now = time.time()
            # RECENT footage only: prefer the last COUPLE DAYS, widen to a WEEK at most. A clip we can't date is
            # excluded (we can't prove it's recent), and we NEVER fall back to older videos — a person with no
            # recent clips shows NONE rather than stale footage (the shipped 28-day-old "bombshell" clip).
            out = ([c for c in out if c.get("ts") and (_now - c["ts"]) < 3 * 86400]
                   or [c for c in out if c.get("ts") and (_now - c["ts"]) < 7 * 86400])   # last couple days, a week at most — never a 28-day-old clip
            res = {"clips": out[:n]}
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
                cap = _strip_lead_flag(_tg_headline(p.get("text") or "") or (p.get("text") or "").strip())
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

    def event_for_post(self, text, link=""):
        """The map dot a live-wire post belongs to — even when that dot was first reported HOURS ago by a
        different outlet in different words. Uses the SAME gate that files a clip onto a story (_clip_matches:
        a shared DISTINCTIVE name + word + the SAME country), so it's sharp both ways: it catches the same
        event under different wording, and it will NOT grab a look-alike that merely shares 'Gaza' or 'Israel'.
        Returns {title, lat, lng, country} of the matched dot, or {}."""
        try:
            text = (text or "").strip()
            if len(text) < 12:
                return {}
            cache = os.path.join(CACHE_DIR, "world_24h.json")
            if not os.path.exists(cache):
                return {}
            events = (json.load(open(cache, encoding="utf-8")) or {}).get("events", []) or []

            def _card(e):
                return {"title": e.get("title", ""), "lat": e.get("lat"), "lng": e.get("lng"), "country": e.get("country", "")}
            # 1) EXACT — the dot is built from THIS post, or already cites it as a source
            if link:
                for e in events:
                    if e.get("url") == link or any((s or {}).get("url") == link for s in (e.get("sources") or [])):
                        return _card(e)
            # 2) SAME EVENT, different words — the clip matcher (distinctive name + word + same country)
            for e in events:
                t = e.get("title") or ""
                if t and _clip_matches(t, text):
                    return _card(e)
            return {}
        except Exception:
            return {}

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
                # A LONG video (>12 min) is a daily ROUNDUP / livestream, not footage of THIS single event —
                # its caption's top story rarely matches the dot, so it never belongs on one card. (A real clip
                # of an incident runs seconds to a few minutes.)
                if item.get("video") and _dur_minutes(item.get("dur")) > 12:
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
                cached = json.load(open(cache, encoding="utf-8"))
                # SELF-HEAL: keep a GOOD cached url (re-thumbed to a display size), but do NOT trust a cached
                # url that is now rejected (a flag/map saved before _good_img learned to reject it) OR an EMPTY
                # result — those fall through and RE-QUERY, so a place that returned nothing before (a city-
                # state whose only image was a flag) now resolves via the curated landmark. No cache wipe.
                if cached.get("url") and _good_img(cached["url"]):
                    cached["url"] = _wiki_thumb(cached["url"], 1280)
                    return cached
            except Exception:
                pass

        # most specific first, then widen: "Odesa (port, unspecified)" -> Odesa -> Odesa Oblast -> Ukraine
        qs = []
        # strip the parenthetical BEFORE splitting on commas — "Odesa (port, unspecified)" contains a
        # comma INSIDE the brackets, so splitting first left the query as the literal "Odesa (port".
        clean = re.sub(r"\s*\([^)]*\)", "", place).strip()
        head = clean.split(",")[0].strip()
        # A city-state/microstate has no city article of its own -> query a curated LANDMARK that DOES have a
        # real photo (Singapore -> the Downtown Core skyline), so the hero is never a rejected flag/black frame.
        _ov = _PLACE_PHOTO_QUERY.get(clean.lower()) or _PLACE_PHOTO_QUERY.get(head.lower())
        if _ov:
            qs.append(_ov)
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
            if src and not _good_img(src):
                src = ""                       # a country's Wikipedia lead image is usually its flag/locator map — skip it
            if src:
                out = {"url": _wiki_thumb(src, 1280), "title": j.get("title") or q}
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
                # A photo of the country's MAIN CITY (Kyiv, Jeddah, Tehran) — NEVER the country itself,
                # whose Wikipedia lead image is a flag or a locator map. If that city has no photo, give
                # up (return None) rather than fall back to a flag/map.
                if not country:
                    return None
                city = _LARGEST_CITY.get(country) or _LARGEST_CITY.get(short)
                if not city:
                    return None
                p = self.place_photo(city.title(), "")   # country="" so it can't widen back to the flag
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
                    it["quote"] = q if _quote_important(q) else ""   # drop trivial small talk ("I know X well")
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

    def port_profile(self, name, country=""):
        """A concise, factual profile of a seaport for the click popup: type, when it opened, operator, annual
        throughput, approximate daily vessel calls, strategic significance, recent developments and the waters
        it sits on. Grounded on Google Search via Gemini when a key is set (real figures + recent news), else
        the open LLM for the stable facts. Cached 7 days, so throughput/vessel figures refresh about weekly.
        Always returns a dict with name/country + a live-tracker URL, even with no LLM — the popup still opens."""
        name = re.sub(r"\s+", " ", (name or "").strip())
        country = re.sub(r"\s+", " ", (country or "").strip())
        base = {"name": name, "country": country}
        if len(name) < 2:
            return base
        cache = os.path.join(CACHE_DIR, "port_" + hashlib.sha1(
            (_PORT_VER + "\n" + name.lower() + "\n" + country.lower()).encode("utf-8")).hexdigest()[:16] + ".json")
        if _fresh(cache, 7 * 86400):
            try:
                return json.load(open(cache, encoding="utf-8"))
            except Exception:
                pass
        # LAYER 1 — WIKIPEDIA: a real photo + cited basic facts (founded/type/operator/throughput/berths) from
        # the port's own article. This is the public info the user asked for, and it covers nearly every port.
        out = dict(base)
        try:
            wiki = _port_wiki(name, country)
            for k, v in wiki.items():
                if v:
                    out[k] = v
        except Exception:
            pass
        # LAYER 2 — CURATED BASELINE: the "cool" ranking/superlative + waters (+ a default type/role line),
        # filling any gap Wikipedia left. Guarantees every port has content even if its article had no infobox.
        for k, v in _port_baseline(name, country).items():
            if v and not out.get(k):
                out[k] = v
        # LAYER 3 — AI ENRICHMENT (only when a live key exists): ships/day, recent developments, a richer
        # significance — filling remaining gaps, never overriding the cited Wikipedia facts or curated ranking.
        where = name + (", " + country if country else "")
        data = None
        try:
            key = load_gemini_key()
        except Exception:
            key = ""
        if key:
            data = _port_profile_gemini(where, key)      # grounded -> real figures + recent developments
        if data is None and _llm_available():
            data = _port_profile_llm(where)              # open model -> stable facts, figures only if known
        if data:
            for k, v in data.items():
                if v and not out.get(k):
                    out[k] = v
        try:
            json.dump(out, open(cache, "w", encoding="utf-8"))
        except Exception:
            pass
        return out

    def world_events(self, hours=24):
        """Real, geolocated world news for the map — GDELT DOC 2.0 (free, no key). Cached 15 min.
        Returns {"events":[{title,cat,lat,lng,place,country,hrs,source,domain,url,image}], "generated":ts}."""
        try:
            h = int(hours)
        except Exception:
            h = 24
        if h not in (6, 12, 24, 48):
            h = 24
        base = _feed_base()
        if base:                                        # THIN CLIENT: one GET of the server-built feed
            hosted = self._hosted_world_events(base, h)
            if hosted is not None:
                return hosted                           # (falls through to the local build if unreachable)
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
        # FRESH RESWEEP: a feed built by an OLDER _DATA_VER is from before this update — discard it and build
        # fresh (new rules, new AI) rather than serving stale dots the fix was meant to correct.
        if cached and cached.get("dv") != _DATA_VER:
            cached = None
        if cached:
            # restore the clip->owner map too, or the feed would serve with an EMPTY owner map and the
            # same clip would reappear under several dots until the next rebuild.
            if isinstance(cached.get("clip_owner"), dict):
                global _CLIP_OWNER
                _CLIP_OWNER = cached["clip_owner"]
            if not _fresh(cache, 120):        # rescan news every ~2 min so fresh wire news reaches the map fast (AI results cache, so a new post costs ~one call; the rest is re-served from cache)
                _spawn_world_refresh(self, h)
                cached = dict(cached)
                cached["stale"] = True
            _spawn_summary_prewarm(self, h, cached)   # warm summaries for the served feed too (e.g. cold open)
            return cached
        return self._build_world_events(h)

    def _hosted_world_events(self, base, h):
        """THIN-CLIENT PATH — fetch the pre-built feed from the hosted backend. This is one small, CDN-cached
        GET that every user shares, so the origin does the GDELT/geolocate/Telegram work ONCE for everybody
        (the model that scales to millions and powers the mobile apps). A 60s client cache keeps the 10-min
        poll from refetching; a stale hosted copy is served on a hiccup; None means 'server unreachable —
        use the local build' so the desktop app still works offline."""
        cache = os.path.join(CACHE_DIR, "hosted_%dh.json" % h)
        cached = None
        if os.path.exists(cache):
            try:
                cached = json.load(open(cache, encoding="utf-8"))
            except Exception:
                cached = None
        if cached and _fresh(cache, 60):
            return cached                               # very fresh — serve instantly, no network at all
        try:
            data = json.loads(_http_get(base.rstrip("/") + "/world_%dh.json" % h, 10))
            if not isinstance(data, dict) or "events" not in data:
                raise ValueError("bad feed")
            if isinstance(data.get("clip_owner"), dict):
                global _CLIP_OWNER
                _CLIP_OWNER = data["clip_owner"]
            try:
                json.dump(data, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
            return data
        except Exception:
            return cached                               # last hosted copy, or None to fall back to local

    def _build_world_events(self, h, live=False):
        """The live build — GDELT + feeds + Telegram, geolocated and deduped, written to the cache. Blocks;
        run synchronously only on a cold start, and in a background thread by stale-while-revalidate.

        live=False (the SYNCHRONOUS cold-start path) keeps every AI step OFF the critical path: locations
        come from rules + the cached summary WHERE, and the LLM dedup net is skipped. That is the difference
        between the map filling in ~1 min and sitting empty for 6-12 min while hundreds of live LLM calls
        stack up. live=True (the BACKGROUND refresh) then does the full AI geo + dedup, so the very next
        served feed is the upgraded one — the cache warms invisibly while the user already has dots."""
        cache = os.path.join(CACHE_DIR, "world_%dh.json" % h)
        span = "%dh" % h
        # Fetch GDELT, the RSS feeds, and the OSINT Telegram channels ALL AT ONCE, capped by a SINGLE
        # deadline. The old code awaited GDELT then feeds with separate timeouts (worst case 12s+16s
        # stacked) and only then fetched Telegram sequentially — so a slow source stretched cold start
        # badly. Now whatever is ready by the deadline is used; stragglers finish in the background and
        # are simply dropped from this build (the next 15-min rebuild picks them up).
        arts = []
        _ex = concurrent.futures.ThreadPoolExecutor(max_workers=3)
        try:
            f_gdelt = _ex.submit(_gdelt_doc, COMBINED_QUERY, span, 250)
            f_feeds = _ex.submit(_collect_feeds)
            f_tg = _ex.submit(_tg_arts, h)
            # GDELT + the RSS feeds are best-effort under a tight deadline (a slow one just misses this build).
            # The OSINT Telegram wire is the app's CORE breaking-news source, and its 24h page-back across the
            # channels legitimately runs ~15-20s — so it gets its own, longer deadline (measured from the same
            # start), never dropped as a "straggler". SHIPPED BUG: at a flat 16s the whole wire timed out and
            # EVERY Telegram dot (a Ukrainian refinery strike, etc.) silently vanished from the map.
            _t0 = time.time()
            for fut, dl in ((f_gdelt, 16), (f_feeds, 16), (f_tg, 30)):
                try:
                    arts += fut.result(timeout=max(0.1, dl - (time.time() - _t0))) or []
                except Exception:
                    pass
        finally:
            _ex.shutdown(wait=False)          # don't block on a straggler; its socket timeouts bound it
        events, seen_urls, seen_titles, added_sigs = [], set(), set(), []
        if not _WEAK_MATCH:
            _init_weak_match()               # the inline dedup subtracts it, like _ai_dedup does
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
            if _is_fluff(title, url) or _is_muted(a.get("domain"), a.get("_src"), url):
                continue
            if _is_spam(title + " " + (a.get("desc") or "")):     # crypto-signals ad / invite-link pump — not news
                continue
            norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:55]
            if norm in seen_titles:
                continue
            hrs = a.get("hrs")
            if hrs is None:
                hrs = _seendate_hours(a.get("seendate") or "")
            if hrs > h:                    # dots strictly expire after the 24h window
                continue
            # Geolocate on the HEADLINE, with the full post as the summary. A Telegram roundup names several
            # strikes; using the whole post as the "title" let a later, higher-profile place (a Bashkortostan
            # refinery) outrank the headline's own subject (a strike in the Sea of Azov). The headline is what
            # the card shows, so it's what the dot must match; body clarifications ("…in southern Lebanon")
            # are still read from the desc when the headline itself names no scene.
            # Cold start (live=False): rules + the CACHED summary WHERE only. A live per-art geolocation call
            # would stack to minutes on a cold cache (351 calls seen) and the map would sit empty; the AI
            # pinpoint lands on the next (background) build via the WHERE the summary prewarm fills in.
            loc = _locate(title, a.get("sourcecountry") or "", a.get("geo_text") or a.get("desc") or "", url,
                          allow_ai=live)
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
            # IMPORTANCE GATE. The world map is for news that is COUNTRY-, REGION- or WORLD-changing — a war
            # move, a leader's consequential statement, a mass-casualty event — not every true-but-minor local
            # story ("illegal sand extraction erodes a beach"), and not a broad analysis with no place to pin.
            # The AI rates each story's SCOPE + WHERE in the summary pass. Both drops are overridden by
            # _hard_news (casualties or a top official on the record). Hidden stories still surface in the
            # STARRED-country feed (country_news). No scope yet (a brand-new story) -> shown, then gated on the
            # next build once summarised — the same one-build lag as the AI pinpoint.
            if not _map_worthy(title, a.get("desc") or "", loc):
                continue
            # Dedup on DISTINCTIVE words only. Comparing raw sigwords merged genuinely different events: every
            # Russia+security story shares {drone, strike, oil, refinery...}, so a fresh strike on the Tver oil
            # depot was thrown away as a "duplicate" of an unrelated Omsk strike. Subtract BOTH _GENERIC_WORDS
            # AND _WEAK_MATCH (demonyms + ubiquitous names: russian/ukrainian/israeli…) — exactly what _ai_dedup
            # uses. SHIPPED BUG: six different NOELREPORTS war posts (a Novorossiysk strike, a HIMARS shot, a
            # Zelensky statement, the daily losses tally) all pinned to 'Ukraine' + same category collapsed into
            # ONE dot because {russian, ukrainian} counted as 2 shared "distinctive" words.
            _sig = _sigwords(title)
            _key = _sig - _GENERIC_WORDS - _WEAK_MATCH
            # Similarity set = title + the lede. The title alone missed the SAME story told with different
            # words (a long "Jerusalem Post… Israel surprised by Iran's recovery" vs a short "Israel shocked
            # by Iran's rebound") — the bodies share the real content. Only feeds _same_story, which still
            # demands a 0.72 overlap AND same place/country + a 12h window, so distinct events don't collapse.
            _toks = _norm_tokens(title + " " + (a.get("desc") or "")[:280])
            # The NAMES a story mentions (Jerusalem Post, Mossad, Tel Aviv). Names don't collide by coincidence
            # the way ordinary words do, so a strong shared-name overlap is the most reliable "same story told
            # with different words" signal — it catches re-headlined copies (astonished vs surprised, military
            # rebuild vs defense recovery) that share almost no common vocabulary but clearly the same subject.
            _props = {w.rstrip("'") for w in _proper_words(title + " " + (a.get("desc") or "")[:280])}
            img = a.get("socialimage") or ""
            _is_tg = bool(a.get("_tg"))
            _dup_ei = None
            _spec = place != country and place != _co_short(country)          # this dot names a specific city/site
            for _co2, _cat2, _pl2, _key2, _toks2, _props2, _hrs2, _ei2 in added_sigs:
                _inter = len(_key & _key2)
                # TWO FAR-APART SPECIFIC PLACES ARE DIFFERENT EVENTS. SHIPPED BUG: a Ukrainian strike on the
                # Komsomolsk-on-Amur refinery (far-east Khabarovsk Krai) was folded into a strike on the Orsk
                # refinery (Orenburg, ~6,000 km away). But this must be DISTANCE, not a string compare — else
                # "Orsk Refinery, Russia" and "Orsk, Russia" (the same site, ~10 km apart, a facility vs its
                # host city) read as different and the SAME refinery-halt story stays two dots. Only a real gap
                # (>60 km) marks a different scene.
                _diff_place = False
                if _spec and _pl2 != _co2 and _pl2 != _co_short(_co2) and _pl2 != place:
                    _e2 = events[_ei2]
                    _diff_place = _km(lat, lng, _e2.get("lat", lat), _e2.get("lng", lng)) > 60
                # SIMILARITY METER: the same story from another source/channel — a copy may carry an extra
                # prefix ("President Trump via Truth Social:"), be re-headlined, or land in a different
                # category. Near-identical wording, or same-country/place with high overlap, = a duplicate.
                if (_inter >= 4                                        # near-identical wording
                        or (_co2 == country and _inter >= 3 and not _diff_place)   # same country, strongly alike
                        or (_pl2 == place and _cat2 == cat and _inter >= 2)   # same place, same kind of event
                        or ((_co2 == country or _pl2 == place) and abs(hrs - _hrs2) <= 12
                            and _same_story(_toks, _toks2) and not _diff_place)
                        or ((_co2 == country or _pl2 == place) and abs(hrs - _hrs2) <= 12
                            and not _diff_place and len(_props & _props2) >= 3)):   # 3+ shared NAMES + same scene/day = the same story, however reworded
                    _dup_ei = _ei2
                    break
            if _dup_ei is not None:
                # A DUPLICATE — don't drop it, CREDIT its outlet on the dot it duplicates, so every source
                # that ran the story (antiwar.com, a wire, a channel) is cited instead of the copies vanishing.
                _cite_source(events[_dup_ei], {
                    "source": (a.get("_src") or _domain_name(a.get("domain") or "")),
                    "domain": ("t.me" if _is_tg else (a.get("domain") or "")), "url": url,
                    "hrs": round(hrs, 1), "title": title, "image": img if _good_img(img) else "",
                })
                seen_urls.add(url)
                continue
            # RULE 4: never drop a story. These caps DID drop real news — the per-country cap of 7
            # silently binned the 8th Russia story of the day, and a full-scale war produces far more
            # than seven. Dedup (above) is what protects the map from repetition; a cap is a blunt
            # instrument that throws away events we correctly fetched, classified and placed.
            # They are a runaway guard only — so HARD NEWS (casualties, a top official on the record, a
            # strike on strategic infrastructure) BYPASSES them entirely and can never be capped out.
            # SHIPPED BUG: a full day of war news filled the security cap (70/70) and the map silently
            # dropped a Ukrainian strike on the Komsomolsk-on-Amur refinery — exactly the news it's for.
            _hard = _hard_news(title, a.get("desc") or "")
            _cap = 5 if cat == "sports" else (150 if cat == "security" else 90)
            if not _hard and (per_cat.get(cat, 0) >= _cap or per_country.get(country, 0) >= 55):
                continue
            events.append({
                "title": title, "cat": cat, "sid": _share_id(url, title),
                "lat": round(lat, 4), "lng": round(lng, 4),
                "place": place, "country": country, "geo_confidence": _geo_confidence(loc),
                "hrs": round(hrs, 1),
                "source": (a.get("_src") or _domain_name(a.get("domain") or "")),
                "domain": ("t.me" if _is_tg else (a.get("domain") or "")),
                "url": url,
                "image": img if _good_img(img) else "",   # filter Telegram link-preview logos too (TASS/RT cards)
                "sum": _sharpen_desc(a.get("desc") or ""),
                "involved": (_involved_countries(title, country) or [country]),
                "channel": (a.get("_src") or "") if _is_tg else "",
                "srcmedia": (a.get("_media") or []) if _is_tg else [],   # source post's own media, always available
                "tg": _is_tg,
                "_hard": _hard,     # transient: keeps hard news ahead of the final cap; stripped before return
            })
            seen_urls.add(url)
            seen_titles.add(norm)
            added_sigs.append((country, cat, place, _key, _toks, _props, hrs, len(events) - 1))
            per_cat[cat] = per_cat.get(cat, 0) + 1
            per_country[country] = per_country.get(country, 0) + 1
        # HARD NEWS first (so a strike/casualty/official statement is NEVER cut by the final cap), then
        # picture-bearing, then most-recent — the order the merge keeps and the final cap trims from the end.
        events.sort(key=lambda e: (0 if e.get("_hard") else 1, 0 if e["image"] else 1, e["hrs"]))
        try:
            events = _merge_same_event(events)     # one dot per EVENT — cite every source that covered it
        except Exception:
            for _e in events:                      # a merge bug must NEVER blank the feed — degrade to un-merged
                _e.setdefault("sources", [_src_of(_e)])
        events = _collapse_colocated(events)   # then one dot per place — merge a co-located barrage
        try:
            # LIVE build: ask the LLM about new candidate pairs (capped) and LEARN the verdicts. COLD start:
            # apply the verdicts already learned — NO live calls, so the reworded duplicates the last build
            # merged stay merged on the very first paint instead of reappearing until the background pass runs.
            events = _ai_dedup(events, cache_only=not live)
        except Exception:
            pass
        events = events[:400]                  # raised from 260 — a busy war day has more than 260 real stories
        for _e in events:
            _e.pop("_hard", None)              # transient sort key — not part of the served feed
            # WHO IS REPORTING — a factual ownership note (computed AFTER merges/promotions, so it matches the
            # outlet actually shown). Lets the card flag "TASS · Russian state media" so a reader weighs slant.
            _e["srcnote"] = _source_note(_e.get("source"), _e.get("domain"))
        try:
            _assign_clips(events, _tg_all_posts())   # each clip belongs to ONE dot, feed-wide
        except Exception:
            pass
        _spread(events)   # fan out dots that share a location
        res = {"events": events, "generated": int(time.time()), "clip_owner": _CLIP_OWNER, "dv": _DATA_VER}
        if events:  # never cache an empty/failed result — let it retry next time
            try:
                json.dump(res, open(cache, "w", encoding="utf-8"))
            except Exception:
                pass
        _spawn_summary_prewarm(self, h, res)   # new news dropped -> summarize it all NOW, before anyone clicks
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
            cache = os.path.join(CACHE_DIR, "starred_%s_%s_%dh.json" % (_DATA_VER, _slug(country)[:24], h))
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
                if _is_fluff(title, url) or _is_muted(a.get("domain"), a.get("_src"), url):
                    continue
                norm = re.sub(r"[^a-z0-9]+", " ", title.lower()).strip()[:55]
                if norm in seen_titles:
                    continue
                hrs = _seendate_hours(a.get("seendate") or "")
                if hrs > h:
                    continue
                loc = _locate(title, a.get("sourcecountry") or country,
                              a.get("geo_text") or a.get("desc") or "", url, allow_ai=False)
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
                    "title": title, "cat": cat, "sid": _share_id(url, title),
                    "lat": round(lat, 4), "lng": round(lng, 4),
                    "place": place, "country": ev_country, "geo_confidence": _geo_confidence(loc),
                    "hrs": round(hrs, 1),
                    "source": _domain_name(a.get("domain") or ""),
                    "domain": a.get("domain") or "", "url": url,
                    "srcnote": _source_note(_domain_name(a.get("domain") or ""), a.get("domain") or ""),
                    "image": img if _good_img(img) else "",
                    "sum": _sharpen_desc(a.get("desc") or ""),
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
        return {"version": APP_VERSION, "frozen": bool(getattr(sys, "frozen", False)),
                "repo": _update_repo(), "feed": _feed_base()}    # feed base doubles as the share-page host

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
                    c = json.load(open(cache, encoding="utf-8"))
                    # RE-VALIDATE against the CURRENT version. The cached 'available' was computed against the
                    # version that was running when it was written — so after an update it's stale and would
                    # show a phantom "Update to vX" pill for the version you JUST installed. Recompute it.
                    c["available"] = bool(c.get("url")) and _is_newer(c.get("version") or "", APP_VERSION)
                    return c
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
                    "setlocal enableextensions\r\n"
                    "ping 127.0.0.1 -n 2 >nul\r\n"
                    "set /a n=0\r\n"
                    ":wait\r\n"
                    # wait for us to quit — but bounded (~60s), then force it, so a lingering process can
                    # never wedge the update. findstr, not find (find hung the whole updater once).
                    'tasklist /nh /fi "imagename eq Meridian.exe" 2>nul | findstr /i "Meridian.exe" >nul\r\n'
                    "if errorlevel 1 goto swap\r\n"
                    "set /a n+=1\r\n"
                    "if %n% geq 30 goto swap\r\n"
                    "ping 127.0.0.1 -n 2 >nul\r\n"
                    "goto wait\r\n"
                    ":swap\r\n"
                    "taskkill /f /im Meridian.exe >nul 2>&1\r\n"   # make sure nothing holds the file open
                    "ping 127.0.0.1 -n 2 >nul\r\n"
                    'copy /y "' + newexe + '" "' + target + '" >nul\r\n'
                    'del "' + newexe + '" >nul 2>&1\r\n'
                    'start "" "' + target + '"\r\n'
                    'del "%~f0" >nul 2>&1\r\n'
                )
            # CREATE_NO_WINDOW, NOT DETACHED_PROCESS. A detached (console-less) cmd left `tasklist | find`'s
            # `find` blocked on stdin forever — the update hung with a stuck window and never swapped the exe.
            # CREATE_NO_WINDOW keeps a real (hidden) console so the pipe works; a child still outlives us.
            FLAGS = 0x08000000 | 0x00000200               # CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
            subprocess.Popen(["cmd", "/c", bat], creationflags=FLAGS, close_fds=True)

            def _bye():
                time.sleep(0.7)                           # let the response reach the UI first
                try:
                    webview.windows[0].destroy()
                except Exception:
                    pass
                time.sleep(0.6)
                os._exit(0)                                # GUARANTEE the process is gone so the swapper can
                #   replace the exe — a lingering process is exactly what left the old swapper waiting.
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
            try:
                cached = json.load(open(cache, encoding="utf-8"))
            except Exception:
                cached = None
            if cached and cached.get("ver") != _LEADER_VER:
                cached = None                    # a resolution-logic fix shipped -> discard the stale card
            if cached:
                # A DEGRADED result — Wikidata was rate-limited, so names came from the Factbook and the
                # photos are missing — must NOT stick for 20h. Retry it in ~20 min so it self-heals to the
                # real Wikidata data (correct spelling + photos) the moment the rate limit passes. A good
                # result is trusted the full 20h. This is what stops a transient 429 from freezing the
                # wrong leader card in place (e.g. King Salman with no photo) until tomorrow.
                ttl = 1200 if cached.get("degraded") else 20 * 3600
                if time.time() - cached.get("generated", 0) < ttl:
                    return cached
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
                    if c["dead"] and c["name"]:
                        _mark_dead(c["name"])                            # remember, so the Factbook can't revive them
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
            # If the best holder Wikidata offers has an ENDED term (a former leader, because the country's P6
            # hasn't been rewired to the successor yet), follow 'replaced by' to the CURRENT holder. This is
            # what makes the daily update catch a change the day it happens — e.g. UK: Starmer (ended) -> Burnham.
            for _role, _pick in (("P35", hos), ("P6", hog)):
                if _pick and _pick.get("ended") and offices.get(_role):
                    _cur = _succession_current(_pick["qid"], offices[_role])
                    if _cur and _cur.get("name") and _cur["qid"] != _pick["qid"]:
                        _fresh_leader = {"qid": _cur["qid"], "name": _cur["name"], "img": _cur.get("img", ""),
                                         "ended": False, "dead": False, "x": "", "tg": "", "truth": ""}
                        if _role == "P35":
                            hos = _fresh_leader
                        else:
                            hog = _fresh_leader
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
                # The SHORT name resolves best (Wikidata search takes "Muhammad bin Salman", not the full
                # "…bin Abd al-Aziz Al Saud") — try it FIRST so we don't burn a doomed call (and risk a rate
                # limit) before the one that works.
                _hog_short = " ".join(fb_hog_name.split()[:3])
                p = _wd_search_person(_hog_short) or (
                    _wd_search_person(fb_hog_name) if fb_hog_name != _hog_short else None)
                if p and p["qid"] != _hos_qid:
                    hog = {"qid": p["qid"], "name": p["name"], "img": p["img"], "x": "", "tg": "", "truth": ""}
                    hog_forced_title = fb_hog_title
            # ALWAYS return CLEAN names. If Wikidata was rate-limited/incomplete and left a role unresolved,
            # use the cleanly-parsed Factbook name (the client fetches its photo from Wikipedia). This is what
            # stops a slow fetch from EVER showing a blank or a garbled fallback again.
            if fb_cos_name and (not hos or not hos.get("name")) and not _is_dead(fb_cos_name):
                hos = {"qid": (hos or {}).get("qid"), "name": fb_cos_name, "img": (hos or {}).get("img", "")}
            if fb_hog_name and (not hog or not hog.get("name")) and not _is_dead(fb_hog_name) and not (
                    hos and _same_person(fb_hog_name, None, hos.get("name", ""), hos.get("qid"))):
                hog = {"qid": (hog or {}).get("qid"), "name": fb_hog_name, "img": (hog or {}).get("img", "")}
                hog_forced_title = hog_forced_title or fb_hog_title
            # AUTHORITATIVE OVERRIDE — Wikipedia's daily-current "heads of state and government" list is the
            # source of truth for WHO holds each post today. Where it names a DIFFERENT current holder than we
            # resolved (Wikidata P6 and the Factbook both lag reshuffles — e.g. a new PM), trust the list: take
            # its name + clean title and pull the photo from Wikipedia. When it names the SAME person we keep our
            # richer Wikidata card (portrait + party). This is what keeps every country correct, checked daily.
            hos_forced_title = ""
            try:
                _hd = _heads_for(country)
                if _hd:
                    _hh = _hd.get("hos") or {}
                    if _hh.get("name") and not _is_dead(_hh["name"]):
                        if not hos or not _same_person(hos.get("name"), hos.get("qid"), _hh["name"], None):
                            hos = {"qid": None, "name": _hh["name"],
                                   "img": _wiki_person_img(_hh.get("article") or _hh["name"])}
                            hos_forced_title = _hh.get("title") or ""
                        elif not hos.get("img"):       # same person, our portrait 429'd -> fill from Wikipedia
                            hos["img"] = _wiki_person_img(_hh.get("article") or _hh["name"])
                    _hg = _hd.get("hog") or {}
                    if _hg.get("name") and not _is_dead(_hg["name"]):
                        if not hog or not _same_person(hog.get("name"), hog.get("qid"), _hg["name"], None):
                            hog = {"qid": None, "name": _hg["name"],
                                   "img": _wiki_person_img(_hg.get("article") or _hg["name"])}
                            hog_forced_title = _hg.get("title") or ""
                        elif not hog.get("img"):
                            hog["img"] = _wiki_person_img(_hg.get("article") or _hg["name"])
            except Exception:
                pass
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
                _emit(hos, "P35", "Head of state", "President", fb_name=fb_cos_name, fb_title=fb_cos_title,
                      forced_title=hos_forced_title)
            if hog and hog.get("name") and not (
                    hos and _same_person(hog.get("name"), hog.get("qid"), hos.get("name"), hos.get("qid"))):
                _emit(hog, "P6", "Head of government", "Prime Minister",
                      fb_name=fb_hog_name, fb_title=fb_hog_title, forced_title=hog_forced_title)
            # CABINET — deputy leader + top diplomat + defence chief, resolved from Wikidata's office
            # 'officeholder' (P1308). ACCURATE where Wikidata maintains it (strong for the US and well-labelled
            # offices) and SILENTLY ABSENT otherwise: a guess is never shown, so the card is never wrong. Wrapped
            # so a cabinet lookup (or a rate limit) can never break the head-of-state/government cards above.
            _cabinet_wiped = False   # US precise-title block came back empty (429) -> re-fetch, don't cache all day
            try:
                # Office names use assorted forms of the country name ("the United States", "United States"),
                # not always the map's ("United States of America"). Build the common variants so the search hits.
                _cn = (country or "").strip()
                _vars = [_cn]
                _short = re.sub(r"(?i)\s+of\s+America$", "", _cn)
                if _short != _cn:
                    _vars.append(_short)
                for _b in list(_vars):
                    if _b and not _b.lower().startswith("the "):
                        _vars.append("the " + _b)

                def _clean_cab_title(t):
                    for v in sorted(_vars, key=len, reverse=True):
                        if v:
                            t = re.sub(r"(?i)\b" + re.escape(v) + r"\b", "", t)
                    t = re.sub(r"(?i)\s+of\s+the\s*$|\s+of\s*$|^\s*of\s+the\s+|^\s*of\s+", " ", t)
                    return re.sub(r"\s+", " ", t).strip(" -")
                _seen_q = {(hos or {}).get("qid"), (hog or {}).get("qid")}
                _seen_q.discard(None)
                _seen_nm = {(_l.get("name") or "").lower() for _l in out}
                # US: precise-title cabinet (Vice President, Secretary of State, Secretary of Defense) from
                # curated Wikidata offices whose current-officeholder is reliably maintained — VP isn't in the
                # minister lists below, and these give the exact US titles.
                _expect_cab = _CABINET_QIDS.get(country, ())
                _cab_added = 0
                for _oq in _expect_cab:
                    _co = _office_holder_qid(_oq)
                    if _co and _co["qid"] not in _seen_q and (_co.get("name") or "").lower() not in _seen_nm:
                        _seen_q.add(_co["qid"]); _seen_nm.add(_co["name"].lower())
                        _cab_added += 1
                        out.append({"name": _co["name"], "title": _clean_cab_title(_co["title"]), "role": "Cabinet",
                                    "img": _co.get("img", ""), "x": None, "telegram": None, "truth": None})
                # Expected a Wikidata cabinet (e.g. the US VP) but got NOTHING back -> the offices 429'd. Flag it so
                # this profile isn't cached for the day with the VP missing; it self-heals on the next fetch.
                if _expect_cab and _cab_added == 0:
                    _cabinet_wiped = True
                # EVERY country: the current foreign + defence minister from Wikipedia's "List of current …
                # ministers" pages (one daily fetch each, all countries). Deduped so the US doesn't repeat.
                for _role, _listart in _MINISTER_LISTS:
                    _m = _minister_for(country, _listart)
                    if _m and _m.get("name") and _m["name"].lower() not in _seen_nm:
                        _seen_nm.add(_m["name"].lower())
                        out.append({"name": _m["name"], "title": _role, "role": "Cabinet",
                                    "img": _m.get("img", ""), "x": None, "telegram": None, "truth": None})
            except Exception:
                pass
            # governing lean = the party of whoever runs the government (head of gov, else head of state)
            ruling = hog if (hog and hog.get("qid")) else hos
            lean = _ruling_party_lean(ruling.get("qid"), pents.get(ruling.get("qid"))) if ruling else None
            # DEGRADED = Wikidata gave no person data (rate-limited), so names/photos came from the Factbook
            # fallback. Cached only briefly (see the read above) so it self-heals; also flagged to the client
            # so a degraded profile isn't kept for the day either.
            res = {"leaders": out, "lean": lean, "generated": int(time.time()),
                   "degraded": (not pents) or _cabinet_wiped, "ver": _LEADER_VER}
            if out:
                try:
                    json.dump(res, open(cache, "w", encoding="utf-8"))
                except Exception:
                    pass
            return res
        except Exception as ex:
            return {"leaders": [], "error": str(ex)}

    def summarize_event(self, title, url="", text="", source=""):
        """Meridian's OWN copyright-free summary of a story (3-4 original sentences). If given only a URL it
        reads the article text first — which is NEVER shown verbatim, only summarized in new words. Cached.
        `source` (the reporting outlet) is passed through so a state/partisan wire's contested claims are
        ATTRIBUTED, not stated as fact; it also decides whether the outlet earns a longer, in-depth brief.
        Returns {"summary": ""} when no LLM key is configured."""
        try:
            body = (text or "").strip()
            if not body and url and str(url).startswith("http"):
                d = self.article_detail(url) or {}
                body = " ".join((d.get("paragraphs") or [])[:16]).strip() or (d.get("desc") or "")
            # A fuller brief when the OUTLET reports at length OR the SCRAPED BODY is itself substantial — so a
            # big article from ANY real paper (Premium Times, a national daily) carries at least a paragraph,
            # not two sentences. A thin wire snippet still gets a tight brief.
            _dp = _indepth_source(source, _domain_of(url)) or len(body) >= 1600
            return {"summary": _summarize(title or "", body, source, _dp)}
        except Exception:
            return {"summary": ""}

    def _prewarm_summaries(self, events):
        """Summarize a whole feed's worth of stories AHEAD of any click, so the 'In brief' is already cached
        by the time a dot is opened (this is what makes the click instant for everyone). Uses the SAME code
        path a click would — an article URL scrapes+summarizes the article; a pure-Telegram post summarizes
        its own text — so the 30-day cache is shared and the click is a pure cache hit. Cheap after the first
        pass (article scrape + summary are both cached); a no-op when no summarizer is set. Runs in a pool."""
        def one(ev):
            try:
                u = (ev.get("url") or "").strip()
                src = (ev.get("source") or ev.get("channel") or "").strip()   # outlet/channel -> attributes contested claims
                s = ""
                if u.startswith("http") and "t.me/" not in u:
                    s = (self.summarize_event(ev.get("title") or "", u, "", src) or {}).get("summary", "")   # real article -> summarize it
                # Scrape blocked, empty, or a news.google.com REDIRECT with no body? Still write a brief from the
                # wire teaser we already hold (RSS <description>). This is what gives EVERY article a real "In
                # brief" — no story is left showing the raw, truncated teaser because its page couldn't be read.
                if not s and len((ev.get("sum") or "")) >= 60:
                    s = _summarize(ev.get("title") or "", ev.get("sum") or "", src, _indepth_source(src, ev.get("domain")))
                # BAKE OUR brief straight into the feed event. Warming the cache alone left every card depending
                # on a click-time call that raced the scrape and, when it lost, showed the raw wire teaser
                # ("…oil is down today a…"). With the brief on the event, world_events serves OUR summary for
                # every story with no per-click work — and the hosted/mobile feed inherits it for free.
                if s:
                    ev["summary"] = s
                # THE PICTURE. A story that shipped without its own image gets a story_photo (a real photo of
                # the subject/place). BAKE the resolved url onto the event so the hero paints INSTANTLY from the
                # served feed — not a black frame that only fills after a 1-5s click-time lookup. Warming the
                # cache alone left the cold-start feed with black heroes (Prudential Hong Kong shipped blank).
                if not ev.get("image") and not ev.get("photo"):
                    _ph = self.story_photo(ev.get("title") or "", ev.get("sum") or "",
                                           ev.get("place") or "", ev.get("country") or "") or {}
                    if _ph.get("url"):
                        ev["photo"] = _ph["url"]
                        _pc = _ph.get("credit") or _ph.get("title") or _ph.get("source") or ""
                        if _pc:
                            ev["photoCredit"] = _pc
            except Exception:
                pass
        try:
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as ex:
                list(ex.map(one, list(events)[:400]))   # the WHOLE feed (the map caps at 400): every story needs
                #   both a baked brief AND a SCOPE for the importance gate — a low-ranked local one at position 380
                #   must still be scored so it can be gated. Background + cached 30 days: one-time cost per new story.
        except Exception:
            pass

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
            # A GOOGLE-NEWS REDIRECT never serves the real article to a scraper — urllib lands on Google's
            # interstitial, whose og:image is the multicolour Google News LOGO and whose "body" is Google
            # chrome. Blank both so the hero falls back to a real place/subject photo (story_photo) and the
            # brief is written from the RSS teaser, never "brought to you by Google News". SHIPPED BUG: a
            # "UAE says Iran launched two missiles" card wore the Google News logo as its photo.
            if "news.google." in (url or "").lower():
                data["image"] = ""
                data["paragraphs"] = []
                if _JUNK_DESC.search(data.get("desc") or ""):
                    data["desc"] = ""
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


# Official primary sources that publish leaders'/governments' VERBATIM statements (RSS/Atom).
CURATED_FEEDS = {
    "Russia":                   ("Kremlin (Office of the President)", ["http://en.kremlin.ru/events/president/transcripts/feed"]),
    "United Kingdom":           ("10 Downing Street", ["https://www.gov.uk/search/news-and-communications.atom?organisations%5B%5D=prime-ministers-office-10-downing-street"]),
    "United States of America": ("The White House", ["https://www.whitehouse.gov/presidential-actions/feed/"]),
}


def _http_get(url, timeout=20):
    # Ask for gzip — these fetch large text (Factbook backgrounds, the ~71 KB heads list, minister lists,
    # feeds), which compress ~4-5x. urllib doesn't auto-decode, so we unzip when the server actually gzipped.
    UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)", "Accept-Encoding": "gzip"}
    resp = urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout)
    data = resp.read()
    if (resp.headers.get("Content-Encoding") or "").lower() == "gzip":
        try:
            data = gzip.decompress(data)
        except Exception:
            pass
    return data.decode("utf-8", "replace")


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
        # the statement TEXT itself (content:encoded is the fullest; description/summary the excerpt) — so the
        # card can QUOTE what was said, not just link its title. HTML-stripped, entity-decoded, capped.
        sm = (re.search(r"<content:encoded[^>]*>(.*?)</content:encoded>", b, re.S)
              or re.search(r"<description[^>]*>(.*?)</description>", b, re.S)
              or re.search(r"<summary[^>]*>(.*?)</summary>", b, re.S)
              or re.search(r"<content[^>]*>(.*?)</content>", b, re.S))
        summary = ""
        if sm:
            # UNESCAPE first (the feed's HTML is entity-encoded: &lt;p&gt;…), THEN strip the real tags — else the
            # tags reappear after decoding. Drop a leading image caption so the quote starts on the words.
            raw = _htmlmod.unescape(sm.group(1).replace("<![CDATA[", "").replace("]]>", ""))
            summary = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", raw)).strip()[:1200]
        if title:
            out.append({"title": title, "link": link, "date": date, "summary": summary})
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


# An IMPORTANT quote carries policy/consequence — not personal small talk. SHIPPED: a leader's card led with
# Lavrov's "I Know Rumen Radev Well", which tells a reader nothing. A quote qualifies when it names something
# consequential OR is a substantial sentence; a short personal/relational remark is dropped.
_TRIVIAL_QUOTE = re.compile(r"^\s*(?:i (?:know|met|like|love|respect|admire|remember|thank|appreciate|enjoy|"
                            r"believe in|trust)\b|(?:thank you|thanks|good (?:morning|evening|luck|day)|"
                            r"happy|congratulations|congrats|welcome|hello|greetings)\b)", re.I)
_STRONG_QUOTE = re.compile(r"\b(war|peace|cease[- ]?fire|attack|strike|missile|drone|nuclear|sanction|tariff|"
                           r"deal|agreement|treaty|election|threat\w*|retaliat\w*|offensive|troops?|weapon|"
                           r"invasion|occupation|genocide|terror\w*|security|alliance|nato|summit|negotiat\w*|"
                           r"defen[cs]e|demand\w*|condemn\w*|reject\w*|billion|million|killed|dead|victory|"
                           r"defeat|surrender|independence|sovereignty|corruption|border|energy|economy|"
                           r"will not|must|never|no longer|not allow|red line)\b", re.I)


def _quote_important(q):
    q = (q or "").strip().strip('“”"\'')
    if len(q) < 12:
        return False
    if _TRIVIAL_QUOTE.search(q) and not _STRONG_QUOTE.search(q):
        return False
    return len(q) >= 28 or bool(_STRONG_QUOTE.search(q))


def _port_prompt(where):
    """One prompt shared by the grounded (Gemini) and open-LLM port-profile fetchers. Asks for a compact,
    factual JSON — and to leave a field EMPTY rather than invent a figure, so we never show a made-up stat."""
    return (
        "Give a concise, factual profile of the seaport \"" + where + "\" as a JSON object with EXACTLY these "
        "string keys (use \"\" for any you are not confident about — never guess a number):\n"
        "- type: the kind of port in a few words (e.g. \"Container & transshipment\", \"Oil/LNG terminal\", "
        "\"Naval base\", \"Bulk cargo\").\n"
        "- opened: when the modern port was established or opened (a year or short phrase).\n"
        "- operator: the main operating company or port authority.\n"
        "- throughput: latest annual throughput with unit and year (e.g. \"~24.7M TEU (2023)\" or \"~90M tonnes/yr\").\n"
        "- ships_per_day: approximate vessel calls PER DAY (a number or short range) — derive it from public "
        "annual vessel-call figures if needed.\n"
        "- significance: ONE sentence on why this port matters (trade lane, largest in its region, chokepoint access).\n"
        "- recent: ONE sentence on a development in the last year or two (a new terminal, expansion, automation, "
        "or notable incident) — or \"\" if nothing notable.\n"
        "- waters: the sea, gulf, strait or route it sits on (e.g. \"Strait of Malacca\").\n"
        "Use only real, publicly reported facts. Output ONLY the minified JSON object, no markdown fences."
    )


def _port_json(text):
    """Pull the JSON object out of an LLM/Gemini reply and keep only the expected string fields."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        raw = json.loads(m.group(0))
    except Exception:
        return None
    keys = ("type", "opened", "operator", "throughput", "ships_per_day", "significance", "recent", "waters")
    out = {k: re.sub(r"\s+", " ", str(raw.get(k, "") or "")).strip() for k in keys}
    return out if any(out.values()) else None


def _port_profile_gemini(where, key):
    """Grounded profile via Gemini + Google Search — real public throughput/vessel figures and recent news."""
    try:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + GEMINI_MODEL + ":generateContent?key=" + urllib.parse.quote(key))
        body = json.dumps({
            "contents": [{"parts": [{"text": _port_prompt(where)}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.2, "maxOutputTokens": 900},
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=55) as r:
            j = json.loads(r.read().decode("utf-8"))
        return _port_json(j["candidates"][0]["content"]["parts"][0]["text"])
    except Exception:
        return None


def _port_profile_llm(where):
    """Fallback profile from the open model (no live web) — stable facts (type/opened/significance/waters);
    it is told to leave figures blank if unsure, so we don't show invented throughput numbers."""
    system = ("You are a neutral maritime reference. Answer only with real, publicly known facts about a named "
              "seaport, as strict JSON. If you are unsure of a figure, use an empty string — never invent one.")
    return _port_json(_llm_complete(system, _port_prompt(where), max_tokens=500, temperature=0.2))


# Curated, ALWAYS-available baseline so EVERY port has a profile even with no LLM key: a "cool" ranking/
# superlative and the waters it sits on. Well-established facts (2023 container-throughput standings + stable
# regional superlatives); the AI pass only ADDS figures/news on top and never overrides these. Keys must match
# the port names in PORTS (frontend). (name -> (ranking line, waters))
_PORT_INFO = {
    "Shanghai": ("The world's #1 busiest container port.", "East China Sea"),
    "Port of Singapore": ("The world's #2 port and its busiest transshipment hub.", "Singapore Strait"),
    "Ningbo": ("Among the world's top three container ports by volume.", "East China Sea"),
    "Shenzhen": ("One of the world's top five container ports.", "Pearl River Delta / South China Sea"),
    "Qingdao": ("A top-five global container port.", "Yellow Sea"),
    "Guangzhou": ("A top-ten global container port.", "Pearl River Delta"),
    "Busan": ("South Korea's largest port and a top-ten global transshipment hub.", "Korea Strait"),
    "Tianjin": ("Northern China's largest port; a top-ten global container port.", "Bohai Sea"),
    "Hong Kong": ("A top-ten global container port and historic free port.", "South China Sea"),
    "Rotterdam": ("Europe's largest and busiest port.", "North Sea"),
    "Antwerp": ("Europe's #2 port (Antwerp-Bruges).", "North Sea / Scheldt"),
    "Hamburg": ("Germany's largest port and Europe's #3.", "North Sea / Elbe"),
    "Jebel Ali": ("The largest and busiest port in the Middle East.", "Persian Gulf"),
    "Khalifa Port": ("Abu Dhabi's flagship deep-water port, among the world's fastest-growing.", "Persian Gulf"),
    "Los Angeles": ("The busiest container port in the United States.", "San Pedro Bay / Pacific"),
    "Long Beach": ("One of the two busiest US container ports, beside neighboring LA.", "San Pedro Bay / Pacific"),
    "New York/NJ": ("The busiest port on the US East Coast.", "New York Harbor / Atlantic"),
    "Savannah": ("One of the busiest and fastest-growing US container ports.", "Atlantic"),
    "Houston": ("The busiest US port by total tonnage.", "Gulf of Mexico"),
    "Santos": ("Latin America's largest and busiest port.", "South Atlantic"),
    "Colombo": ("South Asia's main transshipment hub.", "Indian Ocean"),
    "Piraeus": ("Greece's largest port and a major Mediterranean gateway.", "Aegean Sea / Mediterranean"),
    "Tanger Med": ("Africa's largest container port.", "Strait of Gibraltar"),
    "Durban": ("The busiest port in sub-Saharan Africa.", "Indian Ocean"),
    "Port Klang": ("Malaysia's largest port and a top-15 global container hub.", "Strait of Malacca"),
    "Tanjung Pelepas": ("A major Malaysian transshipment hub.", "Strait of Malacca"),
    "Kaohsiung": ("Taiwan's largest port.", "Taiwan Strait / South China Sea"),
    "Laem Chabang": ("Thailand's largest and busiest port.", "Gulf of Thailand"),
    "Tanjung Priok": ("Indonesia's largest and busiest port, serving Jakarta.", "Java Sea"),
    "Mundra": ("India's largest commercial port by cargo volume.", "Gulf of Kutch / Arabian Sea"),
    "Mumbai (JNPT)": ("India's largest container port (Nhava Sheva).", "Arabian Sea"),
    "Valencia": ("Spain's busiest container port and a Mediterranean leader.", "Mediterranean Sea"),
    "Algeciras": ("A leading Mediterranean transshipment hub, by the Strait of Gibraltar.", "Strait of Gibraltar"),
    "Felixstowe": ("The United Kingdom's busiest container port.", "North Sea"),
    "London Gateway": ("A major deep-water container port on the Thames.", "Thames / North Sea"),
    "Le Havre": ("One of France's largest ports, at the mouth of the Seine.", "English Channel"),
    "Marseille": ("France's largest port (Marseille-Fos).", "Mediterranean Sea"),
    "Gioia Tauro": ("Italy's largest transshipment container port.", "Mediterranean Sea"),
    "Genoa": ("Italy's busiest port.", "Ligurian Sea / Mediterranean"),
    "Vancouver": ("Canada's largest port.", "Pacific"),
    "Manzanillo": ("Mexico's busiest port.", "Pacific"),
    "Lázaro Cárdenas": ("A major Mexican Pacific container port.", "Pacific"),
    "Cartagena": ("A leading transshipment hub in the Caribbean.", "Caribbean Sea"),
    "Callao": ("Peru's largest and busiest port.", "Pacific"),
    "Buenos Aires": ("Argentina's busiest port.", "Río de la Plata"),
    "Salalah": ("A major Indian Ocean transshipment hub in Oman.", "Arabian Sea"),
    "Jeddah": ("Saudi Arabia's largest and busiest port.", "Red Sea"),
    "Dammam": ("Saudi Arabia's main Persian Gulf port.", "Persian Gulf"),
    "Hamad Port": ("Qatar's main port, a large modern greenfield build.", "Persian Gulf"),
    "Novorossiysk": ("Russia's largest port, on the Black Sea.", "Black Sea"),
    "St Petersburg": ("Russia's largest Baltic port.", "Baltic Sea / Gulf of Finland"),
    "Vladivostok": ("Russia's main Pacific port.", "Sea of Japan"),
    "Mombasa": ("East Africa's largest port.", "Indian Ocean"),
    "Lagos (Apapa)": ("Nigeria's busiest port.", "Gulf of Guinea / Atlantic"),
    "Karachi": ("Pakistan's largest and busiest port.", "Arabian Sea"),
    "Chittagong": ("Bangladesh's largest and busiest port.", "Bay of Bengal"),
    "Colón": ("A major transshipment hub at the Caribbean end of the Panama Canal.", "Caribbean Sea / Panama Canal"),
    "Balboa": ("A major port at the Pacific end of the Panama Canal.", "Pacific / Panama Canal"),
    "Port Said": ("A key port at the Mediterranean mouth of the Suez Canal.", "Mediterranean / Suez Canal"),
    "Melbourne": ("Australia's busiest container port.", "Port Phillip Bay"),
    "Port Botany": ("Sydney's main container port, among Australia's busiest.", "Botany Bay / Tasman Sea"),
    "Gdańsk": ("Poland's largest port and a fast-growing Baltic hub.", "Baltic Sea"),
    "Gothenburg": ("Scandinavia's largest port.", "Kattegat / North Sea"),
    "Odessa": ("Ukraine's largest and busiest port.", "Black Sea"),
    "Constanța": ("Romania's largest port and the biggest on the Black Sea.", "Black Sea"),
    "Haifa": ("One of Israel's two main ports.", "Mediterranean Sea"),
    "Ashdod": ("One of Israel's two main ports.", "Mediterranean Sea"),
    "Djibouti": ("A strategic Red Sea / Gulf of Aden port and regional transshipment hub.", "Gulf of Aden"),
    "Tauranga": ("New Zealand's largest port.", "Bay of Plenty / Pacific"),
    "Bandar Abbas": ("Iran's largest and most important port.", "Strait of Hormuz / Persian Gulf"),
    "Gwadar": ("A deep-water port on the Arabian Sea, central to China's Belt and Road.", "Arabian Sea"),
    "Aden": ("A historic port on one of the world's busiest shipping lanes.", "Gulf of Aden / Bab-el-Mandeb"),
}


def _port_baseline(name, country):
    """The zero-LLM profile shown for EVERY port: its type, a ranking/superlative (or a plain role line) and
    the waters it sits on. Guarantees the popup always has real content, even with no AI key."""
    out = {"type": "Container & general-cargo port"}
    info = _PORT_INFO.get(name)
    if info:
        out["rank"] = info[0]
        if info[1]:
            out["waters"] = info[1]
    elif country:
        out["significance"] = "A commercial seaport of " + _co_short(country) + "."
    return out


def _wiki_wikitext(title):
    """Lead-section wikitext (which contains the infobox) of a Wikipedia article, or '' — for basic facts."""
    try:
        url = ("https://en.wikipedia.org/w/api.php?format=json&action=parse&prop=wikitext&section=0&redirects=1&page="
               + urllib.parse.quote((title or "").replace(" ", "_")))
        j = json.loads(_http_get(url, 15))
        return (((j.get("parse") or {}).get("wikitext") or {}).get("*")) or ""
    except Exception:
        return ""


def _wt_clean(v):
    """De-wikify an infobox value to plain text: drop refs/comments, unwrap common templates and [[links]]."""
    v = v or ""
    v = re.sub(r"<!--.*?-->", "", v, flags=re.S)
    v = re.sub(r"<ref[^>]*/>", "", v, flags=re.I)
    v = re.sub(r"<ref[^>]*>.*?</ref>", "", v, flags=re.S | re.I)
    v = re.sub(r"\{\{\s*convert\s*\|([^{}]*)\}\}", lambda m: " ".join(m.group(1).split("|")[:2]), v, flags=re.I)
    v = re.sub(r"\{\{\s*(?:nowrap|nobr|nobreak|nobold|small|abbr)\s*\|([^{}]*)\}\}", r"\1", v, flags=re.I)
    # a date template ({{Start date|1965}}, {{Start date and age|1834|...}}) -> its year, so 'opened' survives
    v = re.sub(r"\{\{\s*(?:start[ _]?date(?:[ _]and[ _]age)?|date)\s*\|([^{}]*)\}\}",
               lambda m: (re.findall(r"\d{3,4}", m.group(1)) or [" "])[0], v, flags=re.I)
    for _ in range(3):                        # collapse remaining / one level of nested templates
        v = re.sub(r"\{\{[^{}]*\}\}", " ", v)
    v = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"\[\[([^\]]*)\]\]", r"\1", v)
    v = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", v)
    v = re.sub(r"\[https?://\S+\]", "", v)
    v = re.sub(r"'{2,}", "", v)
    v = re.sub(r"<[^>]+>", " ", v)
    return re.sub(r"\s+", " ", v).strip(" ,;|·—-")


def _infobox_map(wt):
    """{normalized_field: raw_value} from the first {{Infobox …}} block, brace/bracket-depth aware so a '|'
    inside a nested {{convert|…}} or [[link|text]] doesn't split a field."""
    m = re.search(r"\{\{\s*Infobox\b", wt, re.I)
    if not m:
        return {}
    i = m.start(); depth = 0; j = i
    while j < len(wt) - 1:
        two = wt[j:j+2]
        if two == "{{":
            depth += 1; j += 2; continue
        if two == "}}":
            depth -= 1; j += 2
            if depth == 0:
                break
            continue
        j += 1
    body = wt[i+2:max(i+2, j-2)]
    parts, cur, dc, db = [], "", 0, 0
    k = 0
    while k < len(body):
        two = body[k:k+2]
        if two == "{{": dc += 1; cur += two; k += 2; continue
        if two == "}}": dc -= 1; cur += two; k += 2; continue
        if two == "[[": db += 1; cur += two; k += 2; continue
        if two == "]]": db -= 1; cur += two; k += 2; continue
        ch = body[k]
        if ch == "|" and dc <= 0 and db <= 0:
            parts.append(cur); cur = ""; k += 1; continue
        cur += ch; k += 1
    parts.append(cur)
    out = {}
    for p in parts:
        if "=" in p:
            key, val = p.split("=", 1)
            key = re.sub(r"\s+", "", key).lower()
            if key:
                out[key] = val.strip()
    return out


def _port_infobox_facts(wt):
    """Basic port facts from the article's Infobox: opened/founded, type, operator, throughput, berths."""
    f = _infobox_map(wt)
    if not f:
        return {}

    def g(*keys):
        for k in keys:
            if f.get(k, "").strip():
                c = _wt_clean(f[k])
                if c:
                    return c
        return ""
    out = {}
    opened = g("opened", "built", "founded", "completed", "established", "constructionstartdate", "beganoperations")
    if opened:
        yr = re.search(r"\b(?:1[0-9]{3}|20[0-9]{2})\b", opened)
        out["opened"] = yr.group(0) if (yr and len(opened) > 20) else opened[:48]
    typ = g("type")
    if typ:
        out["type"] = typ[:48]
    op = g("operated", "operator", "owner", "manager", "managedby", "portauthority")
    if op:
        out["operator"] = op[:70]
    val = g("containervolume", "annualcontainervolume") or g("cargotonnage", "annualcargotonnage", "annualtonnage")
    if val:
        val = re.split(r"\s+(?:up|down|increase|decrease)\b", val, 1, flags=re.I)[0].strip(" ,;")
        # keep only a real figure (a number + a unit), so junk like a bare "(2025)" year is dropped
        if re.search(r"\d", val) and re.search(r"(TEU|tonne|\bton|cargo|million|billion|\bMT\b|\bm\b)", val, re.I):
            out["throughput"] = val[:60]
    berths = g("berths", "wharfs", "piers", "quays")
    if berths and re.search(r"\d", berths):
        out["berths"] = berths[:40]
    return out


def _port_wiki(name, country):
    """Photo + basic facts for a port from Wikipedia, in one page resolution: the port's own article
    ('Port of X' / 'X Port'), falling back to the city for a photo. Returns any subset of
    {photo,photo_title,opened,type,operator,throughput,berths} — real, cited public data, no LLM."""
    def _summary(t):
        return _wiki_json("https://en.wikipedia.org/api/rest_v1/page/summary/" + urllib.parse.quote((t or "").replace(" ", "_")))
    city = re.sub(r"\s*\([^)]*\)", "", name).split("/")[0].split(",")[0].strip()   # "Mumbai (JNPT)"->Mumbai, "New York/NJ"->New York
    titles = []
    for t in ["Port of " + city, city + " Port", "Port of " + name, name + " Port"]:
        if t and t not in titles:
            titles.append(t)
    out, ptitle, pimg = {}, "", ""
    for t in titles:
        j = _summary(t)
        if not j or j.get("type") == "disambiguation":
            continue
        ptitle = j.get("title") or t
        src = ((j.get("originalimage") or {}).get("source") or (j.get("thumbnail") or {}).get("source") or "")
        if src and _good_img(src):
            pimg = src
        facts = _port_infobox_facts(_wiki_wikitext(ptitle))
        if facts:
            out.update(facts)
        if out or pimg:
            break
    if not pimg and city:                          # a photo from the city page if the port article had none
        j = _summary(city)
        if j and j.get("type") != "disambiguation":
            src = ((j.get("originalimage") or {}).get("source") or (j.get("thumbnail") or {}).get("source") or "")
            if src and _good_img(src):
                pimg = src
                ptitle = ptitle or (j.get("title") or city)
    if pimg:
        out["photo"] = _wiki_thumb(pimg, 1280)
        if ptitle:
            out["photo_title"] = ptitle
    return out


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
""".split() +
# GENERIC INSTITUTION / BODY names that every country has — they must NOT count as a distinguishing shared
# name when matching a wire clip to a story. SHIPPED BUG: an ISRAEL clip ("Israel's SUPREME COURT overturned
# the Army Radio shutdown") was filed under a PAKISTAN Imran Khan COURT story purely on the shared "Supreme
# Court". A body's NAME is generic; only a person/place/company/ship distinguishes an event.
"""supreme court courts high tribunal parliament senate congress assembly cabinet ministry department
council commission committee agency authority bureau board panel radio television broadcaster network
police army navy air force forces guard troops soldiers court justice judge ruling verdict hearing
""".split())
_WEAK_MATCH = set()


def _init_weak_match():
    for key in list(COUNTRY_ALIASES.keys()) + list(DEMONYMS.keys()):
        for w in re.findall(r"[a-z]{4,}", key):
            _WEAK_MATCH.add(_stem(w))
    for w in _COMMON_MATCH:
        _WEAK_MATCH.add(_stem(w))


# TOPIC-GENERIC words — the vocabulary EVERY story on a subject shares, so they cannot prove two posts are the
# SAME event. "Iraq sells crude via Hormuz" and "China avoids Hormuz" share {oil, shipping, cargo, trade, firm}
# and the Strait, yet are different stories. Distinct from _GENERIC_WORDS (conflict filler). DELIBERATELY
# EXCLUDES concrete subjects that DO pin an event — "tanker"/"ship"/"refinery"/"supermarket"/"pub"/"fire".
_TOPIC_GENERIC = set(_stem(w) for w in (
    "oil gas crude fuel energy petroleum diesel gasoline petrol lng shipping cargo cargoes freight export "
    "exports import imports trade shipment shipments barrel barrels pipeline terminal market markets price "
    "prices supply commercial global economy economic company companies firm firms giant giants buyer buyers "
    "seller sellers deal deals sanction sanctions tariff tariffs official officials source sources report "
    "reports reported statement says said told claim claims reuters bloomberg wsj afp anadolu "
    # locational/connective function words that leak through _sigwords and must never count as a subject
    "through via across into onto amid among between within toward towards along about after over "
    # generic transaction/motion verbs — every trade/shipping story has them; not a shared SUBJECT
    "send sending sent buy buying bought sell selling sold stop stops stopped stopping allow allows allowed "
    "make makes making made move moves moving moved ease easing eased bring brings brought raise raises raising "
    "plan plans planned seek seeks progress talks talk meeting meet "
    # coincidental polysemous words that tie unrelated stories: "midterm RACES" vs "RACE to finalize a deal"
    "race races deadline deadlines push pushes drive drives bid bids "
    # conflict/infrastructure filler — a leader's STATEMENT about 'disrupting logistics' / hitting
    # 'infrastructure' must not attach to a strike ON a 'logistics hub' on that one shared word
    "logistics logistic infrastructure infrastructures response responses retaliation"
).split())

# Figures who appear across a huge share of the wire — their surname ALONE ties nothing, so a clip that shares
# only a ubiquitous name (plus one weak word) is coincidence, not the same event. Stemmed to match _proper_words.
_UBIQUITOUS_NAMES = set(_stem(w) for w in (
    "trump biden putin zelensky zelenskyy netanyahu xi jinping musk macron modi erdogan starmer lavrov "
    "rubio vance harris obama pope zuckerberg bezos"
).split())


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
    ev = _geolocate(event_title, "", "")
    cl = _geolocate(subject, "", subject)
    # A SHARED PLACE IS NOT A SHARED SUBJECT, and neither is TOPIC-GENERIC vocabulary. Two different stories
    # set at the same spot on the same topic — "Iraq sells crude via the Strait of Hormuz" and "China stops
    # shipping oil through the Strait of Hormuz" — share the place-name AND {oil, shipping, cargo, trade}, yet
    # they are DIFFERENT events. Strip BOTH the location (of either story) and the topic words from the shared
    # names and words, so a match needs a shared SPECIFIC subject: a company/person/ship/city, or a distinctive
    # non-topic word (the "tankers" that ties two Sea-of-Azov posts, the "supermarket" hit in Zaporozhye).
    ev_place = (ev[2] if ev else "") or ""
    cl_place = (cl[2] if cl else "") or ""
    place_toks = (_proper_words(ev_place) | _sigwords(ev_place)
                  | _proper_words(cl_place) | _sigwords(cl_place))
    _shared_proper = _proper_words(event_title) & _proper_words(subject)
    shared_names = _shared_proper - _WEAK_MATCH - place_toks
    # the distinctive WORDS must be CONTENT words, not the shared names again — a lone ubiquitous name (Trump)
    # that is also a sigword must not sneak in as a "shared word" and pass the single-word bar.
    shared_words = ((_sigwords(event_title) & _sigwords(subject))
                    - _GENERIC_WORDS - _WEAK_MATCH - _TOPIC_GENERIC - place_toks - _shared_proper
                    - _STRIKE_GENERIC - _MONEY_GENERIC)   # 'against'/'airstrike'/'million' are not a subject
    if not (shared_names or shared_words):
        return False                                   # nothing SPECIFIC shared beyond the place + the topic
    # SAME PLACE or SAME COUNTRY: one shared distinctive name OR word is the same event (the Odesa ship footage,
    # the Zaporozhye supermarket aftermath). SAME PLACE is checked too because a shared body of water gets a
    # different nominal COUNTRY depending on the actor named ("Russian tankers" -> Russia, "Ukrainian drones"
    # -> Ukraine) even though both posts are the ONE strike in the Sea of Azov. CROSS-BORDER (a person-led
    # story, Trump on Lindsey Graham): a strong NAME match stands in for the missing shared location.
    same_place = bool(ev_place and cl_place and ev_place == cl_place)
    same_country = bool(ev and cl and ev[3] and cl[3] == ev[3])
    # A single ubiquitous NAME alone (Trump + same country) is NOT the same event — it needs a shared
    # distinctive WORD, or a SECOND name. A single distinctive WORD (the "tanker"/"supermarket"/"pub") is
    # enough because it names the actual subject, not just a person the two stories both mention.
    # SHIPPED BUG: "Trump … data centers … midterm RACES" pulled in "US, Canada RACE to finalize trade deal"
    # — one coincidental word ("race") + Trump. When the ONLY shared name is a UBIQUITOUS figure (Trump, Biden,
    # Putin… — in a huge share of the wire), one weak shared word is coincidence: demand TWO distinctive words.
    if same_place or same_country:
        _only_ubiq = bool(shared_names) and not (shared_names - _UBIQUITOUS_NAMES)   # the ONLY shared name(s) are ubiquitous
        _need_words = 2 if _only_ubiq else 1
        return len(shared_names) >= 2 or len(shared_words) >= _need_words
    return len(shared_names) >= 2


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


def _dur_minutes(dur):
    """A clip's 'MM:SS' / 'H:MM:SS' duration as minutes (0 if unknown). A long value marks a roundup/stream,
    not single-event footage, so the media strip can drop it."""
    s = (dur or "").strip()
    if not re.match(r"^\d{1,2}(:\d{2}){1,2}$", s):
        return 0.0
    parts = [int(x) for x in s.split(":")]
    while len(parts) < 3:
        parts.insert(0, 0)
    return parts[0] * 60 + parts[1] + parts[2] / 60.0


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


# Strike/attack BOILERPLATE + geography words. On a FUZZY area centroid (a whole region like "Southern
# Lebanon", where the gazetteer couldn't pin the specific town), two DIFFERENT strikes fall on the SAME
# point and share this vocabulary — but sharing "air force airstrike against ... southern" does NOT make
# them the same event; the distinct TOWN is what separates them. So on an area place the reworded-merge
# needs a shared token BEYOND this set (see `_merge_same_event`). A real city dot is never gated by this.
_STRIKE_GENERIC = set(_stem(w) for w in (
    "against air force forces airstrike airstrikes strike strikes artillery shelling shell shells "
    "bombardment bombing bomb bombs raid raids attack attacks assault offensive drone drones missile "
    "missiles rocket rockets heavy targeting target targets targeted hit hits struck shell outskirt "
    "outskirts neighborhood neighbourhood vicinity area areas district districts sector axis front "
    "position positions town village city region north south east west northern southern eastern "
    "western central upper lower greater near overnight launches launched fire fired firing "
    "one two three four five six seven eight nine ten several multiple dozen dozens").split())

# Pure MAGNITUDE / quantity words — never a distinctive subject. "$400 million settlement" and "$725
# million UN payment" share {million}; that shared magnitude must NOT tie two unrelated money stories
# together (it filed a TikTok settlement onto a UN-debt dot). Stripped from clip/post matching alongside
# the topic + strike vocabulary. The actual SUBJECT (TikTok, ByteDance, UN) is what should match, not "million".
_MONEY_GENERIC = set(_stem(w) for w in (
    "million millions billion billions trillion trillions thousand thousands hundred hundreds "
    "percent percentage dollar dollars euro euros pound pounds worth amount sum figure").split())


def _same_story(a_toks, b_toks):
    """Text half of the duplicate test: True when two rich token sets overlap almost entirely (at least
    4 shared tokens AND an overlap coefficient >= 0.72). world_events gates this with same-place/country
    and a time window, so it only ever collapses the same story told twice — never two different ones."""
    if not a_toks or not b_toks:
        return False
    shared = len(a_toks & b_toks)
    return shared >= 4 and shared / min(len(a_toks), len(b_toks)) >= 0.72
# A SPORTS story earns a spot on a WORLD news map only when it is a MAJOR moment: a final, a
# championship/title decider, an Olympic or World-Cup medal, a world record, a nation qualifying for a
# World Cup. A routine international result — "Turkey beat Lithuania in women's volleyball" — is true but
# not world news, so a bare result verb ("beat", "won") no longer qualifies on its own; the headline must
# name the big STAGE or PRIZE. (Transfer/preview chatter never had a result and was already dropped.)
_SPORTS_MAJOR = re.compile(
    r"\b(finals?|semi[-\s]?finals?|quarter[-\s]?finals?|deciders?|"
    r"champions?|championships?|titles?|crowned|trophy|trophies|"
    r"world cup|world championships?|world series|super ?bowl|champions league|europa league|"
    r"stanley cup|nba finals?|grand slam|wimbledon|the masters|us open|french open|australian open|"
    r"the ashes|ballon d'?or|olympics?|olympic|paralympics?|(?:gold|silver|bronze)\s+medals?|"
    r"world record|record[-\s]?breaking|qualif(?:y|ies|ied)\s+for\b)\b", re.I)
def _sports_worthy(title):
    """Only keep sport when it's a MAJOR result — a final, a title/championship, an Olympic or World-Cup
    medal, a world record — the moments people actually follow. A routine international result ('Turkey
    beat Lithuania in women's volleyball') or transfer/preview chatter is true but not world news, so a
    bare 'beat/won' no longer qualifies; the headline must name the big stage or prize."""
    return bool(_SPORTS_MAJOR.search(title or ""))
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
    # HUMAN-INTEREST FEATURE, not a located event: a personal journey/profile ("From Sudan to Spain:
    # Between war and home", "One man's escape…", "Meet the…"). These are the 'read my story' pieces, not
    # a dot of something that HAPPENED somewhere.
    r"^from\s+[a-z'.\-]+\s+to\s+[a-z'.\-]+\s*:|"
    r"\b(one (?:man|woman|family|refugee|migrant|boy|girl)'?s?\s+(?:story|journey|struggle|escape|ordeal|"
    r"fight|life)|a day in the life|meet the |portrait of|my journey|how i (?:escaped|fled|survived|left|made))\b|"
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
    # CULTURE / ENTERTAINMENT feature — not a located event on a world news map. SHIPPED: "How Two British
    # Historians Made a Smash Hit Podcast" was a dot in the UK. Podcasts, box-office, celebrity, viral clips,
    # streaming hits, memoirs — the arts desk, not the front page.
    r"\b(podcast|box office|red carpet|blockbuster|memoir|celebrity|reality (?:tv|show)|"
    r"smash hit|streaming (?:hit|sensation|giant)|hit (?:show|series|podcast|album|single|movie|film)|"
    r"binge-?watch|fan-?favou?rite|goes? viral|viral (?:video|clip|moment|sensation)|"
    r"dating app|horoscope|zodiac|makeover|recipe)\b|"
    r"\?\s*$"                                       # question headlines are debates, not events
    r")")


# A THEMATIC think-piece / analysis headline is "Country: <ideology theme>" with no number and no event
# verb — "Germany: Islamism and right-wing extremism", "France: The rise of populism". The map is a board
# of located EVENTS, not op-eds/explainers, so these get no dot. SYMMETRIC BY DESIGN: it keys on the
# ANALYSIS shape (a topic label, not a thing that happened), so it drops an ideology essay whatever side it
# argues — a "rise of the far right" piece and a "roots of woke capitalism" piece alike. It is NOT a
# political-viewpoint filter (which would just bias the feed the other way); it removes the op-ed FORMAT.
_THINK_HEAD = re.compile(r"^[A-Z][\w.&'-]*(?:\s+[\w.&'-]+){0,2}:\s+(.+)$")
_THINK_THEME = re.compile(
    r"(extremism|extremist|radicali[sz]|ideolog|islamism|islamist|nationalis|populis|fascis|jihadis|"
    r"terroris|supremac|islamophob|xenophob|antisemit|anti-semit|far.?right|far.?left|neo.?nazi|"
    r"the (rise|fall|threat|future|return|roots|making|legacy|meaning|danger|paradox|problem) of|"
    r"culture war|disinformation|misinformation|propaganda|weaponi[sz])", re.I)
_THINK_VERB = re.compile(
    r"\b(?:kill|hurt|dead|die|injur|wound|arrest|attack|strike|struck|hit|sign|win|won|launch|seiz|storm|"
    r"raid|ban|vote|elect|election|quit|resign|warn|say|said|claim|urge|accus|deploy|fire|shell|bomb|kidnap|"
    r"releas|sentenc|charg|jail|protest|rally|rallies|clash|erupt|flee|fled|evacuat|rescu|destroy|damag|"
    r"captur|surrender|defect|repel|advanc|withdraw|announc|approv|reject|nominat|appoint|meet|hold|reopen|"
    r"shut|close|open|vow|slam|condemn|threaten|impose|order|surg|march)(?:s|es|ed|ing|d)?\b", re.I)


def _is_thinkpiece(title):
    """A 'Country: <ideology/theme>' analysis headline with no number and no event verb — an op-ed/explainer
    shape, not a located event. See the _THINK_* notes: symmetric across the political spectrum by design."""
    m = _THINK_HEAD.match(title or "")
    if not m:
        return False
    rest = m.group(1)
    if re.search(r"\d", rest) or _THINK_VERB.search(rest):
        return False                              # a number or an action => a real event/report, keep it
    return bool(_THINK_THEME.search(rest))


# A NEWSLETTER DIGEST headline joins SEVERAL unrelated stories with ". And,/Plus,/Also,/Meanwhile," — NPR's
# "Up First" shape ("Trump declares economic warfare on Iran. And, SCOTUS to rule on the ballroom"). It's not
# ONE event, so it's a poor map dot AND it mis-pairs wire clips (a clip about one half matches the whole
# digest — the reported "ballroom wire post -> Iran-warfare dot"). Needs a period + a capitalised digest word
# + comma, so a normal headline with 'and' isn't caught.
_DIGEST_RE = re.compile(r"[.!?]\s+(?:And|Plus|Also|Meanwhile|Elsewhere)\s*,\s+\w")


def _is_fluff(title, url=""):
    """True for features/op-eds/documentaries/analysis that aren't a real event worth a dot on the map.
    Deliberately does NOT require an 'event verb' in general — that wrongly dropped real news ('missiles
    have IMPACTED the port', 'president NOMINATES a PM'). Losing real news is worse than keeping a feature.
    The narrow _is_thinkpiece() exception only fires on a 'Country: ideology-theme' headline with NO event
    verb and NO number, so it can't swallow a real event. A multi-story newsletter DIGEST is also dropped."""
    low = (url or "").lower()
    for p in _FLUFF_PATHS:
        if p in low:
            return True
    return (bool(_FLUFF_PAT.search(title or "")) or _is_thinkpiece(title or "")
            or bool(_DIGEST_RE.search(title or "")))


# A COVERT-SURVEILLANCE / espionage story (a state spying on a dissident, tapping phones, a mercenary
# "operation" that is monitoring not fighting) is a POLITICAL / foreign-interference story — not the red
# "security" bucket the map reserves for STRIKES and violence. "UAE-funded mercenary … covert London
# operation" scored security purely on the word "mercenary". Downgrade ONLY when there is no actual violence.
_ESPIONAGE_RE = re.compile(r"\b(covert[\w\s]{0,25}operation|surveillance|spied on|spying on|wiretap\w*|"
                           r"eavesdrop\w*|bugged|phone[-\s]?hack\w*|secretly (?:monitor|surveil|track|follow)\w*|"
                           r"covert operation|espionage)\b", re.I)
_VIOLENCE_RE = re.compile(r"\b(kill\w*|dead|death toll|attack\w*|strike\w*|bomb\w*|wound\w*|shot|shoot\w*|"
                          r"raid\w*|assault\w*|clash\w*|explos\w*|casualt\w*|massacre|airstrike|missile|shell\w+)\b", re.I)


def _classify(title, desc=""):
    """Score every category and take the highest — never first-match-wins (see the notes on CAT_ORDER).
    If the headline alone is inconclusive (a bare damage report scored 0 and fell to the 'politics'
    default), score again over the story's own text before giving up."""
    best, best_score = _score_cats(title)
    if best_score == 0 and desc:
        best, best_score = _score_cats(title + " " + desc[:300])
    if best == "security":            # a non-violent espionage/surveillance story is politics, not red
        _t = (title or "") + " " + (desc or "")
        if _ESPIONAGE_RE.search(_t) and not _VIOLENCE_RE.search(_t):
            return "politics"
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


# ---------------------------------------------------------------------------
# Cache janitor — a self-clocking daily sweep that reclaims disk from EXPIRED cache files ("the waste").
# It is deliberately conservative: it only deletes a file whose mtime is older than a floor WELL PAST the
# longest TTL any cache uses (30 days), so a file it removes is already DEAD — _fresh() ignores it and no
# live dot reads it. It NEVER touches the app's actual state: the served feed windows (world_*), a dot's
# LOCATION (aiwhere_*), the dedup/merge verdicts (dedup_*), or leader identity are protected by prefix
# regardless of age. Net effect: disk goes down, dots and data are untouched.
# ---------------------------------------------------------------------------
_PURGE_AGE = 45 * 86400     # a cache file untouched this long is past every 30-day TTL -> safe to drop
_PURGE_EVERY = 86400        # sweep at most once a day
# Live state — NEVER auto-cleared, whatever its age. Feeds refresh on their own; the rest position/merge dots
# or are leader identity that panels show. Everything NOT on this list is a cheap, regenerable derived cache.
_PURGE_PROTECT = ("world_", "hosted_", "starred_", "clipsfeed", "livewire_", "aiwhere_", "dedup_",
                  "leaders_", "heads_of_state_gov", "dead_leaders", "office_", "minlist_", "leader_",
                  "person_", "update_check")


def _purge_stale_cache():
    """Delete cache files past every TTL (mtime older than _PURGE_AGE), except the protected live-state
    prefixes. Self-clocked to ~once/day via a marker file. Purely reclaims disk; changes no dot or datum.
    Runs in the background refresh thread, wrapped so a failure never affects the feed."""
    try:
        marker = os.path.join(CACHE_DIR, ".last_purge")
        if _fresh(marker, _PURGE_EVERY):        # already swept within the last day -> nothing to do
            return
        try:
            with open(marker, "w", encoding="utf-8") as f:   # claim the run up-front so two threads don't both sweep
                f.write(str(int(time.time())))
        except Exception:
            return
        cutoff = time.time() - _PURGE_AGE
        for name in os.listdir(CACHE_DIR):
            if name.startswith(".") or name.startswith(_PURGE_PROTECT):
                continue
            p = os.path.join(CACHE_DIR, name)
            try:
                if os.path.isfile(p) and os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except Exception:
                pass
    except Exception:
        pass


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
            _purge_stale_cache()                    # daily disk housekeeping (marker-guarded); never touches live data
            api._build_world_events(h, live=True)   # background: full AI geo + dedup, warms the cache for next time
        except Exception:
            pass
        finally:
            with _WORLD_REFRESH_LOCK:
                _WORLD_REFRESH.discard(h)
    threading.Thread(target=_run, daemon=True).start()


# The moment a feed is built/served, summarize every story in the BACKGROUND so that by the time anyone
# clicks a dot the copyright-free "In brief" is already cached — no spinner, no wait. Deduped per feed
# build (window + generated-timestamp) so a 10-min poll doesn't re-spawn it.
_PREWARMED = set()
_PREWARM_LOCK = threading.Lock()


def _spawn_summary_prewarm(api, h, data):
    if not isinstance(data, dict) or not data.get("events"):
        return
    if not _llm_available():                        # no summarizer configured (Groq/Gemini/Ollama) -> nothing to warm
        return
    key = (h, data.get("generated"))
    with _PREWARM_LOCK:
        if key in _PREWARMED:
            return
        _PREWARMED.add(key)
        if len(_PREWARMED) > 64:
            _PREWARMED.clear()
            _PREWARMED.add(key)
    events = list(data.get("events") or [])

    def _work():
        # Summarize the feed (warms the 30-day cache AND bakes ev["summary"] onto each event), then re-save the
        # served json so world_events hands OUR brief to the card directly — no click-time generation.
        try:
            api._prewarm_summaries(events)
        finally:
            if any(e.get("summary") for e in events):
                try:
                    cache = os.path.join(CACHE_DIR, "world_%dh.json" % h)
                    # Don't clobber a NEWER feed a concurrent rebuild may have written — its own prewarm bakes it.
                    on_disk = None
                    if os.path.exists(cache):
                        try:
                            on_disk = json.load(open(cache, encoding="utf-8"))
                        except Exception:
                            on_disk = None
                    if on_disk is None or on_disk.get("generated") == data.get("generated"):
                        tmp = cache + ".tmp"
                        json.dump(data, open(tmp, "w", encoding="utf-8"))
                        os.replace(tmp, cache)      # atomic: a polling client reads the old OR new file, never a torn one
                except Exception:
                    pass
    threading.Thread(target=_work, daemon=True).start()


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
    # Fail FAST when Wikidata is rate-limiting: retrying 1.5s later just hits the same 429 while the
    # profile panel spins. The caller falls back to the Factbook names instantly and the short-TTL cache
    # retries Wikidata in ~20 min, so a quick single retry is all that's worth blocking the UI for.
    for attempt in range(2):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Meridian/1.0"})
            raw = urllib.request.urlopen(req, timeout=8).read().decode("utf-8", "replace")
            return (json.loads(raw) or {}).get("entities", {}) or {}
        except urllib.error.HTTPError as ex:
            if getattr(ex, "code", None) == 429 and attempt < 1:
                time.sleep(0.4)
                continue
            return {}
        except Exception:
            if attempt < 1:
                time.sleep(0.4)
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


# Connective particles carry no identity — "bin", "al", "de", "von" appear in millions of unrelated names.
# Counting them as shared tokens made 'Muhammad bin Salman al Saud' match the unrelated 'Muhammad Said
# al-Attar' (shared "muhammad"+"al") and wrongly flagged the living Crown Prince as a dead leader.
_NAME_KEY_PARTICLES = {"bin", "bint", "ibn", "al", "el", "la", "le", "van", "von", "der", "den",
                       "de", "da", "dos", "das", "of", "the", "ben", "abu"}


def _name_key(s):
    return [w for w in re.sub(r"[^a-z ]", " ", _fold(s or "").lower()).split()
            if len(w) > 1 and w not in _NAME_KEY_PARTICLES]


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


# Names Wikidata has told us (via P570, date of death) belong to DECEASED leaders. Persisted to disk so a
# rate-limited fetch that falls back to the CIA Factbook — which lags reality by months and still lists the
# dead as sitting heads of state — can refuse to resurrect them. This is what stops Iran showing the late
# Ali Khamenei (d. 2026-02-28) whenever Wikidata is briefly unreachable, and generalises to every country.
_DEAD_LEADERS = None
_DEAD_PATH = os.path.join(CACHE_DIR, "dead_leaders.json")


def _dead_leaders():
    global _DEAD_LEADERS
    if _DEAD_LEADERS is None:
        try:
            _DEAD_LEADERS = set(json.load(open(_DEAD_PATH, encoding="utf-8")))
        except Exception:
            _DEAD_LEADERS = set()
    return _DEAD_LEADERS


def _mark_dead(name):
    if not name:
        return
    s = _dead_leaders()
    if name not in s:
        s.add(name)
        try:
            json.dump(sorted(s), open(_DEAD_PATH, "w", encoding="utf-8"))
        except Exception:
            pass


def _is_dead(name):
    """Does this (Factbook) name belong to someone we've learned is deceased? Uses the same-person match
    (given name AND family name), NOT a bare surname — so 'Ali Hoseini-Khamenei' matches the late Ali
    Khamenei, but the living King SALMAN bin Abdulaziz Al Saud is NOT confused with a dead former king who
    shares 'bin Abdulaziz Al Saud'."""
    if not name:
        return False
    return any(_same_person(name, None, d, None) for d in _dead_leaders())


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


def _wd_img(e):
    """The Commons portrait URL for a Wikidata entity (P18), or ''."""
    p18 = e.get("claims", {}).get("P18", [])
    if p18:
        fn = p18[0].get("mainsnak", {}).get("datavalue", {}).get("value", "")
        if fn:
            return ("https://commons.wikimedia.org/wiki/Special:FilePath/"
                    + urllib.parse.quote(fn.replace(" ", "_")) + "?width=240")
    return ""


def _succession_current(person_qid, office_qid, hops=3):
    """Follow Wikidata's 'replaced by' (P1366) chain from a FORMER office-holder to the CURRENT one. This is
    what keeps leadership up to date the DAY a PM changes: editors mark the outgoing term ended and set who
    replaced them long before a country's head-of-government (P6) or the office's officeholder (P1308) is
    rewired. SHIPPED BUG: the UK still showed Keir Starmer (term ended 2026-07-20) because the only fresh
    signal — 'replaced by Andy Burnham' — sat on Starmer's ended term and was never read. Matches the SAME
    office so the chain can't wander onto another position; alive + unended = the current holder."""
    if not (person_qid and office_qid):
        return None
    q, seen, first = person_qid, set(), True
    for _ in range(hops):
        if not q or q in seen:
            break
        seen.add(q)
        e = _wd_entities(q, "labels|claims").get(q, {})
        if not e:
            break
        term = None
        for c in e.get("claims", {}).get("P39", []):
            if _wd_claim_qid(c) == office_qid:
                if term is None or (_wd_qual_time(c, "P580") or "") >= (_wd_qual_time(term, "P580") or ""):
                    term = c                                   # this office, its most recent term
        ended = bool(term) and bool(_wd_qual_time(term, "P582"))
        alive = not e.get("claims", {}).get("P570")
        if not first and alive and not ended:                  # a successor whose term is ongoing -> CURRENT
            name = (e.get("labels", {}).get("en", {}) or {}).get("value", "")
            return {"qid": q, "name": name, "img": _wd_img(e)} if name else None
        rep = (term or {}).get("qualifiers", {}).get("P1366", [])   # 'replaced by' on the ended term
        q = ((rep[0].get("datavalue", {}).get("value", {}) if rep else {}) or {}).get("id")
        first = False
    return None


# Verified cabinet OFFICE entities whose Wikidata 'current officeholder' (P1308) is reliably maintained, so we
# resolve them directly (one fetch, no search) — accurate and cheap. Curated because a generic per-country
# name search found nothing outside the US anyway and rate-limited Wikidata enough to degrade the core cards.
_CABINET_QIDS = {
    # US cabinet in order of precedence, from Wikidata offices whose current-officeholder (P1308) is reliably
    # maintained — these give the EXACT US titles (and portraits) the minister lists below can't. Deduped by
    # name against those lists, so the US shows the precise title, not a generic 'Finance Minister'.
    "United States of America": [
        "Q11699",    # Vice President
        "Q14213",    # Secretary of State
        "Q4215834",  # Secretary of the Treasury
        "Q735015",   # Secretary of Defense
        "Q642859",   # Secretary of Homeland Security
    ],
}


def _office_holder_qid(office_qid):
    """Current officeholder (P1308, unended, living) of a known office entity: {qid,name,img,title}, or None.
    Cached ~daily on disk: the VP (and other precise-title offices) have NO minister-list fallback, so a
    transient Wikidata 429 during a full profile fetch must not be able to erase an already-known holder.
    Only successful lookups are cached — a rate-limited None never poisons the cache, it just retries."""
    cache = os.path.join(CACHE_DIR, "office_" + re.sub(r"[^\w]", "", office_qid) + ".json")
    if _fresh(cache, 20 * 3600):
        try:
            return json.load(open(cache, encoding="utf-8")) or None
        except Exception:
            pass
    e = _wd_entities(office_qid, "labels|claims").get(office_qid, {})
    cur = [c for c in e.get("claims", {}).get("P1308", []) if "P582" not in c.get("qualifiers", {})]
    if not cur:
        return None
    pq = _wd_claim_qid(cur[0])
    pe = _wd_entities(pq, "labels|claims").get(pq, {}) if pq else {}
    nm = (pe.get("labels", {}).get("en", {}) or {}).get("value", "")
    if not pq or not nm or pe.get("claims", {}).get("P570"):
        return None
    res = {"qid": pq, "name": nm, "img": _wd_img(pe),
           "title": (e.get("labels", {}).get("en", {}) or {}).get("value", "")}
    try:
        json.dump(res, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return res


# CABINET FOR EVERY COUNTRY — from Wikipedia's "List of current X ministers" pages. Each is ONE article that
# tabulates the current holder for ~200 countries, so a single daily fetch covers them all (no per-country
# title guessing, and — being Wikipedia, not Wikidata — no rate-limit on the core leader lookups). The map's
# country names differ from the list's spellings for a handful of states; alias those.
_MINISTER_LISTS = [
    ("Foreign Minister", "List of current foreign ministers"),
    ("Defence Minister", "List of current defence ministers"),
    ("Finance Minister", "List of current finance ministers"),
    ("Interior Minister", "List of current interior ministers"),
]
_MINLIST_ALIAS = {
    "United States of America": "United States", "Czechia": "Czech Republic", "S. Sudan": "South Sudan",
    "Dem. Rep. Congo": "Democratic Republic of the Congo", "Congo": "Republic of the Congo",
    "Côte d'Ivoire": "Ivory Coast", "The Netherlands": "Netherlands", "Bahamas": "The Bahamas",
    "Cabo Verde": "Cape Verde", "Gambia": "The Gambia", "Micronesia": "Federated States of Micronesia",
    "Sao Tome and Principe": "São Tomé and Príncipe", "Timor Leste": "East Timor", "Vatican": "Vatican City",
    "Myanmar": "Myanmar", "North Macedonia": "North Macedonia",
}


_MINLIST_MEM = {}   # in-process cache (list_article -> (t, data)): browsing many countries reuses ONE parse
                    # instead of re-reading + re-loading the same disk JSON on every country.


def _current_ministers(list_article):
    """Parse a Wikipedia 'List of current X ministers' table into {country: {name, article}} — the current
    holder for EVERY country from ONE page. Cached ~daily on disk, so one fetch serves all countries."""
    _m = _MINLIST_MEM.get(list_article)
    if _m and time.time() - _m[0] < 3600:
        return _m[1]
    cache = os.path.join(CACHE_DIR, "minlist_" + _slug(list_article) + ".json")
    if _fresh(cache, 20 * 3600):
        try:
            d = json.load(open(cache, encoding="utf-8"))
            _MINLIST_MEM[list_article] = (time.time(), d)
            return d
        except Exception:
            pass
    out = {}
    try:
        url = ("https://en.wikipedia.org/w/api.php?format=json&action=parse&prop=wikitext&redirects=1&page="
               + urllib.parse.quote(list_article.replace(" ", "_")))
        wt = (((json.loads(_http_get(url, 20)).get("parse") or {}).get("wikitext") or {}).get("*")) or ""
        for row in re.split(r"\n\|-", wt):
            cm = re.search(r"\{\{flag(?:country|icon|deco)?\|([^}|]+)", row)
            if not cm:
                continue
            country = cm.group(1).strip()
            art = ""
            for cell in re.split(r"\n\|(?!\})", row):
                if "List]]" in cell or "|List" in cell or "flag" in cell:
                    continue
                lm = re.search(r"\[\[([^\]|#]+)", cell)
                if lm and not lm.group(1).strip().lower().startswith(
                        ("list", "ministry", "minister of", "minister for", "department", "office of", "secretary of state for")):
                    art = lm.group(1).strip()
                    break
                sm = re.search(r"\{\{sortname\|([^}|]+)\|([^}|]+)", cell)
                if sm:
                    art = (sm.group(1).strip() + " " + sm.group(2).strip()).strip()
                    break
            if country and art:
                out[country] = {"name": re.sub(r"\s*\([^)]*\)\s*$", "", art).strip(), "article": art}
    except Exception:
        pass
    if out:
        try:
            json.dump(out, open(cache, "w", encoding="utf-8"))
        except Exception:
            pass
        _MINLIST_MEM[list_article] = (time.time(), out)
    return out


def _minister_for(country, list_article):
    """The current minister {name, img} for a country from a 'List of current X ministers' page, or None."""
    m = _current_ministers(list_article)
    rec = m.get(country) or m.get(_MINLIST_ALIAS.get(country, "\0"))
    if not rec or not rec.get("name"):
        return None
    img = ""
    try:
        js = _wiki_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                        + urllib.parse.quote((rec.get("article") or rec["name"]).replace(" ", "_")))
        if js and js.get("type") != "disambiguation":
            img = ((js.get("originalimage") or {}).get("source")
                   or (js.get("thumbnail") or {}).get("source") or "")
    except Exception:
        pass
    return {"name": rec["name"], "img": img}


def _wiki_person_img(name_or_article):
    """A person's portrait URL from their Wikipedia summary (originalimage/thumbnail), or ''."""
    if not name_or_article:
        return ""
    try:
        js = _wiki_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                        + urllib.parse.quote(str(name_or_article).replace(" ", "_")))
        if js and js.get("type") != "disambiguation":
            return ((js.get("originalimage") or {}).get("source")
                    or (js.get("thumbnail") or {}).get("source") or "")
    except Exception:
        pass
    return ""


# AUTHORITATIVE, DAILY-CURRENT LEADERS — Wikipedia's "List of current heads of state and government" is one
# page, edited within hours of any change, that tabulates EVERY country's head of state and head of government.
# It is more current than Wikidata's P6 or the CIA Factbook snapshot (both lag reshuffles by weeks), so we use
# it as the source of truth for WHO currently holds each post, cross-checked against what we already resolved.
_HEADS_HOG = ("prime minister", "premier", "chancellor", "taoiseach", "chief minister",
              "president of the government", "minister-president", "head of government",
              "president of the council of ministers", "chief executive", "prime ministers",
              "chairman of the council of ministers", "chairperson of the council of ministers",
              "council of ministers")
_HEADS_HOS_STRONG = ("king", "queen", "emperor", "sultan", "emir", "grand duke", "president",
                     "supreme leader", "co-prince", "monarch", "captain regent", "captains regent",
                     "yang di-pertuan agong", "amir", "sovereign", "ngwenyama", "prince regnant")
_HEADS_HOS_WEAK = ("crown prince", "general secretary", "chairman", "chairperson", "supreme", "paramount")
_HEADS_NOT_PERSON = ("council", "commission", "government", "committee", "presidency", "authority",
                     "junta", "assembly", "confederation", "cabinet", "members")


def _heads_link(s):
    m = re.search(r"\[\[([^\]]+)\]\]", s or "")
    if not m:
        return ("", "")
    inner = m.group(1)
    if "|" in inner:
        a, d = inner.split("|", 1)
        return (a.strip(), d.strip())
    return (inner.strip(), inner.strip())


def _heads_clean_name(n):
    n = re.sub(r"\{\{[^{}]*\}\}", "", n or "")
    n = re.sub(r"[{}\[\]]", "", n)
    n = re.sub(r"<[^>]+>", "", n)
    n = re.sub(r"\s*\(.*$", "", n)
    n = re.sub(r"'{2,}", "", n)
    return re.sub(r"\s+", " ", n).strip(" |-")


def _heads_clean_title(t):
    t = re.sub(r"\{\{[^{}]*\}\}", "", t or "")
    t = re.sub(r"(?i)\b(?:success|operational|small|nowrap|nobold|smalldiv|align=left|align=center)\b", "", t)
    t = re.sub(r"[{}\[\]|<>=\"]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def _heads_parse_cell(cell):
    """A table cell '[[Office|Title]]&nbsp;- [[Person|Name]]' -> (title, name, person_article)."""
    c = re.sub(r"<ref[^>]*>.*?</ref>", "", cell or "", flags=re.S)
    c = re.sub(r"<ref[^>]*/>", "", c)
    c = re.sub(r"\{\{efn[^{}]*(?:\{\{[^{}]*\}\}[^{}]*)*\}\}", "", c)
    c = re.sub(r"\{\{(?:small|nowrap|nobold|smalldiv)\|", "", c)
    if "–" not in c and "—" not in c:
        return (None, None, None)
    left, right = re.split(r"&nbsp;\s*[–—]|\s[–—]\s?|[–—]", c, maxsplit=1)
    _, disp = _heads_link(left)
    title = _heads_clean_title(disp or left)
    art, pdisp = _heads_link(right)
    name = _heads_clean_name(pdisp) if pdisp else _heads_clean_name(right)
    if name and any(w in name.lower() for w in _HEADS_NOT_PERSON):   # an institution isn't a person
        return (title, None, None)
    return (title, name or None, art or name)


_HEADS_MEM = {"t": 0, "data": None}   # in-process cache: one parse serves every country in the session


def _current_heads():
    """Parse the 'List of current heads of state and government' page into
    {country: {'hos':{name,title,article}|None, 'hog':{...}|None}} — one daily fetch covers every country."""
    if _HEADS_MEM["data"] is not None and time.time() - _HEADS_MEM["t"] < 3600:
        return _HEADS_MEM["data"]
    cache = os.path.join(CACHE_DIR, "heads_of_state_gov.json")
    if _fresh(cache, 20 * 3600):
        try:
            d = json.load(open(cache, encoding="utf-8"))
            _HEADS_MEM["data"] = d; _HEADS_MEM["t"] = time.time()
            return d
        except Exception:
            pass
    out = {}
    try:
        url = ("https://en.wikipedia.org/w/api.php?format=json&action=parse&prop=wikitext&redirects=1&page="
               + urllib.parse.quote("List of current heads of state and government".replace(" ", "_")))
        wt = (((json.loads(_http_get(url, 25)).get("parse") or {}).get("wikitext") or {}).get("*")) or ""
        wt = re.sub(r"<!--.*?-->", "", wt, flags=re.S)    # HTML comments hide some rows (e.g. Spain's King)
        blocks, cur = {}, None
        for seg in re.split(r"\n\|-", wt):
            fm = re.search(r"\{\{flag(?:country|icon|deco)?\|([^}|]+)", seg)
            if fm:
                cur = re.sub(r"\s*\(country\)$", "", fm.group(1).strip())
                cur = {"Kingdom of the Netherlands": "Netherlands"}.get(cur, cur)
                blocks.setdefault(cur, [])
            if cur is None:
                continue
            for line in re.split(r"\n\|", seg):
                if "{{flag" in line or not line.strip():
                    continue
                t, n, art = _heads_parse_cell(line)
                if n and len(n) > 1 and not n.lower().startswith(("list", "vacant", "member")):
                    blocks[cur].append((t or "", n, art, bool(re.search(r'colspan="2"', line))))
        for country, cells in blocks.items():
            if not cells:
                continue

            def _find(keys):
                for t, n, art, cs in cells:
                    if any(k in (t or "").lower() for k in keys):
                        return {"name": n, "title": t, "article": art}
                return None
            hog = _find(_HEADS_HOG)
            hos = _find(_HEADS_HOS_STRONG) or _find(_HEADS_HOS_WEAK)
            # A president sitting under a paramount leader (Supreme Leader / monarch / party secretary) is that
            # country's head of GOVERNMENT (Iran and some one-party states have no separate PM).
            if hos and not hog:
                if any(k in (hos["title"] or "").lower() for k in
                       ("supreme leader", "general secretary", "king", "emperor", "monarch", "crown prince")):
                    pres = next(({"name": n, "title": t, "article": art}
                                 for t, n, art, cs in cells if "president" in (t or "").lower()), None)
                    if pres and pres["name"] != hos["name"]:
                        hog = pres
            if hos or hog:
                out[country] = {"hos": hos, "hog": hog}
    except Exception:
        pass
    if out:
        try:
            json.dump(out, open(cache, "w", encoding="utf-8"))
        except Exception:
            pass
        _HEADS_MEM["data"] = out; _HEADS_MEM["t"] = time.time()
    return out


def _heads_for(country):
    """The current {hos, hog} for a map country name from the heads-of-state list, or None."""
    h = _current_heads()
    return h.get(country) or h.get(_MINLIST_ALIAS.get(country, "\0"))


# The CIA World Factbook is more COMPLETE than Wikidata for "who holds power" (it always lists a chief of
# state AND a head of government), so we parse its free-text fields to fill gaps — e.g. Saudi Arabia, whose
# head of government (Crown Prince MBS) Wikidata doesn't record. Factbook capitalises the surname and puts
# the office first: "Crown Prince and Prime Minister MUHAMMAD BIN SALMAN bin Abd al-Aziz Al Saud (since …)".
_FB_TITLES = set(("president prime minister supreme leader king queen emir emira sultan monarch chancellor "
                  "chief prince princess sheikh sheikha shaykh grand duke duchess governor general captain "
                  "regent co-prince sovereign acting interim transitional head state council chairperson "
                  "chairman chairwoman presidential federal vice deputy and the of crown paramount "
                  "government premier chief-executive").split())
_FB_PARTICLES = {"bin", "al", "of", "the", "von", "van", "de", "da", "del", "el", "ibn", "abu", "abd", "ben", "bint", "la"}


def _fb_parse(raw):
    """(name, title) from a Factbook chief-of-state / head-of-government string. Returns ('', '') if blank."""
    # Drop EVERY parenthetical, not just "(since …)". SHIPPED BUG: Spain's field is "President of the
    # Government (Prime Minister) Pedro SANCHEZ Perez-Castejon (since …)" — splitting on the FIRST '(' kept
    # only "President of the Government" and lost the name. Removing all parens leaves "President of the
    # Government Pedro SANCHEZ Perez-Castejon" for the title/name split below.
    s = re.sub(r"\s*\([^)]*\)", " ", raw or "")
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
        elif len(t) >= 2 and re.fullmatch(r"[IVXLCDM]+", t):
            parts.append(t)                                        # a regnal number stays uppercase: "FELIPE VI" -> "Felipe VI"
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
        j = None
        for attempt in range(2):        # a 429 here (after country_leaders' other WD calls) was dropping MBS
            try:
                j = json.loads(urllib.request.urlopen(
                    urllib.request.Request(url, headers={"User-Agent": "Meridian/1.0"}),
                    timeout=12).read().decode("utf-8", "replace"))
                break
            except Exception:
                if attempt < 1:
                    time.sleep(0.5)
                    continue
                return None
        ids = [h.get("id") for h in ((j or {}).get("search") or []) if h.get("id")]
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


# A trailing "— X" on a headline is a PUBLISHER byline (strip it) only when X is an outlet; when X is who the
# claim is sourced TO ("— platoon commander", "— Zelensky", "— officials", "— ministry") it is part of the
# news and stays. A byline is a curated outlet, or a Title-Case name carrying a press-marker word; a lowercase
# role, or a person/institution being quoted, is an attribution.
_PRESS_MARKER = re.compile(
    r"\b(News|Times|Post|Herald|Journal|Daily|Press|Media|Agency|Wire|Newswire|Network|Tribune|Gazette|"
    r"Chronicle|Observer|Bulletin|Broadcasting|Online|Today|Digest|Insider|Monitor|Dispatch|Telegraph|"
    r"Guardian|Independent|Reporter|Standard|Mirror|Express)\b")


def _is_outlet_byline(tail):
    tail = (tail or "").strip().strip("\"'.")
    if not tail or not tail[0].isupper():
        return False                                            # lowercase role ('platoon commander') = attribution
    if re.fullmatch(r"(?:" + _OUTLET_NAMES_RE + r")", tail, re.I):
        return True                                             # a curated outlet name (Reuters, BBC, TASS…)
    return bool(_PRESS_MARKER.search(tail))                     # "… Daily News", "The X Times" -> a publisher


_DANGLE_TAIL_RE = re.compile(r"[\s,;:]+(?:and|or|but|nor|so|yet|the|a|an|that|which|who|whose|whom|"
                             r"including|during|amid|plus|versus|vs)\s*[.…]*\s*$", re.I)


def _clean_headline(t):
    t = _htmlmod.unescape(t or "")
    t = _strip_promo(t)                                          # bare links, "Follow @x for more news", @handles
    t = re.sub(r"\s+([,.;:!?])", r"\1", t)                       # GDELT spaces before punctuation
    t = re.sub(r"([a-z]),([A-Za-z])", r"\1, \2", t)             # "positions,militants" -> "positions, militants"
    # strip a trailing " - Outlet" BYLINE (Google-News aggregation furniture) whenever a REAL headline
    # (>= 20 chars) remains before the dash — so a short "UAE says … at it - Reuters" loses its byline too.
    # But a trailing "— X" is ONLY a byline when X is a PUBLISHER; a SOURCE/SPEAKER attribution is part of the
    # news and MUST stay. SHIPPED BUG: TASS's "Battlegroup North … positions, militants — platoon commander"
    # lost "— platoon commander" (who the claim is sourced to), which read oddly and dropped real information.
    # The separator needs whitespace on BOTH sides, so a compound ("anti-corruption", "U-turn", "COVID-19")
    # or a range ("2020–2024") is never chopped.
    _m = re.match(r"^(.*\S)\s+[-–—|]\s+([^-–—|]{2,32})$", t)
    if _m and len(_m.group(1)) >= 20 and _is_outlet_byline(_m.group(2)):
        t = _m.group(1)
    t = re.sub(r"\s{2,}", " ", t).strip()
    # A wire post truncated mid-clause becomes a headline that ends on "…" or a dangling connector
    # ("…improve its domestic production of long-range missiles and."). Drop the ellipsis and the dangling
    # conjunction/article so the headline reads as a finished thought — it should never end on "and."/"…".
    if len(t) > 40:
        t = re.sub(r"\s*(?:\.{2,}|…)+\s*$", "", t).strip()
        t = _DANGLE_TAIL_RE.sub("", t).strip()
    if len(t) <= 200:
        return t
    # NEVER a mid-word chop (shipped "…and Western offici"): trim to the last clause break, else the last
    # whole word, within the limit, and mark it continued with "…".
    seg = t[:200]
    mcl = max(seg.rfind(", "), seg.rfind("; "))
    if mcl >= 100:
        return seg[:mcl].rstrip(" ,;:–—-.") + "."
    sp = seg.rfind(" ")
    return (seg[:sp] if sp >= 100 else seg).rstrip(" ,;:–—-.") + "."


def _good_img(u):
    """A REAL news photo, not a logo / flag / branded 'share card'. Outlets (esp. state wires like TASS/RT)
    ship a house-brand card as og:image when an article has no photo — those must be rejected so the app
    falls back to a real event or LOCATION photo. Telegram link-previews carry the same cards, so this now
    filters them too (see the event build)."""
    if not u or not u.startswith("http"):
        return False
    lu = u.lower()
    bad = (
        # generic 'no real photo' cards
        "og-image", "og_image", "og-default", "og_default", "/og-", "/og_", "opengraph", "open-graph",
        "twitter-card", "twittercard", "summary-card", "placeholder", "no-image", "noimage", "no_image",
        "default.jp", "default.pn", "default-", "-default.", "_default.", "/default", "share_default",
        "share-image", "share-card", "-share.", "/social", "social-", "socialcard", "meta-image",
        "meta_image", "preview-default", "/card.", "card-default", "generic-", "stub.",
        # logos / brand marks / flags
        "/logo", "-logo", "logo.", "logo_", "_logo", "site-logo", "header-logo", "brand-", "/brand",
        "sprite", "favicon", "blank.", "watermark", "/flag", "-flag.", "flag-",
        # Google branding — a Google-News RSS item's url is a news.google.com REDIRECT, whose og:image is the
        # multicolour Google News mark (it shipped as a Spain wildfire story's hero). gstatic.com is Google's
        # static-asset/branding CDN — never a news photo. (lh*.googleusercontent.com proxies REAL cached photos,
        # so it is deliberately NOT blocked.)
        "gstatic.com", "news.google.", "googlelogo", "google_news", "google-news", "googlenews",
        # country FLAGS, coats of arms, and LOCATOR MAPS (Wikipedia's lead image for a country is usually one
        # of these) — a flag/map is never the "picture" of a news story. Real photos are jpg/png/webp, so an
        # .svg is always a flag/logo/map. Never show one; fall back to the coloured category card instead.
        "flag_of", "flag of", "flag%20of", "/flag_", "coat_of_arms", "coat-of-arms", "coat%20of%20arms",
        ".svg", "orthographic", "locator", "location_map", "location-map", "_map.", "on_the_globe", "(projection", "%28orthographic",
        "map_of", "map-of", "map%20of", "_map_", "-map-", "old_map", "historical_map", "topographic", "cartogram", "blank_map",
        # house 'brand card' filenames (extend as spotted — keep to the CARD, not the whole domain, so real
        # photos from the same outlet still show)
        "tass_logo", "og-tass", "tass-card", "tass-cover", "tass-og", "rt-logo", "sputnik-logo", "ria-logo",
    )
    return not any(b in lu for b in bad)


def _wiki_thumb(url, px=1280):
    """Bound a Wikimedia image URL to a <=px-wide THUMBNAIL. Wikipedia's `originalimage` is the FULL-RES
    commons original — routinely 10-30 MB — which never finishes loading in the webview, so the hero stays a
    black frame (the "many pictures are black" bug). A thumbnail is a few hundred KB and always paints.
    Handles the three shapes we emit: an already-rendered /thumb/.../NNNpx- URL (swap the size), a
    Special:FilePath redirector (add ?width=), and a bare /commons/a/bc/File.ext original (build its thumb)."""
    if not url or ("wikimedia.org" not in url and "Special:FilePath" not in url):
        return url
    if re.search(r"/\d+px-[^/]*$", url):                          # already a rendered thumbnail -> resize
        return re.sub(r"/\d+px-([^/]*)$", (r"/%dpx-\1" % px), url)
    if "Special:FilePath/" in url:                               # redirector -> ask it for a bounded width
        return url if re.search(r"[?&]width=", url) else url + ("&" if "?" in url else "?") + "width=" + str(px)
    m = re.match(r"^(https?://upload\.wikimedia\.org/wikipedia/[^/]+)/([0-9a-fA-F]{1,2})/"
                 r"([0-9a-fA-F]{1,2})/([^/?#]+)$", url)
    if m:                                                        # bare full-res original -> its /thumb/ URL
        base, d1, d2, fname = m.groups()
        return "%s/thumb/%s/%s/%s/%dpx-%s" % (base, d1, d2, fname, px, fname)
    return url


_OUTLET_NAMES = {
    "aljazeera.com": "Al Jazeera", "aljazeera.net": "Al Jazeera", "bbc.com": "BBC",
    "bbc.co.uk": "BBC", "cnn.com": "CNN", "reuters.com": "Reuters", "apnews.com": "AP",
    "theguardian.com": "The Guardian", "nytimes.com": "The New York Times",
    "washingtonpost.com": "The Washington Post", "wsj.com": "The Wall Street Journal",
    "ft.com": "Financial Times", "bloomberg.com": "Bloomberg", "dawn.com": "Dawn",
    "scmp.com": "South China Morning Post", "france24.com": "France 24",
    "dw.com": "Deutsche Welle", "npr.org": "NPR", "politico.com": "Politico",
    "axios.com": "Axios", "cnbc.com": "CNBC", "moneycontrol.com": "Moneycontrol",
    "antiwar.com": "Antiwar.com", "news.antiwar.com": "Antiwar.com",
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
    # regional powers + broader perspective set
    "cgtn.com": "CGTN", "middleeastmonitor.com": "Middle East Monitor",
    "tehrantimes.com": "Tehran Times", "riotimesonline.com": "The Rio Times",
    "antaranews.com": "Antara", "premiumtimesng.com": "Premium Times",
    "mexiconewsdaily.com": "Mexico News Daily",
    # Western populist / far-right
    "breitbart.com": "Breitbart", "thegatewaypundit.com": "The Gateway Pundit",
    "zerohedge.com": "ZeroHedge",
}


def _domain_name(domain):
    d = (domain or "").lower().strip()
    if d.startswith("www."):
        d = d[4:]
    parts = d.split(".")
    # exact match, then drop the leftmost sub-domain label and retry (news.cgtn.com -> cgtn.com,
    # en.antaranews.com -> antaranews.com) so one mapped base domain names all its sub-domains.
    for i in range(max(1, len(parts) - 1)):
        cand = ".".join(parts[i:])
        if cand in _OUTLET_NAMES:
            return _OUTLET_NAMES[cand]
    core = parts[0] if parts and parts[0] else ""
    return (core[:1].upper() + core[1:]) if core else (domain or "Source")


# WHO IS REPORTING — a short, FACTUAL note on the outlet's ownership so a reader can weigh its likely slant.
# It states OWNERSHIP/funding, never a verdict ("propaganda"): state-owned wires ARE state media, a fact that
# is true whoever the state is. Deliberately EVEN-HANDED — Russian, Chinese, Iranian, Gulf, US-funded and
# Western public broadcasters are all labelled by the same ownership standard. Each entry is (substrings, note);
# a substring is matched against the lowercased outlet NAME and its DOMAIN, first match wins. Unknown outlets
# get NO note (better silent than a guessed label).
_SOURCE_ORIGIN = [
    (("tass", "rt.com", " rt ", "russia today", "ria novosti", "ria.ru", "sputnik", "izvestia",
      "izvestija", "rossiyskaya", "vesti", "zvezda", "regnum", "gazeta.ru"), "Russian state media"),
    (("cgtn", "xinhua", "global times", "globaltimes", "people's daily", "peoples daily", "china daily",
      "chinadaily", "cctv", "ecns.cn"), "Chinese state media"),
    (("press tv", "presstv", "tasnim", "fars news", "farsnews", "fars.", "irna", "mehr news", "mehrnews",
      "islamic republic news", "tehran times", "khamenei.ir"), "Iranian state media"),
    (("kcna", "korean central news", "rodong"), "North Korean state media"),
    (("anadolu", "aa.com.tr", "trt world", "trtworld", "daily sabah", "dailysabah"), "Turkish state media"),
    (("al jazeera", "aljazeera"), "Qatari state-funded"),
    (("wam", "emirates news agency", "the national ae", "thenationalnews"), "UAE state media"),
    (("saudi press agency", "spa.gov", "al arabiya", "alarabiya", "asharq"), "Saudi-owned media"),
    (("prensa latina", "granma", "cubadebate"), "Cuban state media"),
    (("telesur",), "Venezuelan state-funded"),
    (("wafa", "palestine news"), "Palestinian Authority media"),
    (("syrian arab news", "sana.sy", " sana "), "Syrian state media"),
    (("bbc",), "UK public broadcaster"),
    (("voice of america", "voanews", "radio free europe", "rferl", "radio liberty", "radio free asia"),
     "US government-funded"),
    (("deutsche welle", "dw.com"), "German public broadcaster"),
    (("france 24", "france24", "rfi ", "radio france"), "French public broadcaster"),
    (("npr", "pbs"), "US public broadcaster"),
]


def _source_note(source, domain=""):
    """A short factual ownership note for an outlet ('Russian state media', 'UK public broadcaster') or "" if
    it's an ordinary/unknown outlet. Lets a reader gauge likely slant without the app taking a side."""
    hay = " " + (source or "").lower().strip() + " " + (domain or "").lower().strip() + " "
    for subs, note in _SOURCE_ORIGIN:
        if any(s in hay for s in subs):
            return note
    return ""


# Outlets that report AT LENGTH — a brief from one of these may run a little longer to carry the quotes,
# figures and context they actually provide (see _summarize). Matched like _source_note. A wire snippet or a
# thin aggregator is NOT here, so its brief stays tight.
_INDEPTH_SOURCES = (
    "new york times", "nytimes", "washington post", "washingtonpost", "wall street journal", "wsj",
    "the guardian", "theguardian", "reuters", "associated press", "ap news", "apnews", "afp",
    "bloomberg", "the economist", "financial times", " ft.com", "the atlantic", "bbc", "npr",
    "politico", "foreign policy", "foreignpolicy", "der spiegel", "spiegel", "le monde", "the times",
    "los angeles times", "latimes", "the new yorker", "propublica", "axios", "cnn", "abc news", "cbs news",
    "nbc news", "haaretz", "times of israel", "timesofisrael", "kyiv independent", "kyivindependent",
    # major NATIONAL / quality papers worldwide — they publish full articles, so their briefs run fuller
    "premium times", "premiumtimes", "the punch", "punchng", "vanguard", "the nation", "thisday", "daily trust",
    "the guardian nigeria", "daily nation", "the standard", "the east african", "mail & guardian", "news24",
    "the citizen", "the hindu", "times of india", "hindustan times", "indian express", "dawn", "the daily star",
    "the diplomat", "al-monitor", "al monitor", "nikkei", "south china morning post", "scmp", "straits times",
    "the jakarta post", "the korea herald", "yonhap", "kyodo", "the japan times", "el país", "el pais",
    "le figaro", "der standard", "corriere", "la repubblica", "el universal", "clarín", "clarin", "la nación",
    "folha", "o globo", "the moscow times", "novaya", "meduza", "rappler", "the irish times",
)


def _indepth_source(source, domain=""):
    hay = " " + (source or "").lower().strip() + " " + (domain or "").lower().strip() + " "
    return any(s in hay for s in _INDEPTH_SOURCES)


# Outlets the user has HIDDEN from the map — no dots, no citations. Matched as a lowercase substring of an
# article's domain or source name, so "theguardian.com" also hides www./amp. variants. Code default plus a
# user-editable muted_sources.txt (one entry per line, # comments) in DATA_DIR — if that file exists it
# REPLACES the default, so the user can add or clear mutes without a code change.
_MUTED_FILE = os.path.join(DATA_DIR, "muted_sources.txt")
_MUTED_DEFAULT = ("theguardian.com", "thegatewaypundit.com", "gateway pundit")
_MUTED_POOL = {"t": 0.0, "set": None}


def _muted_sources():
    now = time.time()
    if _MUTED_POOL["set"] is not None and now - _MUTED_POOL["t"] < 30:
        return _MUTED_POOL["set"]
    out = None
    try:
        if os.path.exists(_MUTED_FILE):
            out = set()
            for line in open(_MUTED_FILE, encoding="utf-8").read().splitlines():
                line = line.split("#", 1)[0].strip().lower()
                if line:
                    out.add(line)
    except Exception:
        out = None
    if out is None:
        out = {s.lower() for s in _MUTED_DEFAULT}
    _MUTED_POOL["t"], _MUTED_POOL["set"] = now, out
    return out


def _is_muted(domain, source="", url=""):
    """Is this article from a muted outlet? Checks domain, source name AND url so a Guardian story reaches
    the map via none of the three paths (feeds, GDELT, a citation)."""
    muted = _muted_sources()
    if not muted:
        return False
    blob = ((domain or "") + " " + (source or "") + " " + (url or "")).lower()
    return any(m in blob for m in muted)


# How map-worthy a category is, most first. A strike outranks an analysis piece at the same spot.
_SEVERITY = {"security": 0, "climate": 1, "health": 2, "society": 3, "economy": 4,
             "tech": 5, "politics": 6, "sports": 7}


_NUMWORD = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8,
            "nine": 9, "ten": 10, "eleven": 11, "twelve": 12, "dozen": 12, "thirteen": 13, "fourteen": 14,
            "fifteen": 15, "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19, "twenty": 20,
            "thirty": 30, "forty": 40, "fifty": 50}
_TOLL_N = (r"(\d{1,4}|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve|dozen|thirteen|"
           r"fourteen|fifteen|sixteen|seventeen|eighteen|nineteen|twenty|thirty|forty|fifty)")
# (?<![\w-]) so a compound like 'twenty-one' can't match the bare 'one' inside it and report 1. The gap
# between the number and 'killed' must NOT cross an injured-word, so 'N injured and M killed' reads M, not N.
_TOLL_P1 = re.compile(r"(?<![\w-])" + _TOLL_N + r"(?:\s+(?!injur|wound|hurt|maim|hospital)\w+){0,3}?\s+"
                      r"(?:killed|dead|deaths?|died|fatalities|perished|lives\s+lost)\b", re.I)
_TOLL_P2 = re.compile(r"\b(?:kill(?:s|ed|ing)?|claim(?:s|ed)?|leav(?:es|ing)|left|dead[:,]?)\s+"
                      r"(?:at least\s+|nearly\s+|some\s+|up to\s+)?(?<![\w-])" + _TOLL_N + r"\b", re.I)


def _death_toll(text):
    """The casualty figure a story leads with ('9 dead', 'kills nine', 'left nine dead') as an int, or
    None. A shared, specific death toll is the strongest signal that two differently-worded reports cover
    the SAME event — 'Kyiv: 9 dead', 'barrage kills nine', 'left nine civilians dead' are all one story."""
    text = text or ""
    for p in (_TOLL_P1, _TOLL_P2):
        m = p.search(text)
        if m:
            v = m.group(1).lower()
            n = int(v) if v.isdigit() else _NUMWORD.get(v)
            if n and n <= 5000:
                return n
    return None


# symmetric to _TOLL_P1: the gap must NOT cross a killed-word, so 'Seven killed and 40 injured' reads 40.
_INJ_P1 = re.compile(r"(?<![\w-])" + _TOLL_N + r"(?:\s+(?!kill|dead|died|death|fatal|perish)\w+){0,3}?\s+"
                     r"(?:injured|wounded|hurt|maimed|hospitali[sz]ed)\b", re.I)
_INJ_P2 = re.compile(r"\b(?:injur(?:e|es|ed|ing)|wound(?:s|ed|ing)?|hurt|hospitali[sz]e[sd]?)\s+"
                     r"(?:at least\s+|nearly\s+|some\s+|up to\s+|more than\s+)?(?<![\w-])" + _TOLL_N + r"\b", re.I)


def _injured_toll(text):
    """Companion to _death_toll: the injured/wounded figure a story gives ('40 injured', 'wounded nine',
    'hospitalized 21'), or None. Two reports that match on BOTH killed AND injured are almost certainly the
    same incident even when nothing else lines up — a two-number fingerprint the merge can trust anywhere,
    with no geographic constraint (one dot may sit on 'Black Sea', the other on the named town)."""
    text = text or ""
    for p in (_INJ_P1, _INJ_P2):
        m = p.search(text)
        if m:
            v = m.group(1).lower()
            n = int(v) if v.isdigit() else _NUMWORD.get(v)
            if n and n <= 20000:
                return n
    return None


# A top-tier actor whose ON-RECORD statement is consequential enough to belong on the world map even when
# the AI, judging the wording alone, filed it as 'local'. Deliberately institutions/offices (not every named
# person) so it stays a tight safety net, not a second classifier.
_MAJOR_ACTOR = re.compile(
    r"\b(president|prime minister|foreign minister|defen[cs]e minister|chancellor|"
    r"kremlin|white house|pentagon|state department|secretary of (?:state|defen[cs]e)|"
    r"supreme leader|ayatollah|(?:the\s+)?(?:un|eu|nato|imf|opec|g7|g20)|central bank|"
    r"federal reserve|the fed|parliament|congress|senate|politburo|"
    r"prosecutor|attorney general|sanctions?|tariffs?|ceasefire|treaty)\b", re.I)


# A hit on STRATEGIC INFRASTRUCTURE is map-worthy even with no casualties and even when the target's side
# downplays it ("a technical failure, no casualties") — a refinery fire / drone strike on a power plant / a
# depot ablaze is exactly the war-and-security news the map exists for. Needs BOTH an attack/damage word AND
# a strategic-facility word, so an ordinary "kitchen fire" or "port city" never trips it.
_ATTACK_WORD = re.compile(
    r"(?i)\b(?:strike|struck|strikes|hit|hits|hitting|attack(?:s|ed|ing)?|shell(?:s|ed|ing)?|"
    r"drone|drones|missile|missiles|explosion|explode[ds]?|blast|blaze|ablaze|fire|"
    r"damaged?|destroy(?:s|ed|ing)?|sabotage[ds]?|set\s+ablaze|caught\s+fire|on\s+fire)\b")
_STRATEGIC_FACILITY = re.compile(
    r"(?i)\b(?:refiner(?:y|ies)|oil\s+depot|oil\s+terminal|fuel\s+depot|oil\s+hub|oil\s+storage|"
    r"pipeline|power\s+(?:plant|station)|nuclear\s+(?:plant|power\s+plant)|substation|power\s+grid|"
    r"military\s+base|air\s?base|airfield|naval\s+base|shipyard|ammunition\s+depot|"
    r"arms\s+depot|weapons?\s+depot|munitions?\s+depot|drone\s+factory|missile\s+(?:plant|factory))\b")


# A LONE ACCIDENTAL casualty is LOCAL news. An electrocution, a single drowning/road/workplace accident,
# a fall — these kill someone but change nothing beyond the family, so they must NOT auto-qualify as
# world-map "hard news" the way a strike or a shooting does. Only a VIOLENT cause or a MASS event does.
_ACCIDENTAL = re.compile(
    r"\b(electrocut\w*|drown\w*|road\s+accident|traffic\s+accident|car\s+crash|road\s+crash|workplace\s+accident|"
    r"industrial\s+accident|construction\s+accident|fell\s+(?:from|to|into)|slipped|collaps\w*|accidental\w*|"
    r"mishap|lightning|snakebite|snake\s+bite|electrical\s+fault)\b", re.I)
_VIOLENT_CAUSE = re.compile(
    r"\b(strike|strikes|struck|attack\w*|shell\w*|shot|shoot\w*|gun\w*|stab\w*|bomb\w*|blast|explos\w*|"
    r"airstrike|air\s+strike|missile|drone|raid\w*|killed\s+by|murder\w*|assassinat\w*|clash\w*|militant\w*|"
    r"terror\w*|forces|troops|soldier\w*|gunman|shelling|ambush|massacre|beheaded|lynch\w*|riot\w*)\b", re.I)


def _hard_news(title, desc=""):
    """A deterministic 'this matters regardless' net for the importance gate — so the world map NEVER hides a
    mass-casualty event, a top-official statement, or a strike on strategic infrastructure, even if the AI
    rated the wording 'local'. Everything else defers to the AI's SCOPE."""
    t = title or ""
    both = t + " " + (desc or "")
    _toll = _death_toll(t) or _death_toll(desc or "")
    if _toll or _injured_toll(t):
        # A LONE ACCIDENTAL death/injury (a workplace electrocution, one road accident) is LOCAL — defer it to
        # the AI scope instead of forcing it onto the world map. A mass event (3+) or a VIOLENT cause is kept.
        if (_toll or 0) <= 2 and _ACCIDENTAL.search(both) and not _VIOLENT_CAUSE.search(both):
            pass
        else:
            return True                                # a shooting / attack / disaster with casualties
    if _MAJOR_ACTOR.search(t) and _TG_STMT_VERB.search(t):
        return True                                    # a government / leader / institution on the record
    both = (title or "") + " " + (desc or "")
    if _ATTACK_WORD.search(both) and _STRATEGIC_FACILITY.search(both):
        return True                                    # a strike/fire on a refinery, depot, plant, base…
    return False


# SOFT / HUMAN-INTEREST news the world map doesn't want (the STARRED-country feed still carries it): consumer
# travel gripes, animal-welfare and "community concern" pieces. A reliable keyword backstop for when the AI
# scope pass didn't mark it "local" — SHIPPED: a Qantas passenger-compensation story and a kangaroo-drowning
# story both became world dots. Gated AFTER _hard_news, so a plane CRASH or a real disaster is never caught.
_SOFT_NEWS = re.compile(
    r"\b("
    r"(?:passengers?|travell?ers?|tourists?|holidaymakers?)\s+(?:stranded|stuck|demand|left\s+stranded)"
    r"|stranded\s+(?:passengers?|travell?ers?|tourists?)"
    r"|flight\s+(?:delays?|cancellations?)|delayed\s+flights?"
    r"|kangaroos?|koalas?|wombats?|platypus|possums?"
    r"|animal\s+welfare|wildlife\s+(?:concern|rescue|welfare)|stray\s+(?:dogs?|cats?)"
    r"|community\s+concern|goes?\s+viral|heart-?warming|feel-?good"
    # LOCAL LIFESTYLE / EVENTS — a beer festival, a concert, a fair. Zero geopolitical consequence, so never
    # a world dot (the STARRED-country feed still carries them). Gated after _hard_news, so a deadly stampede
    # or an attack AT a festival — which carries casualties — is never caught here.
    r"|(?:beer|wine|food|music|jazz|art|arts|cultural|street|craft|folk|film|comedy|book|seafood|coffee)\s+(?:festival|fair|fest)"
    r"|festival\s+(?:hits|opens|returns|kicks\s+off|features|celebrates|draws|brings)"
    r"|free\s+(?:beer|drinks?|food|entry|concerts?)|local\s+brews?|craft\s+(?:beer|brews?)"
    r"|(?:concert|gig|carnival|parade|pageant|gala|marathon|fun\s+run|fashion\s+week|comic\s+con|"
    r"food\s+fair|street\s+fair|county\s+fair|state\s+fair|talent\s+show|beauty\s+pageant)\b"
    r"|things\s+to\s+do|what'?s\s+on\s+this\s+weekend|weekend\s+guide|line-?up\s+includes"
    r"|celebrity|red\s+carpet|box\s+office|reality\s+(?:tv|show)|dating\s+show"
    # PROFESSIONAL-MISCONDUCT / REGULATORY / CELEBRITY-LEGAL — a doctor cleared by a medical watchdog, a
    # lawyer struck off, a "to the stars" professional's tribunal. Local human-interest, not region-changing.
    # (A malpractice case with casualties trips _hard_news first, so it is never caught here.)
    r"|to\s+the\s+stars\b|professional\s+misconduct|medical\s+(?:council|board|watchdog|tribunal|regulator)"
    r"|disciplinary\s+(?:hearing|tribunal|action|panel|proceedings|committee)|struck\s+off"
    r"|misconduct\s+(?:hearing|panel|tribunal|case)|malpractice\s+(?:suit|case|claim|lawsuit)|licen[sc]ing\s+board"
    r"|(?:cleared|reprimanded|censured|suspended|sanctioned)\s+(?:by\s+(?:the\s+)?(?:medical|bar|nursing|dental|regulatory)|over\s+(?:failure|allegations?))"
    # a LONE citizen's death ABROAD handled as a consular case (a foreign-ministry "consular assistance" note,
    # a tourist who died on holiday) is human-interest, not world news — a mass-casualty event trips _hard_news.
    r"|consular\s+(?:assistance|case|support|help)|(?:dies?|died|found\s+dead|drowned?)\s+(?:while\s+)?(?:abroad|overseas|on\s+holiday|on\s+vacation|while\s+(?:travell?ing|holidaying|vacationing))"
    # A LONE ACCIDENTAL death — one worker electrocuted, a man drowned, a driver in a road accident — is a
    # local incident, not world news. Needs a SINGLE person-role AND an accidental cause, so a mass toll
    # ("20 die in a road accident") — which has no role word — and a violent death are never caught here.
    r"|(?:worker|labou?rer|man|woman|driver|electrician|miner|farmer|pedestrian|resident|villager|student|"
    r"youth|boy|girl|guard|employee|technician|artisan|trader|conductor|cyclist|motorist|apprentice)\s+"
    r"(?:\w+\s+){0,4}?(?:electrocut\w*|drown\w*|in\s+(?:an?\s+)?(?:road|traffic|car|workplace|construction|"
    r"industrial|electrocution|drowning|boat|ferry|mining)\s+(?:accident|incident|mishap|crash))"
    # HUMAN-INTEREST PROFILE — ONE person's personal journey/struggle is not region-changing news (a
    # "Palestinian American returns to defend his home" story). Narrowly worded so AGGREGATE conflict news
    # ("settlers attack a village", "10 killed in a raid") is NOT caught: it needs the personal 'his/her/their
    # home/family' frame, or an explicit "meet the…"/"one man's story" profile lead-in.
    r"|(?:returns?|returning|travell?ed|travels?|flies|flew|journeys?|heads?\s+back|comes?\s+back|went\s+back)"
    r"\s+(?:\w+\s+){0,5}?to\s+(?:defend|save|rebuild|reclaim|protect|fight\s+for|be\s+with|reunite\s+with)"
    r"\s+(?:his|her|their)\s+(?:home|homes|family|land|village|town|people|community|farm|house)"
    r"|meet\s+the\s+(?:man|woman|family|father|mother|refugee|survivor|teen|boy|girl|grandmother|grandfather|widow)"
    r"|one\s+(?:man|woman|family|father|mother|refugee|survivor|villager|farmer|girl|boy)(?:'s|’s)\s+(?:story|journey|fight|struggle|battle|mission|quest|ordeal|life)"
    r"|a\s+(?:father|mother|widow|refugee|survivor|grandmother|grandfather|daughter|son)(?:'s|’s)\s+(?:story|journey|fight|struggle|battle|ordeal|mission)"
    # NEAR-MISS with a ROAD VEHICLE — "almost hit by a car", "close call with a truck". NOTHING happened (no
    # casualty); a human-interest scare, not a world dot. Deliberately requires a road vehicle so a MILITARY
    # near-miss ("jet narrowly avoided collision", "warships in a close call") — which IS security news —
    # stays on the map. _hard_news runs first too, so any near-miss that caused casualties is kept.
    r"|(?:almost|nearly|narrowly)\s+(?:\w+\s+){0,4}?(?:hit|struck|run\s+over|ran\s+over|knocked\s+(?:down|over)|missed|avoided)\s+(?:by\s+|into\s+)?(?:a\s+|the\s+|an?\s+)?(?:car|truck|lorry|bus|van|vehicle|motorbike|motorcycle|bike|bicycle|scooter|taxi|cab)"
    r"|\bclose\s+call\s+with\s+(?:a\s+|the\s+)?(?:car|truck|lorry|bus|van|vehicle|bike|bicycle|driver|motorist)"
    # EXPLAINER / SERVICE FEATURE — "What X means for Y", "How far should…", "what you need to know",
    # "experts/lawyers weigh in". It poses a question and analyses; it reports NO event. The map is for
    # EVENTS, so these features do not belong (the starred-country feed still keeps them).
    r"|\bwhat\s+[\w'’\s,]{2,50}?\s+means\s+for\b|\bhow\s+far\s+(?:are|should|can|do|will)\b"
    r"|\bwhat\s+you\s+need\s+to\s+know\b|\bhere'?s\s+(?:what|why|how|everything)\b|\bexplain(?:ed|er)\b"
    # RETROSPECTIVE / HISTORY FEATURE — "How a health study helped shape modern medicine", "How the Wall
    # changed a generation". A look-back, not breaking news. Gated after _hard_news (a "how the strike killed
    # 12" report trips that first), and the verb set is retrospective so real events aren't caught.
    r"|\bhow\s+[\w'’\s,-]{3,60}?\s+(?:helped|shaped|reshaped|changed|transformed|revolutionised|revolutionized|paved|inspired|forged|built|became|made\s+possible|gave\s+rise)\b"
    # BLAME-DEFLECTION OPINION — "X was not the one to trigger/blame", "Y should be the one to blame". A
    # diplomat assigning fault reports NO event; it's a talking point, not news. Narrow (needs a blame verb),
    # so a real accountability report ("negligence caused the crash, inquiry finds") is untouched.
    r"|(?:was|were|is|are)\s+not\s+the\s+one[s]?\s+to\s+(?:blame|trigger|start|cause|provoke|escalate|initiate)"
    r"|should\s+be\s+the\s+one[s]?\s+to\s+blame\b"
    r")\b", re.I)


def _soft_news(title, desc):
    """True for consumer-travel gripes, animal-welfare and human-interest fluff — not world-map material."""
    return bool(_SOFT_NEWS.search((title or "") + " " + (desc or "")))


def _map_worthy(title, desc, loc):
    """The IMPORTANCE GATE for the WORLD map (the STARRED-country feed keeps everything). `loc` is the
    _locate tuple (or None). Hide a story that is minor-LOCAL, or a broad analysis with no place to pin —
    UNLESS _hard_news (casualties / a top official on the record) forces it on. The AI's SCOPE + WHERE from
    the summary pass drive it, so it only bites once a story is summarised (brand-new -> shown, then gated)."""
    if _hard_news(title, desc):
        return True
    if _soft_news(title, desc):
        return False                                   # travel/consumer/animal-welfare human-interest
    if _ai_scope(title) == "local":
        return False                                   # a true-but-minor LOCAL story
    # The AI reviewed it (scope set) but named NO single scene (WHERE was NONE -> _ai_where empty), and the
    # rules found nothing specific either (a bare country centroid) — a "state of the world/region" analysis
    # with nowhere to pin (the "Wars, Wildfires and Migrants Leave Europe" piece that kept landing in the
    # Mediterranean / on Iran). Not a map dot.
    if _ai_scope(title) and not _ai_where(title) and (loc is None or _geo_is_weak(loc)):
        return False
    return True


def _src_of(e):
    """One citation for the sources list — the outlet, its link, and when it reported."""
    return {"name": e.get("source") or _domain_name(e.get("domain") or "") or "Source",
            "url": e.get("url") or "", "hrs": e.get("hrs"), "title": e.get("title") or ""}


def _absorb_source(primary, dup):
    """Fold a duplicate report INTO the primary (first-to-report) dot: cite its outlet, fold its text in
    so the AI brief reflects every source, and upgrade the primary's picture/place if it was missing one."""
    srcs = primary.setdefault("sources", [_src_of(primary)])
    for ds in (dup.get("sources") or [_src_of(dup)]):     # carry the dup's whole citation list (chained merges)
        if not any(s.get("url") == ds.get("url") and s.get("name") == ds.get("name") for s in srcs):
            srcs.append(ds)
    extra = _strip_promo(dup.get("sum") or "")
    if extra and extra[:40].lower() not in (primary.get("sum") or "").lower():
        primary["sum"] = _clip(((primary.get("sum") or "") + " " + extra).strip(), 900)
    if not primary.get("image") and dup.get("image"):
        primary["image"] = dup["image"]
    # the dot stays FRESH: its timestamp tracks the most recent update, even though the primary keeps the
    # first reporter's headline. (Individual report times live in each entry of `sources`.)
    if dup.get("hrs") is not None:
        primary["hrs"] = min(primary.get("hrs", dup["hrs"]), dup["hrs"])
    # a SPECIFIC place (Kyiv) beats a country-level one (Ukraine) even if the country dot reported first
    if (dup.get("place") and dup["place"] != dup.get("country") and dup["place"] != _co_short(dup.get("country") or "")
            and (not primary.get("place") or primary["place"] == primary.get("country")
                 or primary["place"] == _co_short(primary.get("country") or ""))):
        primary["lat"], primary["lng"], primary["place"] = dup.get("lat"), dup.get("lng"), dup["place"]


def _cite_source(primary, dup):
    """Lighter than _absorb_source: just CREDIT a duplicate's outlet on the dot it duplicates (and keep the
    dot fresh + pictured), WITHOUT folding its text into the brief. Used by the inline dedup, whose looser
    matches shouldn't muddle the summary — so a story covered by antiwar.com, a wire and a channel is
    credited to all three instead of the later copies vanishing."""
    srcs = primary.setdefault("sources", [_src_of(primary)])
    ds = _src_of(dup)
    if ds["url"] and not any(s.get("url") == ds["url"] for s in srcs):
        srcs.append(ds)
    # PRIMARY = the FIRST to report (largest hrs). The build adds the FRESHEST copy first, so a later-fetched
    # but EARLIER-reported outlet was being cited under the fresher one's byline ("Source: Aa" while
    # 'Middle East Monitor … FIRST' sat in the list). If this copy reported earlier than the one shown,
    # promote its outlet + headline so the story is attributed to whoever broke it.
    _shown = primary.get("_shown_hrs")
    if _shown is None:
        _shown = primary.get("hrs", 0) or 0
    if (dup.get("hrs") or 0) > _shown + 0.05 and dup.get("title") and dup.get("url"):
        # Promote the earlier reporter's WHOLE identity as ONE unit — headline, teaser, link and byline
        # together. SHIPPED BUG: title/url were swapped in but 'sum' was only overwritten "if dup.get('sum')",
        # so an earlier report that carried NO wire description left the EARLIER story's headline sitting above
        # the LATER, DIFFERENT story's body — a "DPRK slams US-ROK drills" headline over a US gasoline-price
        # paragraph. Pair them: the teaser and flags always match the headline now shown (empty teaser is fine —
        # the baked brief, regenerated from THIS url, fills it), never a Frankenstein of two stories.
        primary["title"] = dup["title"]
        primary["url"] = dup["url"]
        primary["sum"] = dup.get("sum") or ""
        primary["summary"] = dup.get("summary") or ""      # drop any stale brief; it belongs to the old headline
        primary["involved"] = _involved_countries(dup["title"], primary.get("country") or "") or primary.get("involved")
        for k in ("source", "domain"):
            if dup.get(k):
                primary[k] = dup[k]
        primary["_shown_hrs"] = dup.get("hrs")
    primary.setdefault("_shown_hrs", _shown)
    if dup.get("hrs") is not None:
        primary["hrs"] = min(primary.get("hrs", dup["hrs"]), dup["hrs"])   # the DOT's timestamp stays freshest
    if not primary.get("image") and dup.get("image"):
        primary["image"] = dup["image"]


def _merge_same_event(events, window_h=18):
    """Fold multiple sources covering the SAME event into ONE dot: the FIRST to report it stays as the
    primary and every other source is cited on it (never dropped). Same-event demands STRONG evidence —
    same country, within the window, and EITHER near-identical wording (a re-headlined wire copy) OR the
    same specific casualty figure at the same spot (the three '9 dead in Kyiv' reports). A mere shared
    place + topic is NOT enough: on a high-volume topic like the Ukraine war, many DIFFERENT events happen
    in Kyiv on one day and would wrongly chain together. The survivor keeps the most specific place and a
    picture; `sources` lists everyone who reported it."""
    if not _WEAK_MATCH:
        _init_weak_match()
    # hrs = HOURS AGO, so the FIRST source to report has the LARGEST hrs: process oldest-first so the
    # first reporter becomes the primary and later sources fold into it.
    events = sorted(events, key=lambda e: -e.get("hrs", 0))
    kept, metas = [], []
    for e in events:
        blob = (e.get("title") or "") + ". " + (e.get("sum") or "")
        toll = _death_toll(blob)
        inj = _injured_toll(blob)
        key = _sigwords(e.get("title") or "") - _GENERIC_WORDS - _WEAK_MATCH
        toks = _norm_tokens(e.get("title") or "")
        pl, co = e.get("place") or "", e.get("country") or ""
        la, ln = e.get("lat"), e.get("lng")
        hit = None
        for i, (mco, mtoll, minj, mkey, mtoks, mpl, mla, mln) in enumerate(metas):
            if mco != co or abs(e.get("hrs", 0) - kept[i].get("hrs", 0)) > window_h:
                continue
            near = (None not in (la, ln, mla, mln)
                    and (la - mla) ** 2 + (ln - mln) ** 2 < 0.6)     # ~<0.77 deg, so Kyiv≈Ukraine-centroid merges
            _pl_match = (bool(pl) and pl == mpl) or near             # the SAME scene (place string, or coords)
            _shared = toks & mtoks
            _shared_content = _shared - _WEAK_MATCH                  # drop the actor country/demonym names
            # On a FUZZY AREA (region centroid) two different towns share the same point + a cloud of strike
            # boilerplate, so a wording match is NOT proof of one event — demand a shared DISTINCT token (the
            # town / target / verb). Check BOTH sides: the kept dot may be the one on the region centroid while
            # THIS one pinned a village ('Ali al-Taher' vs 'Bayout El Siyad', both southern Lebanon). A real
            # city-to-city pair (neither an area) and the hard casualty fingerprint below are never gated.
            _area_ok = (not (_is_area_place(pl) or _is_area_place(mpl))) or bool(_shared_content - _STRIKE_GENERIC)
            same = ((_same_story(toks, mtoks) and _area_ok)          # a re-headlined copy of the same wire
                    or (toll and mtoll and toll == mtoll and near)   # the SAME casualty figure at the SAME spot
                    or (toll and mtoll and toll == mtoll             # ...or BOTH killed AND injured match: a
                        and inj and minj and inj == minj)            # two-number fingerprint, valid anywhere
                    # REWORDED SAME EVENT at the SAME scene: 3+ shared content tokens, 2+ of them NOT mere actor
                    # names. The event nouns ('ballistic', 'missile', 'transaction') that _key strips as generic
                    # survive in _norm_tokens, so the three 'UAE detects Iranian missiles' reports and the two
                    # 'UAE halts trade with Iran' reports — which share no _key word — merge deterministically on
                    # every build. Missile-vs-trade shares only {uae, iran} (2 actors, 0 content) -> stays apart.
                    # ON A FUZZY AREA (a region centroid where two different towns land on the same point), the
                    # shared tokens must include something BEYOND strike boilerplate — else two separate strikes
                    # in 'Southern Lebanon' merge on 'air force airstrike against southern' alone. New area = new
                    # dot, exactly as the reader expects; a real city dot is never gated (`_is_area_place` False).
                    or (_pl_match and len(_shared) >= 3 and len(_shared_content) >= 2 and _area_ok))
            if same:
                hit = i
                break
        if hit is not None:
            _absorb_source(kept[hit], e)
            continue
        e.setdefault("sources", [_src_of(e)])   # keep any citations the inline dedup already added
        kept.append(e)
        metas.append((co, toll, inj, key, toks, pl, la, ln))
    return kept


_WATER_SUFFIX = re.compile(r"\b(sea|ocean|gulf|bay|strait|straits|channel|lagoon|sound|fjord|firth)\b", re.I)


def _is_water_place(place):
    """Is this dot's place a broad body of water (a sea/ocean/gulf/strait) rather than a point? Such a name
    is a big AREA, so several unrelated stories can land on it and must not be treated as 'the same spot'.
    Checks the gazetteer's own water set first, then a suffix fallback ('… Sea', '… Gulf') for any water
    name the set missed. Word-bounded, so a city like 'Swansea' is never mistaken for the open sea."""
    if not place:
        return False
    p = place.split(",")[0].strip().lower()
    if _WATER_NAMES and p in _WATER_NAMES:
        return True
    return bool(_WATER_SUFFIX.search(p))


_AREA_PREFIX = re.compile(r"^(north|south|east|west|northern|southern|eastern|western|central|"
                          r"upper|lower|greater)\b", re.I)


def _is_area_place(place):
    """Is this dot's place a BROAD AREA — a sea, or a directional region ('Southern Lebanon', 'Eastern
    Ukraine', 'Northern Gaza') — rather than a specific point? When the gazetteer can't pin the exact town
    it falls back to the region centroid, so several DIFFERENT strikes land on the very same coordinate and
    read as 'co-located'. On such a place a shared cloud of strike boilerplate is NOT proof of one event
    (see `_merge_same_event`). A bare country centroid is deliberately NOT counted here (national stories
    like 'UAE detects missiles' still merge on their own distinct verb)."""
    if not place:
        return False
    if _is_water_place(place):
        return True
    return bool(_AREA_PREFIX.match(place.split(",")[0].strip()))


# Co-location only means "one situation" for a PHYSICAL event that actually happens AT a place — a strike,
# a bombardment, a disaster. A capital is the SEAT of endless separate political/economic stories, so those
# never collapse on place alone (see `_collapse_colocated`).
_COLLAPSE_CATS = {"security", "climate"}


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
        # A SEA/OCEAN or a directional REGION is a huge area, not one spot: two unrelated stories that both
        # fell back to 'Black Sea' (a resort strike + a 'Black Sea Petroleum' note) or to 'Southern Lebanon'
        # (two strikes on different villages) must NOT collapse into one dot.
        specific = (bool(pl) and pl != co and pl != _co_short(co)
                    and not _is_water_place(pl) and not _is_area_place(pl))
        if specific:
            hit = None
            for ki in buckets.get(pl, []):
                # A CAPITAL/major city is the SEAT of many UNRELATED stories (Washington hosts a trade-deal
                # story, a Brazil-tariff story, and five ambassador-quote clips at once) — co-location there is
                # NOT one situation. Only collapse when the cluster is a PHYSICAL EVENT at the scene (a strike,
                # a disaster): then the terse split-across-categories posts really are one unfolding thing. Pure
                # statement/politics/economy dots at a shared seat stay separate (the real merges run in
                # _merge_same_event, which needs shared CONTENT). SHIPPED BUG: 6 Washington dots -> 1 mega-dot;
                # then a WORSE one — with leader statements now dotting the capital, ONE Kyiv drone-defence dot
                # (security) vacuumed 24 co-located "Zelensky says…" statements into a 25-source mega-dot. So
                # BOTH dots must be physical (a strike + another strike in the same barrage), never a physical
                # event pulling in every co-located statement. Different physical events at a capital scene are
                # still one unfolding situation within the window; statements stay their own dots.
                if (abs(e["hrs"] - kept[ki]["hrs"]) <= window_h
                        and e.get("cat") in _COLLAPSE_CATS and kept[ki].get("cat") in _COLLAPSE_CATS):
                    hit = ki
                    break
            if hit is not None:
                k = kept[hit]
                if _SEVERITY.get(e["cat"], 9) < _SEVERITY.get(k["cat"], 9):
                    if not e.get("image") and k.get("image"):
                        e["image"] = k["image"]           # the survivor should still show a picture
                    e.setdefault("sources", [_src_of(e)])
                    _absorb_source(e, k)                  # e becomes the survivor -> inherit k's citations
                    kept[hit] = e
                else:
                    _absorb_source(k, e)                  # k survives -> cite e on it
                continue
        kept.append(e)
        if specific:
            buckets.setdefault(pl, []).append(len(kept) - 1)
    return kept


_AI_DEDUP_VER = "d5"    # d5: see-through-synonyms prompt (halts trade = suspends commercial activity) + gpt-oss model
# The same-event judgment ('is a Black Sea resort strike the same as a Gelendzhik beach drone attack?') needs
# real reasoning: the fast small model answers NO, a bigger one gets it right. Use the stronger FREE Groq
# model for this one call when the provider is Groq; other providers keep their configured model.
# (Groq retired llama-3.3-70b-versatile; openai/gpt-oss-120b is the current strong reasoning model.)
_DEDUP_MODEL = "openai/gpt-oss-120b"


def _ai_dedup_facet(e):
    """The block of context handed to the LLM for one dot: its place, headline, and the OTHER headlines
    already folded onto it. Those sibling headlines are the bridge that lets the model connect two dots the
    bare primary titles don't — a dot titled '21 hospitalized in Gelendzhik' also carries a source headline
    'drone attack on Russian Black Sea resort', which ties it to the DW 'Black Sea resort' dot. We
    deliberately do NOT include the raw summary: a story's desc is often a stray, off-topic sentence (DW's
    said 'Zelensky put his peace negotiator in charge of intelligence') that misleads a small model into a
    false NO. Headline + place + the sibling wire headlines are the clean signal."""
    title = (e.get("title") or "").strip()
    place = (e.get("place") or e.get("country") or "").strip()
    sib, seen = [], {title.lower()}
    for s in (e.get("sources") or []):
        st = (s.get("title") or "").strip()
        if st and st.lower() not in seen:
            seen.add(st.lower())
            sib.append(st)
        if len(sib) >= 3:
            break
    lines = []
    if place:
        lines.append("Place: " + place)
    lines.append("Headline: " + title)
    if sib:
        lines.append("Also reported as: " + " | ".join(sib))
    return "\n".join(lines)


def _ai_same_event(a, b, cache_only=False):
    """Ask the free LLM whether two dots report the SAME specific incident (same event, day and place — just
    a different outlet or wording), as a strict YES/NO. The model is given each dot's PLACE and the sibling
    headlines already merged onto it, so a town and the sea it sits on, or a 'deaths' report and an
    'injuries' report of one strike, read as one event. Conservative: different events that merely share a
    topic, country or person stay apart. Cached 30 days per unordered title pair. False on error / no LLM.
    cache_only=True returns a PREVIOUSLY-LEARNED verdict with NO live call (None if unknown) — this is what
    lets the COLD-START build apply the merges the last live build already discovered, instantly."""
    ta, tb = (a.get("title") or "").strip(), (b.get("title") or "").strip()
    if not ta or not tb:
        return False
    # Key on the FACETS (place + headline + sibling headlines), not just the titles: the sibling context can
    # change between builds and it drives the verdict, so a stale cache must not pin an out-of-date answer.
    fa, fb = _ai_dedup_facet(a), _ai_dedup_facet(b)
    lo, hi = sorted((fa, fb))
    cache = os.path.join(CACHE_DIR, "dedup_" + hashlib.sha1(
        (_AI_DEDUP_VER + "\n" + lo + "\n" + hi).encode("utf-8")).hexdigest()[:16] + ".json")
    if _fresh(cache, 30 * 86400):
        try:
            return bool(json.load(open(cache, encoding="utf-8")).get("s"))
        except Exception:
            pass
    if cache_only:
        return None                                   # not learned yet; the live build will decide next time
    system = ("You are a precise news-desk editor deduplicating a world-news map. Two items are the SAME when "
              "they report the SAME SPECIFIC INCIDENT — one real event, on the same day, at the same place — "
              "just from different outlets. The same incident is ROUTINELY REWORDED with synonyms and "
              "paraphrase, and you must see through that: 'halts all trade' = 'suspends all commercial "
              "activity' = 'cuts economic ties'; 'detected incoming ballistic missiles' = 'says two ballistic "
              "missiles were fired at it' = 'reports a missile threat'; a 'deaths' report and an 'injuries' "
              "report of ONE strike; a town and the body of water it sits on. Judge the underlying INCIDENT, "
              "not the wording. But do NOT merge genuinely different events that merely share a topic, country "
              "or person: two SEPARATE strikes, two UNRELATED statements or deals, or a policy and a reaction "
              "to it. If it is clearly ONE incident described two ways, answer YES; only when genuinely unsure "
              "whether it is one incident or two, answer NO.")
    user = ("Do these two dots report the SAME specific incident? Answer with ONLY 'YES' or 'NO'.\n\n"
            "ITEM 1\n" + fa + "\n\n"
            "ITEM 2\n" + fb)
    _, _url, _ = _summary_cfg()
    mdl = _DEDUP_MODEL if (_url and "groq" in _url.lower()) else None   # stronger model on Groq; else configured
    out = _llm_complete(system, user, max_tokens=4, temperature=0.0, model=mdl).strip().upper()
    same = out.startswith("YES")
    try:
        json.dump({"s": same}, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return same


def _ai_dedup(events, window_h=30, budget=120, cache_only=False):
    """Semantic duplicate pass — the safety net under the deterministic merges. Some duplicates share NO
    distinctive words and NO casualty fingerprint the code can key on: 'Russia says civilians killed in
    strike on Black Sea resort' (pinned to the sea) and 'Seven killed in drone attack on Gelendzhik' (the
    named town) are one event that word-overlap and geo-proximity both miss. For CANDIDATE pairs only — two
    dots close in time that either share >=2 distinctive words OR sit at specific scenes in the same country
    within a few degrees — ask the free LLM 'same specific incident?' and fold the later report into the
    better-resourced dot. Cheap and safe: candidates are pre-filtered so most feeds ask only a handful, every
    verdict is cached, live calls are capped, and with no LLM the feed is returned untouched."""
    n = len(events)
    if n < 2 or (not cache_only and not _llm_available()):
        return events
    if not _WEAK_MATCH:
        _init_weak_match()
    info = []
    for e in events:
        dist = {w for w in (_sigwords(e.get("title") or "") - _GENERIC_WORDS) if w not in _WEAK_MATCH}
        pl, co = e.get("place") or "", e.get("country") or ""
        specific = bool(pl) and pl != co and pl != _co_short(co)
        info.append((dist, e.get("lat"), e.get("lng"), co, specific, e.get("hrs", 0), e.get("cat") or ""))
    cand = []
    for i in range(n):
        di, lai, lni, coi, spi, hri, cati = info[i]
        for j in range(i + 1, n):
            dj, laj, lnj, coj, spj, hrj, catj = info[j]
            if abs(hri - hrj) > window_h:
                continue
            d2 = ((lai - laj) ** 2 + (lni - lnj) ** 2) if None not in (lai, lni, laj, lnj) else 9e9
            shared = len(di & dj)
            # TWO SPECIFIC SCENES FAR APART ARE DIFFERENT EVENTS — never even pair them for the LLM. SHIPPED
            # BUG: the Komsomolsk-on-Amur refinery strike (far-east Khabarovsk Krai) and the Orsk refinery
            # strike (Orenburg, ~6,000 km away) share {ukrainian, ukraine, refinery}, so `shared>=2` paired
            # them and a small model folded them into one — the far-east dot vanished for a whole day.
            diff_scene = spi and spj and d2 >= 25
            geo = (spi and spj and coi == coj and d2 < 25)          # specific scenes <~5 deg apart, same country
            # SAME COUNTRY + SAME CATEGORY + ONE shared distinctive word — regardless of the specific-vs-centroid
            # PLACE split. A statement/political story lands at the capital in one report and the country centroid
            # in another ("Sacked Ukraine defence minister calls for wartime election" [Kyiv] vs "Fedorov demands a
            # wartime vote" [Ukraine]); requiring the same spot kept them apart. diff_scene still blocks two FAR
            # -APART SPECIFIC scenes, so this only pairs when at least one side isn't a distinct far city.
            samecat = (shared >= 1 and coi == coj and cati == catj)
            # COUNTRY-CENTROID dots (no specific scene) in the SAME country within the window are candidates
            # for the LLM even with NO shared distinctive word. A fully-reworded report of one national event
            # ("UAE detected 2 incoming ballistic missiles from Iran" vs "UAE defense ministry says Iran fired
            # two ballistic missiles at its territory") shares only generic war words, so the token rules never
            # pair them — but they sit on the same country centroid and the LLM can tell they are one incident.
            # Scored LOWEST so the budget is spent on stronger pairs first; the LLM still vetoes different events.
            centroid = (coi == coj and not spi and not spj)
            if not diff_scene and (shared >= 2 or geo or samecat or centroid):
                cand.append((shared + (1 if geo else 0) + (1 if samecat else 0) + (1 if centroid else 0), i, j))
    if not cand:
        return events
    cand.sort(reverse=True)                                          # spend the budget on the strongest pairs first
    parent = list(range(n))

    def find(x):
        r = x
        while parent[r] != r:
            r = parent[r]
        while parent[x] != r:
            parent[x], x = r, parent[x]
        return r

    def quality(e):                                                 # which dot should survive a merge
        return (1 if e.get("image") else 0,
                1 if (e.get("place") and e["place"] != e.get("country")
                      and e["place"] != _co_short(e.get("country") or "")) else 0,
                len(e.get("sources") or [0]),
                e.get("hrs", 0))                                     # ties -> earliest reporter

    removed, calls = set(), 0
    for _s, i, j in cand:
        if not cache_only and calls >= budget:
            break
        ri, rj = find(i), find(j)
        if ri == rj:
            continue
        a, b = events[ri], events[rj]
        if cache_only:
            same = _ai_same_event(a, b, cache_only=True)   # free: a learned verdict, or None if not yet known
            if not same:                                   # None (unknown) or False -> leave apart on cold start
                continue
        else:
            calls += 1
            if not _ai_same_event(a, b):
                continue
        if quality(a) >= quality(b):
            keep, drop, kr, dr = a, b, ri, rj
        else:
            keep, drop, kr, dr = b, a, rj, ri
        keep.setdefault("sources", [_src_of(keep)])
        _absorb_source(keep, drop)
        parent[dr] = kr
        removed.add(dr)
    if not removed:
        return events
    return [e for k, e in enumerate(events) if k not in removed]


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
    # Reuters killed its own public RSS in 2020, so pull its wire via a Google-News site filter. Reuters also
    # arrives through GDELT; the same-event merge folds any overlap into one dot (whichever reported first).
    ("https://news.google.com/rss/search?q=site:reuters.com+when:1d&hl=en-US&gl=US&ceid=US:en", "United Kingdom"),
    # AP — the other gold-standard wire legacy media pay for; no reliable public RSS, so same Google-News path.
    ("https://news.google.com/rss/search?q=site:apnews.com+when:1d&hl=en-US&gl=US&ceid=US:en", "United States of America"),
    # PBS NewsHour — non-profit public broadcast, fact-checked, no clickbait (joins NPR below).
    ("https://www.pbs.org/newshour/feeds/rss/headlines", "United States of America"),
    ("https://news.antiwar.com/feed/", "United States of America"),
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
    # --- added: NYT, more regional powers, broader-perspective + Western populist outlets ---
    ("https://rss.nytimes.com/services/xml/rss/nyt/World.xml", "United States of America"),      # The New York Times
    ("https://rss.nytimes.com/services/xml/rss/nyt/MiddleEast.xml", "United States of America"),
    ("https://www.middleeastmonitor.com/feed/", "United Kingdom"),                                # pro-Palestinian / Muslim world
    ("https://www.tehrantimes.com/rss", "Iran"),                                                  # Iran (regional power)
    ("https://www.cgtn.com/subscribe/rss/section/world.xml", "China"),                            # China state broadcaster
    ("https://www.scmp.com/rss/91/feed", "China"),                                                # South China Morning Post
    ("https://riotimesonline.com/feed/", "Brazil"),                                               # Brazil
    ("https://en.antaranews.com/rss/news.xml", "Indonesia"),                                      # Indonesia (state wire)
    ("https://www.premiumtimesng.com/feed", "Nigeria"),                                           # Nigeria
    ("https://mexiconewsdaily.com/feed/", "Mexico"),                                              # Mexico
    ("https://feeds.feedburner.com/breitbart", "United States of America"),                       # US populist right
    ("https://www.thegatewaypundit.com/feed/", "United States of America"),                       # US far right
    ("https://feeds.feedburner.com/zerohedge/feed", "United States of America"),                  # US contrarian / populist
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
        # Google-News RSS wraps the real outlet in <source url="reuters.com">Reuters</source>. Without this the
        # link is a news.google.com redirect, so the dot was bylined "News" (from news.google.com) instead of
        # "Reuters". Take the outlet name AND its domain from the tag; a direct RSS feed has no <source>, so it
        # falls through to the domain name exactly as before.
        sm = re.search(r"<source[^>]*>(.*?)</source>", b, re.S)
        su = re.search(r'<source[^>]*url="([^"]+)"', b)
        _src_name = _cdata(sm.group(1)).strip() if sm else ""
        out.append({
            "title": title, "url": link.split("?")[0] if "theguardian" not in link else link,
            "hrs": round(_pub_hours(_cdata(pm.group(1)) if pm else ""), 1),
            "socialimage": _upsize_thumb(im.group(1)) if im else "",
            "domain": _domain_of(su.group(1)) if su else _domain_of(link),
            "sourcecountry": home, "desc": desc,
            "_src": _src_name,     # real outlet ("Reuters"); "" on direct feeds -> domain name is used
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
    "moldovan": "Moldova", "serbian": "Serbia", "serb": "Serbia", "serbs": "Serbia", "serbians": "Serbia",
    "croatian": "Croatia",
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
# Words GeoNames lists as MID-SIZE towns (so they slip past the "weak" guards) but which in a headline
# are a generic noun. "University" is essentially NEVER the town University, Florida — even "at the
# University of Tehran" — so it's always vetoed. SHIPPED: "UNIVERSITY courses" -> University, Florida.
_NEVER_CITY_WORDS = {"university", "surprise", "middle east", "schengen",
                     # "Arab" in the news is the demonym ("Arab citizens/world/League"), never Arab, Alabama;
                     # "the village" is the generic phrase "the village of X", never The Village(s), US;
                     # "central" is a direction/region word ("Central and Eastern Europe", "Central Asia"),
                     # never Central, Ontario — the multi-word "Central African Republic" still matches.
                     "arab", "the village", "central",
                     # A FACILITY TYPE is not a town. SHIPPED BUG: "Matecaña International Airport, Pereira's
                     # Airport, is now in shambles" dotted "Airport, United States" (a Honolulu neighbourhood)
                     # instead of Pereira, Colombia — the bare word "airport" outranked the real city. These
                     # are type words, never a standalone place; a real multi-word name ("Airport West",
                     # Melbourne) is a longer gram and still matches, so it is unaffected.
                     "airport", "seaport", "heliport", "airfield", "airbase",
                     # A DIRECTION or a WEATHER PATTERN is not a town. SHIPPED: "Ivory Coast has acquired…drones"
                     # dotted West, TEXAS; "Southern African bloc raises alarm over severe El Nino" dotted El
                     # Niño, MEXICO. "west"/"el nino" are never the scene; "West Bank"/"West Virginia" are longer
                     # grams and still match.
                     "west", "el nino", "el niño",
                     # A SOURCE ABBREVIATION or a PERSON'S NAME is not an obscure foreign town. SHIPPED: an
                     # Israeli-politics poll story ("new ToI poll", ToI = The Times of Israel) dotted Toi, JAPAN;
                     # a Kennedy Center story dotted Kennedy, COLOMBIA (a Bogotá district). "Kennedy Center" is a
                     # longer gram (a _MANUAL_PLACE -> Washington) and still matches; bare "kennedy" never does.
                     "toi", "kennedy",
                     # "Republic" is a word in dozens of country names ("Republic of Türkiye", "Czech
                     # Republic", "Republic of Korea"), never a scene on its own. SHIPPED BUG: a Türkiye
                     # government statement ("The Republic of Türkiye Directorate of Communications…") dotted
                     # Republic, Missouri. The multi-word "Central African Republic"/"Democratic Republic of
                     # Congo" are longer grams and still match; bare "republic" never does.
                     "republic"}
# These ARE real cities (Sparks NV, Brent in London) but usually appear as a verb / market benchmark / an
# ADJECTIVE in a proper-noun phrase — a dot ONLY when the sentence locates something there ("in Sparks").
# SHIPPED: "chipmaker SPARKS fears" -> Sparks, Nevada; "BRENT crude" -> Brent, London; a Trump "'GOLDEN
# Fleet'" battleship program -> Golden, Colorado. "Golden Dome", "Liberty Bell", "Victory Day", "Union
# workers" are the same trap: the word is a modifier, not the town, unless a preposition locates it.
_NOT_CITY_WORDS = {"sparks", "brent", "shaping", "golden", "silver", "liberty", "victory",
                   "union", "enterprise", "sunrise", "sunset", "eagle", "hope", "energy",
                   "fleet", "dome", "shield", "dawn", "sentinel", "guardian",
                   # An OCEAN, the SUN'S CORONA, and a UNIT OF WEIGHT are not the scene unless the sentence
                   # locates something there. SHIPPED: "Putin threatened…Russian vessels" dotted Pacific, MISSOURI;
                   # "solar eclipse could help scientists" dotted Corona, CALIFORNIA; "gold surpasses $4,500 per
                   # troy ounce" dotted Troy, MICHIGAN. "in Corona"/"in Troy" still dots the real town.
                   "pacific", "atlantic", "corona", "troy"}   # program/operation names, oceans, measures
# Month names are DATES, never the scene. A single "May"/"March"/"August" — even after a preposition that
# normally locates ("only two in May", "since March") — is a gazetteer town (May, India; March, England) the
# scan would otherwise dot. SHIPPED BUG: a Kamchatka submarine story dotted "May, India" off "…two in May".
# Only lone month tokens are blocked, so a real multi-word place ("May Pen", Jamaica) still matches first.
_MONTHS = {"january", "february", "march", "april", "may", "june", "july",
           "august", "september", "october", "november", "december"}
# Place names that are ALSO everyday English words — a dot ONLY when Capitalised in the source. "polish"
# (shine), "china" (porcelain), "turkey" (the bird / cold turkey), "guinea" (guinea pig), "chad" (hanging
# chad). "Polish"/"China"/"Turkey" the country still work; "a bit of polish" does not go to Poland.
_CASED_PLACE_WORDS = {"polish", "china", "turkey", "guinea", "chad"}
_MANUAL_PLACES = {   # regions/nicknames GeoNames doesn't list as a city
    # Gulf ENERGY hubs the gazetteer misses but that recur constantly in oil/gas strike news — without these
    # a "fire at Jubail" story drops on the country centroid (Riyadh), miles from the actual coast.
    "jubail": (27.00, 49.66, "Saudi Arabia"), "al jubail": (27.00, 49.66, "Saudi Arabia"),
    "jubail industrial city": (27.02, 49.62, "Saudi Arabia"),
    "yanbu": (24.09, 38.06, "Saudi Arabia"), "ras tanura": (26.64, 50.16, "Saudi Arabia"),
    "abqaiq": (25.93, 49.67, "Saudi Arabia"), "buqayq": (25.93, 49.67, "Saudi Arabia"),
    "khurais": (25.10, 48.10, "Saudi Arabia"), "jazan": (16.89, 42.55, "Saudi Arabia"),
    "ras laffan": (25.90, 51.55, "Qatar"), "ruwais": (24.11, 52.73, "United Arab Emirates"),
    "silicon valley": (37.387, -122.058, "United States of America"),
    "wall street": (40.706, -74.009, "United States of America"),
    "hollywood": (34.098, -118.327, "United States of America"),
    # US landmarks the gazetteer would otherwise send to a same-named foreign town (Kennedy, Colombia).
    "kennedy center": (38.8956, -77.0557, "United States of America"),
    "kennedy centre": (38.8956, -77.0557, "United States of America"),
    "kennedy space center": (28.573, -80.649, "United States of America"),
    "west bank": (31.95, 35.3, "Palestine"),
    "gaza strip": (31.42, 34.35, "Palestine"),
    # Lebanon's regions as the wire writes them — the war zone UNIFIL/Israel report on. Curated here so the
    # exact phrase "south Lebanon" pins southern Lebanon, NOT the village of South Lebanon, Ohio (pop 4,346)
    # that GeoNames offers under the same two words. SHIPPED BUG: dotted Ohio, labelled "South Lebanon, US".
    "south lebanon": (33.36, 35.37, "Lebanon"), "southern lebanon": (33.36, 35.37, "Lebanon"),
    "north lebanon": (34.44, 35.84, "Lebanon"), "northern lebanon": (34.44, 35.84, "Lebanon"),
    "east lebanon": (33.85, 35.90, "Lebanon"), "eastern lebanon": (33.85, 35.90, "Lebanon"),
    # Malaysian Borneo states + a few East/SE-Asia regions GeoNames doesn't index as a city
    "sabah": (5.98, 116.07, "Malaysia"), "sarawak": (1.55, 110.36, "Malaysia"),
    "labuan": (5.28, 115.24, "Malaysia"), "peninsular malaysia": (3.99, 102.14, "Malaysia"),
    "west papua": (-2.55, 133.74, "Indonesia"), "papua": (-4.27, 138.08, "Indonesia"),
    "aceh": (4.70, 96.75, "Indonesia"), "mindanao": (7.87, 124.95, "Philippines"),
    "donbas": (48.5, 37.8, "Ukraine"),
    "crimea": (45.3, 34.4, "Ukraine"),
    # Far-east Russian refinery cities/regions the wire names in Ukrainian long-range drone-strike news but
    # GeoNames' English index misses under this transliteration. SHIPPED BUG: "Komsomolsk-on-Amur refinery in
    # Khabarovsk Krai" mis-dotted — the bare word "Amur" matched Amur, INDIA, and nothing pinned the real
    # city. Absorbing the WHOLE hyphenated name as one gram also kills the false "Amur" match. (Hyphens
    # tokenise to spaces, so the key is spaced.)
    "komsomolsk on amur": (50.55, 137.01, "Russia"), "komsomolsk na amure": (50.55, 137.01, "Russia"),
    "khabarovsk krai": (48.48, 135.08, "Russia"), "khabarovsk region": (48.48, 135.08, "Russia"),
}
# The strategic straits/seas/canals live in _WATERS (below), NOT here: an international water must register
# at _FACILITY_PRIOR so it is NEVER NER-vetoed (spaCy tags "Hormuz"/"Bosphorus" as a PERSON and a 5M-prior
# region entry got deleted) and so its arbitrary "country" is overridden by the story's own context.
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
    # Zaporizhzhia-front villages the wire captures/loses daily, by the spellings it prints (RU "Malaya"
    # vs UA "Mala") — without these "Malaya Tokmachka" matched Malaya, PHILIPPINES and a Ukraine war story
    # dotted the wrong continent.
    "malaya tokmachka": (47.534, 35.901, "Ukraine"), "mala tokmachka": (47.534, 35.901, "Ukraine"),
    "orekhov": (47.567, 35.786, "Ukraine"), "orikhiv": (47.567, 35.786, "Ukraine"),
    "tokmak": (47.253, 35.708, "Ukraine"), "hulyaipole": (47.66, 36.25, "Ukraine"), "gulyaipole": (47.66, 36.25, "Ukraine"),
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
    "zaporizhzhia npp": (47.512, 34.586, "Ukraine"),
    # the Russian spelling RT/TASS print — the plant itself, not the city 50 km away
    "zaporozhye npp": (47.512, 34.586, "Ukraine"), "zaporozhye nuclear power plant": (47.512, 34.586, "Ukraine"),
    "kakhovka dam": (46.778, 33.369, "Ukraine"),
    "chernobyl": (51.389, 30.099, "Ukraine"), "crimean bridge": (45.311, 36.520, "Ukraine"),
    "kerch bridge": (45.311, 36.520, "Ukraine"), "saky air base": (45.093, 33.599, "Ukraine"),
    # Middle East
    "shuwaikh port": (29.350, 47.930, "Kuwait"), "al udeid air base": (25.117, 51.315, "Qatar"),
    "ain al asad": (33.785, 42.441, "Iraq"), "muwaffaq salti air base": (32.356, 36.259, "Jordan"),
    "natanz": (33.724, 51.727, "Iran"), "fordow": (34.885, 50.993, "Iran"),
    # "Pickaxe Mountain" (Kuh-e Kolang Gaz La) — the deep tunnel enrichment complex just south of Natanz.
    "pickaxe mountain": (33.647, 51.729, "Iran"), "kuh-e kolang gaz la": (33.647, 51.729, "Iran"),
    "kolang gaz la": (33.647, 51.729, "Iran"),
    "kharg island": (29.231, 50.324, "Iran"), "bandar abbas": (27.183, 56.277, "Iran"),
    "ras tanura": (26.644, 50.158, "Saudi Arabia"), "abqaiq": (25.934, 49.671, "Saudi Arabia"),
    "haifa port": (32.826, 35.001, "Israel"), "dimona": (31.070, 35.033, "Israel"),
    "ben gurion airport": (32.011, 34.887, "Israel"), "ben gurion": (32.011, 34.887, "Israel"),
    # seats of power are PLACES. "Trump welcomes the Iraqi PM to the WHITE HOUSE" was a dot on IRAQ.
    "white house": (38.898, -77.037, "United States of America"),
    "the kremlin": (55.752, 37.617, "Russia"),
    # UK — Sizewell nuclear complex sits on the Suffolk coast; "Suffolk" alone otherwise dots Suffolk, Virginia
    "sizewell": (52.215, 1.620, "United Kingdom"), "sizewell nuclear plant": (52.215, 1.620, "United Kingdom"),
    "sizewell c": (52.215, 1.620, "United Kingdom"), "sizewell b": (52.215, 1.620, "United Kingdom"),
    # Lebanon — Beaufort Castle (Arnoun, southern Lebanon); the bare name "Beaufort" otherwise dots Malaysia
    "beaufort castle": (33.204, 35.539, "Lebanon"),
    # Lebanon — Ali al-Taher ridge/heights by Nabatieh, a recurring flashpoint in Israel-Hezbollah clashes;
    # the bare name is in no city gazetteer, so a post naming only "Ali Taher Ridge" fell back to the actor
    "ali taher": (33.352, 35.520, "Lebanon"), "ali al taher": (33.352, 35.520, "Lebanon"),
    "ali taher ridge": (33.352, 35.520, "Lebanon"), "ali taher heights": (33.352, 35.520, "Lebanon"),
    # US — MCAS Miramar is in San Diego; the bare name "Miramar" otherwise dots Miramar, Florida (near Miami)
    "miramar air base": (32.868, -117.142, "United States of America"),
    "mcas miramar": (32.868, -117.142, "United States of America"),
    "miramar air station": (32.868, -117.142, "United States of America"),
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
    # SHIPPED: "Saudi Aramco refinery in JAZAN" dotted a tiny Iranian village 'Jazan' (pop 1,818) because
    # the major Saudi city is in GeoNames only under the 'Jizan' spelling. Jazan/Jizan is one place: the
    # Red-Sea port and Aramco refinery in SW Saudi Arabia.
    "jazan": (16.889, 42.551, "Saudi Arabia"), "jizan": (16.889, 42.551, "Saudi Arabia"),
    # SHIPPED: a Turkish overflight of the Greek islet FARMAKONISI dotted "The Village, US"; the "Hays"
    # front of the Yemen war dotted Hays, Kansas. Both are absent from the city gazetteer, so a US namesake
    # (or the generic "The Village") won.
    "farmakonisi": (37.287, 27.081, "Greece"),
    "hays": (13.848, 43.483, "Yemen"), "al hays": (13.848, 43.483, "Yemen"),
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
    # (No continental rivers like the Danube: a 10-country river is a LINE, so a single mention would
    #  hijack a national story onto an arbitrary point. Dnieper/Euphrates/Tigris earn a point only because
    #  each is an active war front where the river itself is the scene.)
    # STRATEGIC CHOKEPOINTS — the straits/canals/seas that show up in shipping-and-strike news. Curated at
    # facility prior so NER never vetoes them (spaCy reads "Hormuz"/"Bosphorus" as a person) and their
    # bookkeeping country is overridden by whatever the story actually names.
    "strait of hormuz": (26.57, 56.25, "Iran"), "hormuz strait": (26.57, 56.25, "Iran"),
    "hormuz": (26.57, 56.25, "Iran"),
    "bab el mandeb": (12.58, 43.33, "Yemen"), "bab al mandab": (12.58, 43.33, "Yemen"),
    "strait of gibraltar": (35.95, -5.60, "Spain"), "gibraltar strait": (35.95, -5.60, "Spain"),
    "strait of malacca": (2.50, 101.00, "Malaysia"), "malacca strait": (2.50, 101.00, "Malaysia"),
    "bosphorus": (41.12, 29.07, "Turkey"), "dardanelles": (40.22, 26.40, "Turkey"),
    "taiwan strait": (24.50, 119.50, "Taiwan"), "english channel": (50.30, 0.30, "France"),
    "suez canal": (30.42, 32.35, "Egypt"), "panama canal": (9.08, -79.68, "Panama"),
    "red sea": (20.00, 38.50, "Saudi Arabia"), "south china sea": (13.00, 114.00, "Philippines"),
    "persian gulf": (26.50, 51.50, "Iran"), "gulf of aden": (12.50, 47.50, "Yemen"),
    # oceans — a last-resort scene when nothing more specific is knowable (a mid-ocean incident)
    "pacific ocean": (0.00, -155.00, "United States of America"),
    "atlantic ocean": (25.00, -40.00, "United States of America"),
    "indian ocean": (-20.00, 80.00, "India"), "arctic ocean": (85.00, 0.00, "Russia"),
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


# The largest REAL city per country — the "no photo" fallback shows a picture of the country's main city
# (Kyiv, Jeddah, Tehran), never its flag or a locator map. Built from the gazetteer, excluding the curated
# overlays (facilities/regions/famous/war-towns carry round synthetic priors), so it's the biggest actual
# GeoNames city. One cheap pass at import.
_LARGEST_CITY = {}
# Where the largest city is a photoless industrial suburb (Kuwait's Al Ahmadi) or Wikipedia disambiguates
# the bare name (Libya's "Tripoli"), pin a major, well-photographed city instead. Extend as spotted.
_MAIN_CITY_OVERRIDE = {"Kuwait": "kuwait city", "Libya": "benghazi"}

# CITY-STATES & MICROSTATES have no separate city article — their Wikipedia page IS the country, whose lead
# image is a flag/crest/locator map (all rejected), so place_photo returned NOTHING and the hero shipped
# BLACK (Singapore, Hong Kong did exactly this). Map each to a well-photographed LANDMARK/district article
# that has a real skyline photo, tried FIRST. Keyed by the lowercased place/city name place_photo receives.
_PLACE_PHOTO_QUERY = {
    "singapore": "Downtown Core", "hong kong": "Hong Kong Island", "macau": "Macau Peninsula",
    "macao": "Macau Peninsula", "monaco": "Monte Carlo", "vatican city": "St. Peter's Square",
    "vatican": "St. Peter's Square", "san marino": "City of San Marino", "andorra": "Andorra la Vella",
    "liechtenstein": "Vaduz", "gibraltar": "Rock of Gibraltar", "luxembourg": "Luxembourg City",
    "bahrain": "Manama", "qatar": "Doha", "brunei": "Bandar Seri Begawan", "maldives": "Malé",
    "malta": "Valletta", "bahamas": "Nassau, Bahamas", "barbados": "Bridgetown",
}


def _build_largest_city():
    synth = {900_000, 3_000_000, _REGION_PRIOR, _FACILITY_PRIOR}
    best = {}
    for name, cands in CITY_CANDS.items():
        for (clat, clng, c, prior) in cands:
            if prior in synth:
                continue
            if c not in best or prior > best[c][1]:
                best[c] = (name, prior)
    for c, (name, _p) in best.items():
        _LARGEST_CITY[c] = name
    _LARGEST_CITY.update(_MAIN_CITY_OVERRIDE)


_build_largest_city()


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


_GEO_PREP = {"in", "at", "near", "across", "outside", "throughout", "around", "amid", "inside", "over", "above",
             # TRANSIT prepositions: an event that moves "through"/"via" a place happens THERE — a shipment
             # "through the Caspian Sea" is on the Caspian, not at the sender/receiver. SHIPPED BUG: "Russia
             # shipping to Iran through the Caspian Sea" dotted Tehran (and earlier Washington), not the sea.
             "through", "via"}
# "the <these> OF X" declares X a place — enough locational context to accept a small (weak) town.
_PLACE_OF_NOUNS = {"town", "village", "city", "port", "district", "province", "region", "outskirts",
                   "suburb", "suburbs", "municipality", "borough", "county", "settlement", "hamlet",
                   "commune", "capital", "prefecture", "township", "enclave", "exclave"}
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
    "biden": "United States of America", "bessent": "United States of America",
    "scott bessent": "United States of America", "treasury secretary": "United States of America",
    "bondi": "United States of America", "noem": "United States of America",
    "waltz": "United States of America", "witkoff": "United States of America",
    "putin": "Russia", "lavrov": "Russia", "medvedev": "Russia", "peskov": "Russia",
    "zakharova": "Russia", "shoigu": "Russia", "mishustin": "Russia",
    "zelensky": "Ukraine", "zelenskyy": "Ukraine",
    "netanyahu": "Israel", "khamenei": "Iran", "pezeshkian": "Iran", "araghchi": "Iran",
    "xi jinping": "China", "macron": "France", "starmer": "United Kingdom",
    "merz": "Germany", "scholz": "Germany", "meloni": "Italy",
    "erdogan": "Turkey", "modi": "India", "kim jong un": "North Korea",
    "milei": "Argentina", "lula": "Brazil", "orban": "Hungary", "vucic": "Serbia",
    # The EU's own leaders sit in BRUSSELS (mapped to Belgium, as the EU institutions are) — a statement by
    # the Commission/Council President is EU news, not news of whatever country they are talking ABOUT.
    "von der leyen": "Belgium", "ursula von der leyen": "Belgium", "leyen": "Belgium",
    "kaja kallas": "Belgium", "kallas": "Belgium", "antonio costa": "Belgium",
    "eu commission": "Belgium", "european commission president": "Belgium",
    # US institutions acting/announcing are news at their own seat (Washington), not the foreign topic:
    # "PENTAGON lowers Iran war death toll", "WHITE HOUSE weighs strike".
    "pentagon": "United States of America", "white house": "United States of America",
    "centcom": "United States of America", "state department": "United States of America",
    "kremlin": "Russia",
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
              "promises", "promised", "insists", "repeats", "reiterates", "reiterated",
              "thanks", "thanked", "thank", "welcomes", "welcomed", "congratulates", "congratulated"}


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


def _wiki_define(name):
    """A FREE, non-LLM one-line definition of a named org from Wikipedia's REST summary — the baseline so a
    named group is defined even when the LLM's daily budget is spent (and so definitions stop competing with
    summaries for it). Prefers the terse Wikidata short-description (a factual LABEL, not copyrightable, e.g.
    'British Sunni Muslim organisation'); falls back to the extract's first sentence. Returns '' for a missing
    page or a disambiguation. Cached by the caller."""
    try:
        j = _wiki_json("https://en.wikipedia.org/api/rest_v1/page/summary/"
                       + urllib.parse.quote((name or "").replace(" ", "_")))
        if not j or j.get("type") == "disambiguation":
            return ""
        title = (j.get("title") or name or "").strip()
        desc = (j.get("description") or "").strip()
        if desc and 4 <= len(desc) <= 100 and not desc.lower().startswith(("wikipedia", "wikimedia", "disambig")):
            d = title + " — " + desc                              # "Muslim Association of Britain — British Sunni Muslim organisation"
            return d if d.endswith((".", "!", "?")) else d + "."
        extract = (j.get("extract") or "").strip()
        m = re.match(r"^[\s\S]*?[.!?](?=\s|$)", extract)          # the extract's first whole sentence
        s = (m.group(0) if m else extract).strip()
        if 20 <= len(s) <= 240 and (" is " in s or " was " in s or " are " in s or " refers to " in s):
            return s
        return ""
    except Exception:
        return ""


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

# The US Federal Reserve. "Federal Reserve"/"the Fed" are plain _ORG_COUNTRY keys, but the bare
# clipped form "Fed" (as wires headline it: "Fed officials signal rate hike", Anadolu-sourced ->
# dotted TURKEY because the URL section was Turkish and no org matched) is a landmine — "fed up",
# "well-fed", "fed the troops" are not the central bank. So the bare token only counts as the Fed
# when a monetary-policy context word rides alongside it (or FOMC, which is unambiguous on its own).
_FED_CTX = re.compile(r" (rate|rates|hike|hikes|cut|cuts|powell|fomc|monetary|policymaker"
                      r"|policymakers|taper|tapering|jerome|chair|chairman|reserve) ")


def _is_fed_org(low):
    # `low` is the space-padded, alpha-only fold built in _org_country.
    if " fomc " in low or " federal open market committee " in low:
        return True
    return " fed " in low and _FED_CTX.search(low) is not None


def _org_country(title):
    low = " " + re.sub(r"[^a-z ]", " ", _fold(title or "").lower()) + " "
    for k in _ORG_KEYS:
        if (" " + k + " ") in low:
            return _ORG_COUNTRY[k]
    if _is_fed_org(low):
        return "United States of America"
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
                    if hit.get("url"):                       # self-heal a full-res URL cached before we thumbed
                        hit["url"] = _wiki_thumb(hit["url"], 1280)
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
                    out = {"url": _wiki_thumb(img, 1280), "title": wtitle}
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


_LEADER_ROLE = (r"(prime[\s-]?minister|premier|president|chancellor|foreign\s+minister|"
                r"supreme\s+leader|crown\s+prince|defen[cs]e\s+minister|monarch)")
_LEADER_POSS = re.compile(r"\b([A-Z][A-Za-z.\-]+)['’]s\s+(?:new\s+|acting\s+|interim\s+)?" + _LEADER_ROLE, re.I)
_LEADER_DEM  = re.compile(r"\b([A-Z][a-z]+)\s+(?:new\s+|acting\s+|interim\s+)?" + _LEADER_ROLE, re.I)
_LEADER_OF   = re.compile(_LEADER_ROLE + r"\s+of\s+(?:the\s+)?([A-Z][A-Za-z]+(?:\s[A-Z][a-z]+){0,2})", re.I)
_LEADER_NAME_VER = "l1"   # separate cache version for the legacy _leader_name path — must NOT be named
                          # _LEADER_VER, or (defined later in the file) it silently overrode the real one above.


def _leader_name(country, role):
    """The current holder of a national office, named by the LLM and CACHED. Returns '' when unsure — and the
    caller runs the name through _person_card, which only yields a face for a REAL, verifiable office-holder,
    so a wrong guess simply shows nothing rather than a wrong person."""
    country = (country or "").strip()
    role = (role or "").strip().lower()
    if not country or not role or country not in COUNTRY_COORDS or not _llm_available():
        return ""
    cache = os.path.join(CACHE_DIR, "leader_" + hashlib.sha1((_LEADER_NAME_VER + "|" + country.lower() + "|" + role).encode("utf-8")).hexdigest()[:16] + ".json")
    if _fresh(cache, 7 * 86400):        # leaders change — a shorter TTL than most caches
        try:
            return json.load(open(cache, encoding="utf-8")).get("n", "")
        except Exception:
            pass
    role_q = {"premier": "prime minister", "pm": "prime minister"}.get(role, role)
    system = ("You name current national office-holders. Reply with ONLY the person's full name and nothing "
              "else. If you are not certain, or the office is vacant, reply exactly NONE.")
    prompt = "Who currently holds the office of " + role_q + " of " + country + "? Full name only, or NONE."
    out = re.sub(r"\s+", " ", (_llm_complete(system, prompt, max_tokens=24, temperature=0) or "").strip()).strip(".")
    if len(out) < 3 or out.upper().startswith("NONE") or len(out.split()) > 5 or any(c.isdigit() for c in out):
        out = ""
    try:
        json.dump({"n": out}, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


def _leader_from_title(title, desc=""):
    """A story that names an OFFICE but not the person ('Iraq's premier says…') still has a face. Detect the
    office + country and resolve who it is, so the reader sees WHO is speaking. One face max."""
    text = (title or "") + ". " + (desc or "")[:200]
    found = None
    m = _LEADER_POSS.search(text)
    if m:
        found = (m.group(1), m.group(2))                        # "Iraq's premier" -> (Iraq, premier)
    if not found:
        m = _LEADER_OF.search(text)
        if m:
            found = (m.group(2), m.group(1))                    # "premier of Iraq"
    if not found:
        m = _LEADER_DEM.search(text)
        if m and m.group(1).lower() in DEMONYMS:
            found = (DEMONYMS[m.group(1).lower()], m.group(2))  # "Iraqi premier" -> (Iraq, premier)
    if not found:
        return []
    country = _leader_country(found[0])   # case-insensitive regex over-captures ("France meets allies") -> salvage the real country
    if not country:
        return []
    name = _leader_name(country, found[1])
    return [(name, False)] if name else []


def _leader_country(s):
    """Pull a real country name out of a (possibly over-captured) string: try its leading 1-3 words against
    the gazetteer, country aliases, and demonyms."""
    parts = re.findall(r"[A-Za-z'’.\-]+", s or "")
    for n in (3, 2, 1):
        cand = " ".join(parts[:n]).strip()
        if not cand:
            continue
        if cand in COUNTRY_COORDS:
            return cand
        if cand.title() in COUNTRY_COORDS:
            return cand.title()
        low = cand.lower()
        if low in DEMONYMS:
            return DEMONYMS[low]
        if low in COUNTRY_ALIASES and COUNTRY_ALIASES[low] in COUNTRY_COORDS:
            return COUNTRY_ALIASES[low]
    return ""


def _story_people(title, desc=""):
    picks = _name_candidates(title, desc)
    if not picks:
        picks = _leader_from_title(title, desc)   # no NAME in the story -> resolve the office-holder, verified below
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
                # An EXPLICIT locational cue ("attacks ON Zaporozhye NPP", "strike IN Kabul") outranks a NER
                # PERSON guess even when the span is multi-word: NER routinely swallows a mislabelled FACILITY
                # ("Zaporozhye NPP", "Afipsky Refinery") into a two-token PERSON entity, and the old covers_more
                # veto deleted the located place anyway — a Zaporozhye-NPP attack then dotted "Kiev regime"
                # instead. A geo-preposition in front is far harder evidence than the surname guess.
                if covers_more and not located:
                    return True          # a full name ("Lindsey Graham") — certainly a person
                if not supported and not located:
                    return True          # a lone capitalised name with nothing backing it up
            elif lab == "ORG" and weak and not supported and not located:
                return True
    return False


_COMPASS_WORDS = {"north", "south", "east", "west", "northern", "southern", "eastern", "western",
                  "central", "upper", "lower", "northeast", "northwest", "southeast", "southwest"}
_COUNTRY_WORD_MAP = None


def _country_word_map():
    """Lowercase SINGLE-word country names -> canonical COUNTRY_COORDS key, MINUS names that are also common
    US places or ordinary words (georgia the state, jordan/chad/guinea as US towns/forenames, turkey/china as
    words). Used by the compass+country guard so it converts 'South Lebanon' -> Lebanon but never trips on a
    genuine US story."""
    global _COUNTRY_WORD_MAP
    if _COUNTRY_WORD_MAP is None:
        ambiguous = {"georgia", "jordan", "chad", "guinea", "turkey", "china", "niger", "mali"}
        _COUNTRY_WORD_MAP = {co.lower(): co for co in COUNTRY_COORDS
                             if " " not in co and co.lower() not in ambiguous}
    return _COUNTRY_WORD_MAP


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
                           "signs", "signed", "vetoes", "vetoed",
                           # DELIBERATION/decision-making by a leader is news at THEIR seat, not the foreign
                           # topic: "Trump CONSIDERS attack on Iran", "Trump MET with advisers to decide
                           # operations against Iran" -> Washington, not Tehran. (Going-verbs like visit/
                           # travel stay excluded — there the named place is the destination.)
                           "considers", "considering", "consider", "weighs", "weighed", "weighing",
                           "mulls", "mulled", "mulling", "met", "meets", "meeting", "plans", "planning",
                           "decides", "deciding", "huddles", "huddled", "convenes", "convened",
                           # an institution's ANNOUNCEMENT happens at its seat: "Pentagon LOWERS the toll"
                           "lowers", "lowered", "raises", "raised", "reports", "reported", "revises",
                           "revised", "releases", "released", "publishes", "published", "estimates"}


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


# A leader who is only CONSIDERING / THREATENING / WEIGHING action on a foreign country. Nothing has
# happened there — the story is the deliberation, at the leader's own seat — even though a word like
# "attack"/"strike" makes the target read as "located". "Trump CONSIDERS attack on Iran" is Washington.
_DELIBERATE_VERBS = {"considers", "considering", "consider", "weighs", "weighing", "weigh", "mulls",
                     "mulling", "mull", "plans", "planning", "plan", "threatens", "threatened", "threaten",
                     "vows", "vowed", "vow", "warns", "warned", "warn", "eyes", "eyeing", "readies",
                     "readying", "prepares", "preparing", "ponders", "pondering", "weighs", "wants"}


# A COUNTRY taking a DOMESTIC / administrative action happens AT ITS OWN SEAT — the action is the country's
# own governmental act, not something at a foreign place. Deliberately excludes strike/attack/invade verbs
# (those happen at the TARGET): "Russia strikes Ukraine" stays Ukraine, but "France ORDERS the expulsion…",
# "US SANCTIONS…", "Germany SUMMONS the ambassador" are the acting country's own news.
_ACTOR_SEAT_VERBS = {
    "orders", "order", "ordered", "expels", "expel", "expelled", "bans", "ban", "banned",
    "sanctions", "sanction", "sanctioned", "summons", "summon", "summoned", "recalls", "recall", "recalled",
    "imposes", "impose", "imposed", "announces", "announce", "announced", "declares", "declare", "declared",
    "approves", "approve", "approved", "passes", "pass", "passed", "unveils", "unveil", "unveiled",
    "introduces", "introduce", "introduced", "suspends", "suspend", "suspended", "deports", "deport", "deported",
    "indicts", "indict", "indicted", "outlaws", "outlaw", "outlawed", "designates", "designate", "designated",
    # A WEAPONS TEST happens at the TESTING country (it has no foreign target). SHIPPED: "North Korea tests
    # missile ahead of US-South Korea drills" dotted the United States (the higher-profile country the headline
    # merely names). Strike/attack/launch-on verbs stay OUT (those hit a target); a bare test does not.
    "tests", "test", "tested", "test-fires", "test-fired"}


def _actor_seat_country(hits, words):
    """A COUNTRY that is the SUBJECT at the very start of the headline, taking a domestic/administrative
    action (orders/expels/bans/sanctions/summons/announces…), is acting AT ITS OWN SEAT. Returns that
    country so one named only as BACKGROUND later ("…the start of the Russian Intervention in Ukraine")
    can't steal the dot. Narrow by design: the subject must be at the start and the verb must be domestic."""
    for h in hits:
        if h[1] not in ("country", "demonym") or h[0] > 2:      # must be the subject, at the very start
            continue
        for k in range(h[0] + 1, min(h[0] + 4, len(words))):
            if words[k] in _ACTOR_SEAT_VERBS:
                return h[5]                                      # the acting country
    return None


def _deliberation_country(words):
    """A leader/official who is the SUBJECT at the start and DELIBERATES or makes a STATEMENT about a
    foreign country -> THEIR seat. "Trump considers attack on Iran", "Rubio says ... war in Ukraine",
    "Trump vows tariffs on EU" are all Washington stories — the foreign country is the topic. Going-verbs
    (visit/arrive/land) are absent from these sets, so "Trump in Tehran" stays Tehran; and the caller only
    overrides a foreign COUNTRY, never a specific CITY scene ("Zelensky says forces struck Pokrovsk")."""
    n = len(words)
    for j in range(0, min(4, n)):
        for size in (2, 1):
            if j + size > n:
                continue
            co = _OFFICIAL_COUNTRY.get(" ".join(words[j:j + size]))
            if co:
                for k in range(j + size, min(j + size + 3, n)):
                    if words[k] in _ACT_VERBS or words[k] in _DELIBERATE_VERBS:
                        return co
    return None


# A leader making an ON-THE-RECORD statement, marked by a COLON — "Trump: We'll act", "Trump to Axios: We
# are low-keying it with Iran", "Rubio to Fox: ..." — is news at THEIR seat; the foreign country in the
# quote is only the topic. This is the interview/quote shape the say-verb sets miss: there is no verb, just
# the colon. Requires the leader at the very START, so a quoted foreign country ("...says: Iran will pay")
# can't trigger it. The name run is bounded (1-4 words) and an optional "to <Outlet>" may sit before the colon.
_ONREC_STMT = re.compile(r"^\s*([A-Za-z][\w.'’-]+(?:\s+[A-Za-z][\w.'’-]+){0,3}?)(?:\s+to\s+[A-Za-z][\w.'’&-]+)?\s*:")


def _onrecord_statement_country(title):
    """A national leader named at the very start of a headline, then a colon (optionally 'to <Outlet>:'), is
    making an attributed statement — news at their OWN seat, not the foreign country the quote is about.
    'Trump to Axios: ...with Iran' -> United States. Officials (people) only, so a country label before a
    colon ('Iran: ...') never fires here. Returns their country, or None."""
    m = _ONREC_STMT.match(title or "")
    if not m:
        return None
    toks = m.group(1).lower().split()
    for size in range(len(toks), 0, -1):          # longest leading name run first, then the surname alone
        co = _OFFICIAL_COUNTRY.get(" ".join(toks[:size]))
        if co:
            return co
    if toks:
        return _OFFICIAL_COUNTRY.get(toks[-1])
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
               "invaded", "besiege", "besieged", "reach", "reaches", "reached", "batter", "batters",
               # "US POUNDS Iranian city" was dotting the US: the bombing verb wasn't recognised, so the
               # Iranian target never became the scene. (cf. "US STRIKES Iranian…" which already worked.)
               "pound", "pounds", "pounded", "pounding", "hammer", "hammers", "hammered", "pummel",
               "pummels", "pummelled", "pummeled", "bombard", "bombards", "bombarded", "blitz",
               "blitzes", "blitzed", "pounds,"}

# Pretty labels for names whose tokenised (apostrophe-as-space) form would title-case to nonsense —
# "sana a" -> "Sana A". The apostrophe belongs back in the DISPLAY name, never in the match key.
_DISPLAY_NAMES = {
    "sana a": "Sana'a", "sanaa": "Sana'a", "ta izz": "Ta'izz", "taizz": "Ta'izz",
    "ma rib": "Ma'rib", "marib": "Ma'rib", "sa dah": "Saada",
    "sana a international airport": "Sana'a International Airport",
    "sanaa international airport": "Sana'a International Airport",
    "zaporizhzhia npp": "Zaporizhzhia NPP", "zaporizhzhia nuclear power plant": "Zaporizhzhia NPP",
    "zaporozhye npp": "Zaporizhzhia NPP", "zaporozhye nuclear power plant": "Zaporizhzhia NPP",
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
    # "St Petersburg" / "St. Louis" — the gazetteer stores the full "Saint" form, so an unnormalised
    # "St X" fell through to a tiny same-named US town (Petersburg, Virginia). Normalise the abbreviation.
    if (gram[:3] == "st " or gram[:4] == "st. ") and ("saint " + gram.split(" ", 1)[1]) in CITY_CANDS:
        gram = "saint " + gram.split(" ", 1)[1]
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
        # A COUNTRY must not be demoted to a same-named MINOR town just because that town's country is in
        # context: "southern Lebanon" in a story that also says "US" is the country, not Lebanon, Tennessee
        # (pop 30k). A prominent same-named region still stands (Georgia the US STATE, pop 5M, is correctly
        # the state when the US is the subject) — hence the <500k floor on the town.
        if (best_ctx[0] == "city" and best_ctx[5] < 500000
                and best_all[0] in ("country", "demonym") and best_all[5] >= 20 * max(best_ctx[5], 1)):
            return best_all + (False,)
        return best_ctx + (True,)
    cands.sort(key=lambda c: -c[5])          # no context: strongest prior (country > state > big city)
    return cands[0] + (False,)


_GAZ_STARTS = None


def _gaz_starts():
    """First token of every gazetteer key (countries, demonyms, cities). A word that starts no key can
    start no matching n-gram, so the scanner skips it. Built once, lazily, after the gazetteers load."""
    global _GAZ_STARTS
    if _GAZ_STARTS is None:
        s = set()
        for d in (COUNTRY_ALIASES, DEMONYMS, CITY_CANDS):
            for k in d:
                sp = k.find(" ")
                s.add(k if sp < 0 else k[:sp])
        _GAZ_STARTS = s
    return _GAZ_STARTS


def _scan_places(text, spans, mentions):
    """Gazetteer n-gram scan (longest match first), NER veto on city hits, context-aware resolution."""
    toks = [(mm.group(0), mm.start(), mm.end()) for mm in re.finditer(r"[A-Za-z0-9]+", _fold(text or ""))]
    words = [t[0].lower() for t in toks]
    orig = [t[0] for t in toks]
    n = len(words)
    hits, i = [], 0
    starts = _gaz_starts()
    while i < n:
        # FAST SKIP: no gram starting at words[i] can resolve unless words[i] begins some gazetteer key
        # (or is the plural of a demonym, e.g. "indians"->"indian"). Skipping the ~80% of tokens that are
        # ordinary words ("the", "said", "attack") turns the n-gram scan from 14 ms/article to a fraction.
        w = words[i]
        if w not in starts and not (len(w) > 4 and w.endswith("s") and w[:-1] in starts):
            i += 1
            continue
        got = None
        for size in (5, 4, 3, 2, 1):
            if i + size > n:
                continue
            gram = " ".join(words[i:i + size])
            if size == 1 and gram in _MONTHS:   # a bare month ("in May", "since March") is a DATE, never a place
                continue
            r = _resolve(gram, mentions)
            if not r:
                continue
            kind, lat, lng, country, label, prior, supported = r
            # A demonym/country that is ALSO a common English word ("a bit of polish", "fine china", "cold
            # turkey", "guinea pig") is only that PLACE when it is Capitalised in the source — lower-case in
            # a sentence-case headline is the ordinary word. SHIPPED: "lacking a bit of polish" -> Poland.
            if size == 1 and gram in _CASED_PLACE_WORDS and not orig[i][:1].isupper():
                continue
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
                # "the town/village/city/port of X", "outskirts of X" — X is explicitly declared a place,
                # which is enough locational context to accept a small (weak) town named only in the body,
                # like Kyrylivka in "a hotel in the town of Kirilovka on the Azov Sea".
                if not located_here and i >= 2 and words[i - 1] == "of" and words[i - 2] in _PLACE_OF_NOUNS:
                    located_here = True
                # An ALL-CAPS token of 3+ letters is an ACRONYM (HIV, USAID, GDP, NASA, OPEC), not a city —
                # unless the sentence explicitly locates something there. SHIPPED: "HIV prevention drug" dotted
                # the village of Hiv, Iran. Real city names are Title-case in headlines, never SCREAMING-caps.
                if size == 1 and len(gram) >= 3 and orig[i].isupper() and not located_here:
                    continue
                # TINY towns (pop < 15k — the small-town coverage we added) only dot when the sentence
                # EXPLICITLY locates something there ("in X", "town of X"). Otherwise a same-named common
                # word, company or person ("Meta", "Leader", "Middle East") would drop a false dot. Real
                # cities (pop >= 15k) are unaffected. This is what makes the big gazetteer safe.
                if prior < 15000 and not located_here:
                    continue
                if _ner_vetoes(spans, cs, ce, weak, supported, located_here):
                    continue
                if weak and (gram in _BAD_CITY_NAMES or not orig[i][:1].isupper()):
                    continue
                # "University" is never the town University, Florida. "Sparks"/"Brent" are real cities but
                # usually a verb / oil benchmark — a dot only when the sentence locates something there.
                if gram in _NEVER_CITY_WORDS:
                    continue
                if gram in _NOT_CITY_WORDS and not located_here:
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
                # A small/mid town that is ALSO a personal name ("NANCY Pelosi", "Abrego GARCIA",
                # "Secretary RUBIO") is the PERSON, not the place, when a capitalised NON-place word sits
                # right beside it and nothing locates it. Real metros (>=150k) are protected, and a
                # preposition or a neighbouring place in front keeps the city. This is the surname guard
                # generalised past the tiny "weak" list — it fixes Garcia/Rubio/Nancy dotting
                # Mexico/Venezuela/France on serious US news, where NER would have vetoed the name.
                if prior < 150000 and not located_here:
                    _is_person = False
                    for j in (i - 1, i + size):
                        if 0 <= j < n and orig[j][:1].isupper():
                            wj = words[j]
                            if (wj not in _GEO_PREP and wj not in _GEO_ACTION and wj not in CITY_CANDS
                                    and wj not in COUNTRY_ALIASES and wj not in DEMONYMS
                                    and wj not in _DIRECTIONS):
                                _is_person = True
                                break
                    if _is_person:
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
    # air-defence / weapon-SYSTEM head nouns: "Russian Buk-M3 and S-300 air defense SYSTEM"
    "system", "systems", "battery", "batteries", "launcher", "launchers", "radar", "radars",
    "sam", "sams", "howitzer", "howitzers", "gun", "guns", "vehicle", "vehicles", "equipment",
    "hardware", "installation", "installations", "emplacement", "emplacements", "depot", "depots",
}


def _is_materiel_nationality(h, words):
    """A demonym/country attached to a WEAPON names where the weapon is FROM, not where it struck.
    'Iranian drone', 'Russian missile', "Iran's projectile", 'Russian Buk-M3 and S-300 air defense
    SYSTEM' — drop it exactly like a person's passport so the actual scene ('...on the Kostiantynivka
    front') can win. The head noun can sit a few words past the demonym (a compound weapon name), so scan
    the phrase up to the first verb/preposition."""
    if h[1] not in ("country", "demonym"):
        return False
    j = h[0] + len(str(h[7]).split())
    if j < len(words) and words[j] == "s":           # possessive: "Iran's drone"
        j += 1
    for k in range(j, min(j + 8, len(words))):
        w = words[k]
        if (w in _GEO_PREP or w in _GEO_ACTION or w in _SAY_VERBS or w in _ACTOR_SEAT_VERBS
                or w in ("launches", "launched", "fires", "fired", "conducts", "conducted", "deploys", "deployed")):
            break                                    # a verb/preposition ends the weapon phrase — the country
            #  before it is the ACTOR, not the weapon's nationality ("North Korea TESTS missile" is North Korea)
        if w in _MATERIEL_NOUNS:
            return True
    return False


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


# A country bolted onto one of its OWN ASSETS abroad names the owner, not the scene — the asset sits in
# whatever host country the sentence locates it in. "attacked US BASES in Bahrain" happens in Bahrain, not
# the US; "US EMBASSY in Beirut" is in Beirut; "Russian TROOPS in Syria" are in Syria.
_ASSET_NOUNS = {"base", "bases", "embassy", "embassies", "consulate", "consulates", "troops", "forces",
                "soldiers", "personnel", "installation", "installations", "garrison", "garrisons",
                "contingent", "outpost", "outposts", "warship", "warships", "convoy", "convoys"}


def _is_nationality(h, words):
    """A country bolted onto a PERSON, a SHIP'S FLAG, or one of its OWN ASSETS abroad. Not the scene.
    SHIPPED BUG: "'US NATIONAL' arrested in India" dotted the US, "Russia struck the TANZANIA-FLAGGED
    cargo vessel off Odessa" dotted TANZANIA, and "attacked US BASES in Bahrain" dotted the US — an
    asset's owner is the least locational fact; the host country ('in Bahrain') is where it is."""
    nxt = _nxt_word(h, words)
    return h[1] == "country" and (nxt in _PERSON_NOUNS or nxt in _ASSET_NOUNS
                                  or nxt in ("flagged", "born", "based", "owned"))


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
    nationality, not what a sanction is aimed at, not a water named attributively, and not a backdrop/
    adversary place ('despite the Hormuz crisis', 'the war against Iran')."""
    _ctx = _context_places(hits, words)
    return [h for h in hits
            if h[0] not in _ctx and not _is_actor_h(h, words) and not _is_nationality(h, words)
            and not _is_policy_target(h, words) and not _is_attrib_water(h, words)]


def _bare_city_list(hits, words):
    """A headline that LISTS several cities with none in a locating context — 'from Havana to Tehran to
    Beirut' — is a rhetorical sweep, not one scene. The first-listed city is no more the location than the
    others, so we should read the body for the real place instead of grabbing whichever came first. Fires
    only when there are 2+ city hits AND not one of them sits after an 'in/at/on/strikes…' locator."""
    cities = [h for h in hits if h[1] == "city"]
    if len(cities) < 2:
        return False
    for h in cities:
        if any(w in _GEO_PREP or w in _GEO_ACTION for w in words[max(0, h[0] - 2):h[0]]):
            return False
    return True


_ADVERSARY_NOUNS = {"conflict", "war", "tensions", "tension", "dispute", "clash", "standoff", "rift",
                    "feud", "row", "friction", "confrontation", "rivalry", "quarrel", "spat"}


def _adversary_parties(hits, words):
    """Indices of countries/demonyms named only as the OTHER SIDE of a fight — 'in conflict WITH Russia',
    'war WITH X', 'dispute WITH X'. Such a country is a PARTY to the dispute, not the SCENE of the event."""
    adv = set()
    for h in hits:
        if h[1] in ("country", "demonym"):
            i = h[0]
            if i >= 2 and words[i - 1] == "with" and words[i - 2] in _ADVERSARY_NOUNS:
                adv.add(i)
    return adv


# A place named as CONTRASTING BACKDROP ("China buys oil DESPITE the Hormuz crisis") — the event is the
# subject's action, the place is just scenery.
_CONTEXT_PREP = {"despite", "notwithstanding", "amid", "amidst"}
# An ABSTRACT struggle "against X" makes X the ADVERSARY, not a physical scene ("the WAR against Iran",
# "STRATEGY against Russia"). A PHYSICAL "strike/attack/raid against X" is deliberately NOT here — there the
# target IS where it landed, so X stays the scene.
_CONFLICT_NOUN = {"war", "campaign", "offensive", "strategy", "policy", "pressure", "effort", "efforts",
                  "struggle", "fight", "action", "actions", "measure", "measures", "sanction", "sanctions",
                  "aggression", "hostility", "hostilities", "standoff", "confrontation", "crackdown", "failure"}
# A judicial office right after a country/demonym marks the SEAT of a legal ruling — the story's real scene.
# "UK judge rules", "US Supreme Court", "France's prosecutor charges…": the country is where the court sits.
_JUDICIAL_SEAT = {"judge", "judges", "court", "courts", "justice", "prosecutor", "prosecutors",
                  "tribunal", "magistrate", "magistrates", "supreme"}
# A country named as the BACKER/FUNDER of a scheme is the accused party, not the scene: "UAE-funded plot",
# "Iran-backed militia", "Saudi-led coalition". The event happens where the plot/attack LANDS (British soil,
# Israel, Yemen), so the backer is dropped and the real subject/scene wins. SHIPPED BUG: "After revelation of
# UAE funded plot… MAB urge UK government" dotted the UAE (the accused) instead of the UK (the British group
# making the appeal). A word like "funded" right after a country name is the tell.
_BACKER_WORDS = {"funded", "backed", "sponsored", "financed", "bankrolled", "led", "linked",
                 "directed", "orchestrated", "controlled", "affiliated"}
# Only treat "<Country> <backer-word>" as a BACKER when the backer-word is an ADJECTIVE on a following noun
# ("UAE-funded PLOT", "Saudi-led COALITION"). If the next word is a function word it's a VERB ("Russia backed
# THE deal", "Germany funded OUT of…", "China backed OFF") and the country is the actor/subject — keep it.
_BACKER_VERB_AFTER = {"the", "a", "an", "to", "out", "up", "by", "away", "off", "down", "into", "onto",
                      "over", "that", "this", "it", "them", "him", "her", "us", "and", "or", "but", "with"}


def _context_places(hits, words):
    """Indices of places named as BACKDROP, ADVERSARY or BACKER, not the scene: 'DESPITE/amid the Hormuz
    crisis' (a contrasting backdrop), 'the war/strategy AGAINST Iran' (the adversary of an ABSTRACT struggle),
    and 'UAE-FUNDED plot' / 'Iran-BACKED militia' (the accused sponsor). Such a place must never win over the
    story's real subject/scene."""
    ctx = set()
    for h in hits:
        i = h[0]
        if i >= 1 and words[i - 1] in _CONTEXT_PREP:
            ctx.add(i)
        elif h[1] in ("country", "demonym") and i >= 1 and words[i - 1] == "against":
            if any(words[k] in _CONFLICT_NOUN for k in range(max(0, i - 4), i - 1)):
                ctx.add(i)
        elif (h[1] in ("country", "demonym") and i + 2 < len(words) and words[i + 1] in _BACKER_WORDS
              and words[i + 2] not in _BACKER_VERB_AFTER):
            ctx.add(i)                     # "<Country> funded/backed/led <noun>" -> the sponsor, not the scene
        elif i >= 1 and words[i - 1] == "from" and any(
                words[k] in _RETURN_WORDS for k in range(max(0, i - 5), i - 1)):
            ctx.add(i)                     # "returns/back FROM <place>" -> where the subject WAS, not the scene
        elif i >= 1 and words[i - 1] == "in" and any(
                words[k] in _ORIGIN_WORDS for k in range(max(0, i - 4), i - 1)):
            ctx.add(i)                     # "had BEEN/was/stayed IN <place>" -> a past/origin location
    return ctx


# A leader RETURNING home from abroad is news in their OWN country, not where they were. "Cameroon's President
# returns from … Switzerland" dotted SWITZERLAND. A place after "returns/back FROM" or after "had been/was/
# stayed IN" is where the subject WAS (past/origin) — drop it so the subject's own country wins.
_RETURN_WORDS = {"returns", "returned", "return", "returning", "back", "comes", "came", "arrives",
                 "arrived", "home", "abroad", "overseas"}
_ORIGIN_WORDS = {"been", "was", "were", "stayed", "staying", "remained", "spent", "spending", "holed"}


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
        # A country/demonym that is the SUBJECT of a strike verb ("US POUNDS Iranian city") is the
        # attacker — an earlier "explosions"/"blast" must not sneak it into `located` over its target.
        if loc and (_nxt(h) == "s" or _is_nationality(h, words)
                    or (h[1] in ("country", "demonym") and _nxt(h) in _STRIKE_VERBS)):
            loc = False
        # A LEGAL RULING happens in its JURISDICTION: "<Country> judge/court/justice/prosecutor …" makes
        # that country the scene of the story. SHIPPED BUG: "Palestine Action 'Barclays five' … UK judge
        # rules" dotted PALESTINE (leftmost place, inside the group's NAME) instead of the UK where the
        # court sat. The country right before a judicial office is the seat, so promote it to `located`.
        if h[1] in ("country", "demonym") and _nxt(h) in _JUDICIAL_SEAT:
            loc = True
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


def _sea_country(b, mentions):
    """A sea's filed country is arbitrary (GeoNames files the Black Sea under Turkey). Take it from the
    STORY instead. The actor tends to lead the sentence and the struck location follows, so prefer the
    LAST country named: 'Russian MOD ... strikes on Ukrainian ports' -> Black Sea, Ukraine."""
    if b[7] in _WATER_NAMES:
        ctx = [co for (co, g) in mentions if co in COUNTRY_COORDS]
        if ctx and b[5] not in ctx:
            return ctx[-1]
    return b[5]


_SEA_COORD_RE = re.compile(r"\b([A-Za-z]+)\s+and\s+([A-Za-z]+)\s+seas\b", re.I)


def _expand_water_coord(text):
    """News collapses two adjacent seas into a plural coordination — "the Black and Azov seas", "the
    Baltic and North seas". Neither half then matches a SINGULAR gazetteer key ("black sea"/"azov sea"),
    so the only surviving token, "Azov", hits the TOWN and the dot leaves the water (or falls back to the
    speaker's capital). Expand the coordination back into two singular sea names so each one matches. A
    pairing that isn't a real sea simply matches nothing downstream, so this is safe to apply broadly."""
    return _SEA_COORD_RE.sub(lambda m: m.group(1) + " Sea and " + m.group(2) + " Sea", text or "")


# An OUTLET whose NAME contains a place must not set the scene. "The Wall Street Journal, citing a US Army
# official, reports…" dotted the NYC FINANCIAL DISTRICT (the curated 'wall street' point) — the paper is the
# reporter, not where the event happened. Blank the outlet phrase before scanning so the story's real subject
# (here 'U.S. Army' -> the US) wins. Kept tight to this one name; other outlets' place-words (New York Times,
# Washington Post) resolve to their own country and haven't misfired.
_OUTLET_GEO_STRIP = re.compile(r"\b(the\s+)?wall\s+street\s+journal\b", re.I)


@functools.lru_cache(maxsize=4096)
def _geolocate(title, sourcecountry, desc="", url=""):
    """Best location for an event. Context (other countries named + the article's own section) decides
    between readings of an ambiguous name. If the headline names nowhere we read the story's summary
    before ever falling back to the outlet's home country.

    Memoized: an article's inputs don't change between the 15-min rebuilds, so re-geolocation is free
    after the first pass. The result is a plain tuple/None (deterministic — depends only on the static
    gazetteer), so caching is safe. Restart clears it, which is exactly what we want after a logic fix."""
    title = _OUTLET_GEO_STRIP.sub(" ", _expand_water_coord(title or ""))  # "Black and Azov seas" -> two seas; drop 'Wall Street Journal'
    # Strip the wire's promo lead and any source URL from the BODY before scanning — but NOT the dateline
    # (_dateline_place needs it). SHIPPED BUG: "JUST IN - Nikita Bier resigns…" dotted a village near Yalta,
    # because the trailing "in" of "JUST IN" read as "…IN Nikita", flipping on the locating context that
    # defeats the tiny-town and surname vetoes. A source URL ("reuters.com/world/china") can inject a place too.
    _d = _OUTLET_GEO_STRIP.sub(" ", _PROMO_URL.sub(" ", _PROMO_LEAD.sub("", desc or "")))
    desc = _expand_water_coord(_d)
    mentions = _context_mentions(title + " " + (desc or ""), url)
    dl = _dateline_place(desc, mentions)
    hits, words = _scan_places(title, _person_spans(title), mentions)
    # When a story names BOTH the Black Sea and the enclosed Sea of Azov, the Azov is the specific scene —
    # the ship/tanker strikes happen in that shallow, enclosed basin, while "Black Sea" is the broader
    # theatre. Drop the Black Sea so the dot lands on the more specific water ("Black and Azov seas" ->
    # Sea of Azov). Only fires when both are present, so a lone "Black Sea" story is untouched.
    if hits:
        _wn = {h[7] for h in hits}
        if (_wn & {"sea of azov", "azov sea"}) and "black sea" in _wn:
            hits = [h for h in hits if h[7] != "black sea"]
    # A BACKDROP/ADVERSARY place is not the scene: "China buys oil DESPITE the Hormuz crisis" -> the event is
    # China's; "Trump's failure in the war AGAINST Iran" -> Iran is the adversary. Drop such places so the
    # subject wins; if that empties the title, the desc/actor ladder below finds the real scene (or none).
    if hits:
        _ctx = _context_places(hits, words)
        if _ctx:
            _non = [h for h in hits if h[0] not in _ctx]
            if _non:
                hits = _non
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
                    return b[2], b[3], b[4], _sea_country(b, mentions)
            hits = []                     # title held only nationalities -> use the fallback ladder
    # THE HEADLINE NAMES NO SCENE — ONLY WHO DID IT, OR A COASTLINE. The story's own summary almost
    # always names the actual place, so read it before settling for the actor's country.
    # SHIPPED BUG: "RUSSIA strikes Ukrainian drone industry and BLACK SEA PORTS" dropped a dot in open
    # water in the middle of the sea, while its very first line read "…port infrastructure in ODESSA
    # and Yuzhny". The dot must be where the event happened, and a port is not in the sea.
    if hits and desc and (not _genuine_scenes(hits, words) or _bare_city_list(hits, words)):
        dh, dw = _scan_places(desc[:400], _person_spans(desc[:400]), mentions)
        dscenes = _genuine_scenes(dh, dw) if dh else []
        if dscenes:
            # A POSSESSIVE country in the title is the story's SUBJECT: "Spain's migrant crisis" -> the scene is
            # IN Spain (Ceuta), not the "regime change wars in IRAQ" it blames. A country used as the ACTOR of a
            # verb ("Russia strikes Ukraine") is NOT possessive, so it never hijacks the real (Ukrainian) scene.
            _poss = {h[5] for h in hits if h[1] == "country" and h[0] + 1 < len(words) and words[h[0] + 1] == "s"}
            _pref = [d for d in dscenes if d[5] in _poss] if _poss else []
            # When the TITLE was a bare list of cities, prefer the body scene that IS one of those cities
            # ('from Havana to Tehran to Beirut' + a body about Beirut -> Beirut, not a stray body city).
            _tcities = {h[7] for h in hits if h[1] == "city"}
            _match = [d for d in dscenes if d[7] in _tcities] if _tcities else []
            b = _pick_place(_pref or _match or dscenes, dw)
            if b is not None:
                return b[2], b[3], b[4], _sea_country(b, mentions)
    if hits:
        best = _pick_place(hits, words)
        # "<Compass> <Country>" is a foreign REGION that GeoNames ALSO lists as a small US town — "South
        # Lebanon" is southern Lebanon, not the village of South Lebanon, Ohio. Convert ONLY when the story
        # itself names that country elsewhere (in its desc, or in the title beyond the town span), so a genuine
        # "West Jordan, Utah" local story stays put. SHIPPED BUG: an "Israeli activity in south Lebanon" dot
        # (UNIFIL, Israel) landed in Ohio and was labelled "South Lebanon, United States".
        if best[5] == "United States of America" and best[1] == "city":
            _p = (best[7] or "").split()
            if len(_p) == 2 and _p[0] in _COMPASS_WORDS:
                _fco = _country_word_map().get(_p[1])
                if _fco and _fco in COUNTRY_COORDS:
                    _bp = best[0]
                    _elsewhere = any(w == _p[1] for k, w in enumerate(words) if k not in (_bp, _bp + 1))
                    if _elsewhere or re.search(r"\b" + re.escape(_p[1]) + r"\b", (desc or "").lower()):
                        _la, _ln = COUNTRY_COORDS[_fco]
                        return _la, _ln, best[4].split(",")[0] + ", " + _co_short(_fco), _fco
        # A country named only as the OTHER SIDE of a conflict ('conflict WITH Russia') is a PARTY, not the
        # scene. If that's what the rules picked and the story names a DIFFERENT country/demonym as its
        # subject, prefer that. SHIPPED: 'Siding with West in conflict with Russia unacceptable for Serbs'
        # dotted Russia (Moscow centroid) instead of Serbia.
        if best[1] in ("country", "demonym") and not _is_facility(best):
            _adv = _adversary_parties(hits, words)
            if best[0] in _adv:
                _alt = [h for h in hits if h[0] not in _adv]
                b2 = _pick_place(_alt, words) if _alt else None
                if b2 is not None and b2[0] not in _adv:
                    return b2[2], b2[3], b2[4], _sea_country(b2, mentions)
        # A COUNTRY that is the SUBJECT taking a domestic action at its own seat ("France ORDERS the
        # expulsion…") beats a country named only as background later ("…Intervention in Ukraine") — even
        # when that later one reads as "located" via an "in". Only fires when the chosen place is itself a
        # bare country/demonym, so a real city/facility scene is never overruled.
        if best[1] in ("country", "demonym") and not _is_facility(best):
            asc = _actor_seat_country(hits, words)
            if asc and asc in COUNTRY_COORDS and asc != best[5]:
                la, ln = COUNTRY_COORDS[asc]
                return la, ln, _co_short(asc), asc
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
        # A leader only CONSIDERING/THREATENING a move on a foreign country is news at their seat — even
        # though "attack"/"strike" makes the target look located, the strike is merely being weighed.
        # "Trump considers attack on Iran" -> Washington. (A real city scene is a `city`, so it never
        # reaches here.) Skipped if the leader's own country IS the target.
        if not _is_facility(best) and best[1] in ("country", "demonym"):
            dc = _deliberation_country(words)
            if dc and dc in COUNTRY_COORDS and dc != best[5]:
                la, ln = COUNTRY_COORDS[dc]
                return la, ln, _co_short(dc), dc
            # A leader's on-record COLON statement ("Trump to Axios: ...with Iran") is news at their seat —
            # returns the SAME country point as a say-verb statement, so the two coverages of one statement
            # share a place and dedup-merge into one dot instead of scattering to the foreign topic.
            oc = _onrecord_statement_country(title)
            if oc and oc in COUNTRY_COORDS and oc != best[5]:
                la, ln = COUNTRY_COORDS[oc]
                return la, ln, _co_short(oc), oc
        if not located and not _is_facility(best) and best[1] in ("country", "demonym"):
            # A named official SPEAKING/TESTIFYING is news in their OWN country — the country they name is
            # the topic. "Hegseth testifies on Iran" / "..., says Defense Secretary Hegseth" -> United States.
            ac = _actor_country(words)
            if ac and ac in COUNTRY_COORDS:
                la, ln = COUNTRY_COORDS[ac]
                return la, ln, _co_short(ac), ac
        if not located and not _is_facility(best) and best[1] in ("country", "demonym"):
            # A ministry/government ACTING is news at its own seat — the foreign place it names is
            # the SUBJECT, not the scene. Nothing here is "located", so no real scene is at stake.
            # GUARD (best is country/demonym): a genuine CITY scene must never be overruled by the
            # actor's country. SHIPPED: "Statue of Yoni Netanyahu unveiled at Uganda's Entebbe Airport"
            # dotted ISRAEL — the possessive "Uganda's" pushed the "at" out of the located window, and
            # _statement_country (Netanyahu -> Israel) then hijacked the real Entebbe scene.
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


_GEOAI_VER = "2"   # bump to invalidate cached AI geolocations when the prompt/format changes


def _geolocate_grounded(title, text):
    """GROUNDED geolocation via Gemini + Google Search — for a hard location the model can LOOK UP the exact
    site (which refinery, which town Ukraine struck) instead of guessing from the text alone. Names a plain
    place the caller grounds through the gazetteer for coordinates; NEVER returns coordinates itself. Cached
    30 days per story. Returns "" with no Gemini key or on any error, so it stays purely additive on top of
    the rules + the free (Groq) fallback."""
    title = (title or "").strip()
    text = (text or "").strip()[:4000]
    if not (title or text):
        return ""
    try:
        key = load_gemini_key()
    except Exception:
        key = ""
    if not key:
        return ""
    cache = os.path.join(CACHE_DIR, "geogr_" + hashlib.sha1(
        (_GEOAI_VER + "\n" + title + "\n" + text).encode("utf-8")).hexdigest()[:16] + ".json")
    if _fresh(cache, 30 * 86400):
        try:
            return json.load(open(cache, encoding="utf-8")).get("p", "")
        except Exception:
            pass
    prompt = ("Using web search, identify the ONE real place where the EVENT in this news story physically "
              "happened — the exact city, town or site. If a specific named FACILITY is struck, on fire or "
              "attacked (a refinery, plant, base, airport, port), LOOK UP which city that facility is in and "
              "name that city. IGNORE any place mentioned only for comparison, distance or context "
              "('6,500 km from Ukraine', 'farther than the Orsk refinery'). If the story is only an action "
              "taken BY a country or leader (a statement, threat, ruling), give that actor's OWN country.\n"
              "Reply with ONLY the place — 'City, Country', or just 'Country', or 'NONE'. No explanation, no "
              "coordinates.\n\nHEADLINE: " + title + "\n\nSTORY:\n" + text)
    out = ""
    try:
        url = ("https://generativelanguage.googleapis.com/v1beta/models/"
               + GEMINI_MODEL + ":generateContent?key=" + urllib.parse.quote(key))
        body = json.dumps({
            "contents": [{"parts": [{"text": prompt}]}],
            "tools": [{"google_search": {}}],
            "generationConfig": {"temperature": 0.0, "maxOutputTokens": 60},
        }).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=55) as rr:
            j = json.loads(rr.read().decode("utf-8"))
        out = j["candidates"][0]["content"]["parts"][0]["text"].strip()
    except Exception:
        out = ""
    out = ((out or "").splitlines() or [""])[0]
    out = re.sub(r'^[\s"\'.•*\-]+|[\s"\'.\-]+$', "", out).strip()
    if out.upper() == "NONE" or not (3 <= len(out) <= 60):
        out = ""
    try:
        json.dump({"p": out}, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


@functools.lru_cache(maxsize=4096)
def _geolocate_ai(title, text):
    """FREE AI fallback for the HARD locations the rule gazetteer can't pin. Reads the WHOLE story and
    names the ONE place where the event physically happened, as a plain 'City, Country' (or 'Country', or
    ''). The name is GROUNDED through the same gazetteer for coordinates by the caller — the model proposes
    a place name but NEVER returns lat/long (those it hallucinates). Cached 30 days per story on disk (a
    network call), and memoised per process. Returns "" when no free LLM is configured or on any error."""
    title = (title or "").strip()
    text = (text or "").strip()[:4000]
    if not (title or text) or not _llm_available():
        return ""
    cache = os.path.join(CACHE_DIR, "geoai_" + hashlib.sha1(
        (_GEOAI_VER + "\n" + title + "\n" + text).encode("utf-8")).hexdigest()[:16] + ".json")
    if _fresh(cache, 30 * 86400):
        try:
            return json.load(open(cache, encoding="utf-8")).get("p", "")
        except Exception:
            pass
    system = ("You are a precise news geolocator. You read a story and name the ONE real place where the "
              "described EVENT physically happened — never where someone merely reacted to it, never a "
              "person's nationality, never an organisation's headquarters.")
    user = ("Where did the EVENT in this story physically take place? Reply with ONLY the place, nothing "
            "else:\n"
            "- 'City, Country' when a specific city, town or site is identifiable (e.g. 'Entebbe, Uganda').\n"
            "- If a specific named FACILITY is the scene (a refinery, plant, base, airport, port), name the "
            "CITY that facility is in — e.g. 'Rosneft's Komsomolsk-on-Amur refinery' -> 'Komsomolsk-on-Amur, "
            "Russia'.\n"
            "- Just the country when only the country is knowable (e.g. 'Uganda').\n"
            "- If the story is an action taken BY a country or leader with no scene of its own (a "
            "statement, threat, ruling or decision), give that ACTOR's OWN country — not any country it "
            "merely talks about or threatens.\n"
            "- NEVER name a place mentioned only for COMPARISON, DISTANCE or CONTEXT ('6,500 km from "
            "Ukraine', 'farther than the Orsk refinery', 'unlike Moscow') — only where the event ITSELF "
            "happened.\n"
            "- 'NONE' if there is genuinely no location.\n"
            "No explanation, no coordinates, no quotes — just the place name.\n\n"
            "HEADLINE: " + title + "\n\nSTORY:\n" + text)
    out = _geolocate_grounded(title, text) or _llm_complete(system, user, max_tokens=24, temperature=0.0, spread=True)
    out = ((out or "").splitlines() or [""])[0]
    out = re.sub(r'^[\s"\'.•*\-]+|[\s"\'.\-]+$', "", out).strip()
    if out.upper() == "NONE" or not (3 <= len(out) <= 60):
        out = ""
    try:
        json.dump({"p": out}, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


def _ai_locate_verify(title, text, candidates):
    """A SECOND, independent AI opinion that RESOLVES a location disagreement — the vote the user asked for.
    When the rules and the first AI pass name different countries, this reads the story fresh, is shown the
    competing candidates, and picks the ONE place the EVENT physically happened (the subject's own action —
    never a country named only as a rival, a backdrop, or something the subject talks about). Returns a plain
    'City, Country'/'Country' the caller grounds through the gazetteer, or '' — cached 30 days per story."""
    title = (title or "").strip()
    text = (text or "").strip()[:4000]
    cand = " | ".join(c for c in candidates if c)
    if not (title and cand) or not _llm_available():
        return ""
    cache = os.path.join(CACHE_DIR, "geov_" + hashlib.sha1(
        (_GEOAI_VER + "\n" + title + "\n" + cand + "\n" + text[:200]).encode("utf-8")).hexdigest()[:16] + ".json")
    if _fresh(cache, 30 * 86400):
        try:
            return json.load(open(cache, encoding="utf-8")).get("p", "")
        except Exception:
            pass
    system = ("You are a meticulous news geolocator settling a disagreement between two systems. You name the "
              "ONE real place where the described EVENT physically happened — the place of the SUBJECT's own "
              "action. You are strict about what is NOT the scene: a country named only as a RIVAL or the "
              "other side of a relationship ('amid US-China rivalry' -> not China or the US), a BACKDROP "
              "('despite the Hormuz crisis' -> not Hormuz), an ADVERSARY of an abstract struggle ('the war "
              "against Iran' -> not Iran), a place someone merely TALKS ABOUT or threatens, an org's "
              "headquarters, or a person's nationality.")
    user = ("Two systems disagree on where this happened. Candidate locations: " + cand + "\n"
            "Read the story and give the ONE correct place the EVENT physically took place — it may be one of "
            "the candidates, or a better place they both missed. If a specific named FACILITY is the scene, "
            "name the CITY it sits in. Reply with ONLY 'City, Country', or 'Country', or 'NONE'.\n\n"
            "HEADLINE: " + title + "\n\nSTORY:\n" + text)
    # Prefer GEMINI here: the first opinion came from Groq's gpt-oss, so a different model family (Google's
    # Gemini) makes this a genuinely INDEPENDENT tie-breaker. Falls back to Groq when no Gemini key is set.
    out = _llm_complete(system, user, max_tokens=24, temperature=0.0, prefer="gemini")
    out = ((out or "").splitlines() or [""])[0]
    out = re.sub(r'^[\s"\'.•*\-]+|[\s"\'.\-]+$', "", out).strip()
    if out.upper() == "NONE" or not (3 <= len(out) <= 60):
        out = ""
    try:
        json.dump({"p": out}, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


def _geo_is_weak(r):
    """A rule result worth an AI second opinion: None, or a bare COUNTRY CENTROID — the dot sits on the
    country's own capital coords, meaning the rules pinned no specific city/scene (the fallback ladder, or
    an actor's seat). A real city/facility/water scene is NOT weak and is never second-guessed."""
    if not r:
        return True
    lat, lng, country = r[0], r[1], r[3]
    cc = COUNTRY_COORDS.get(country)
    return bool(cc) and abs(lat - cc[0]) < 1e-3 and abs(lng - cc[1]) < 1e-3


def _place_in_title(label, title):
    """Does a resolved place's city/region name appear in the HEADLINE itself? A rule scene the title NAMES
    ('...in Khabarovsk Krai') is well grounded; an AI guess that contradicts it — often a place named only for
    comparison or distance ('farther than the Orsk refinery') — must not override it."""
    if not label or not title:
        return False
    city = _fold(str(label).split(",")[0]).lower().strip()
    return len(city) >= 4 and city in _fold(title).lower()


# ================= SELF-LEARNING GAZETTEER =================
# The AI often NAMES the exact town ("Deir Seryan, Lebanon") the rule gazetteer can't turn into a point, so
# the dot fell back to a REGION centroid and different towns collapsed onto one dot. Here we geocode that name
# ONCE via free OpenStreetMap Nominatim and remember the coordinates FOREVER (learned_places.json in DATA_DIR,
# gitignored, survives a cache clear). Next time it is a free, deterministic, cold-start rule hit — accuracy
# COMPOUNDS and AI/geocoder calls drop toward zero. Coordinates NEVER come from the LLM (it hallucinates
# lat/long); only from the geocoder. Purely additive: with no network this is a no-op and the rules stand.
_LEARNED_PLACES_PATH = os.path.join(DATA_DIR, "learned_places.json")
_LEARNED_PLACES_LOCK = threading.Lock()
try:
    _LEARNED_PLACES = json.load(open(_LEARNED_PLACES_PATH, encoding="utf-8")) if os.path.exists(_LEARNED_PLACES_PATH) else {}
    if not isinstance(_LEARNED_PLACES, dict):
        _LEARNED_PLACES = {}
except Exception:
    _LEARNED_PLACES = {}


def _lp_key(name):
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _learned_place_lookup(name):
    """A previously-learned place -> (lat, lng, 'Display, Country', country), or None. A free, deterministic
    LOCAL read: works on cold start, costs nothing, and is what makes the gazetteer 'self-learning'."""
    rec = _LEARNED_PLACES.get(_lp_key(name))
    if not rec:
        return None
    try:
        return (float(rec["lat"]), float(rec["lng"]), rec.get("place") or name, rec.get("country") or "")
    except Exception:
        return None


def _learn_place(name, lat, lng, place, country):
    """Persist a name->coordinates mapping the geocoder resolved, so it is ours forever (atomic write)."""
    key = _lp_key(name)
    if not key:
        return
    rec = {"lat": round(float(lat), 5), "lng": round(float(lng), 5),
           "place": place or name, "country": country or "", "ts": int(time.time())}
    with _LEARNED_PLACES_LOCK:
        if _LEARNED_PLACES.get(key) == rec:
            return
        _LEARNED_PLACES[key] = rec
        try:
            tmp = _LEARNED_PLACES_PATH + ".tmp"
            json.dump(_LEARNED_PLACES, open(tmp, "w", encoding="utf-8"), ensure_ascii=False)
            os.replace(tmp, _LEARNED_PLACES_PATH)
        except Exception:
            pass


_NOMINATIM_MIN_INTERVAL = 1.1          # OSM usage policy: <= ~1 request/second, descriptive User-Agent required
_nominatim_last = [0.0]
_nominatim_lock = threading.Lock()
_nominatim_budget = [0, 0.0]           # [calls_this_window, window_start] — a safety cap on a cold first build


def _geocode_nominatim(place):
    """Resolve a place NAME ('Deir Seryan, Lebanon') to real (lat, lng, country) via free OSM Nominatim.
    Cached 30 days on disk (misses too), rate-limited + budgeted per OSM policy, and fully error-safe:
    returns None on any failure or offline, so the feature stays additive. NEVER called on the synchronous
    cold-start path (guarded by `allow_ai` in `_locate`) — only in the background/live pass."""
    q = (place or "").strip()
    if len(q) < 3:
        return None
    cache = os.path.join(CACHE_DIR, "geocode_" + hashlib.sha1(q.lower().encode("utf-8")).hexdigest()[:16] + ".json")
    if _fresh(cache, 30 * 86400):
        try:
            j = json.load(open(cache, encoding="utf-8"))
            return (j["lat"], j["lng"], j.get("country") or "") if j.get("lat") is not None else None
        except Exception:
            pass
    # SAFETY BUDGET: cap live geocodes per rolling window so a cold first build (many new towns) can't run
    # for many minutes. The rest simply stay rule-placed this build and get learned on later ones.
    now = time.time()
    if now - _nominatim_budget[1] > 900:       # 15-min window
        _nominatim_budget[0], _nominatim_budget[1] = 0, now
    if _nominatim_budget[0] >= 80:
        return None
    out = None
    try:
        with _nominatim_lock:
            wait = _NOMINATIM_MIN_INTERVAL - (time.time() - _nominatim_last[0])
            if wait > 0:
                time.sleep(wait)
            _nominatim_last[0] = time.time()
        _nominatim_budget[0] += 1
        url = ("https://nominatim.openstreetmap.org/search?format=jsonv2&limit=1&addressdetails=1&q="
               + urllib.parse.quote(q))
        req = urllib.request.Request(url, headers={
            "User-Agent": "Meridian-News-Map/%s (news map dot geocoding)" % APP_VERSION})
        with urllib.request.urlopen(req, timeout=12) as rr:
            arr = json.loads(rr.read().decode("utf-8"))
        if arr:
            top = arr[0]
            country = ((top.get("address") or {}).get("country")) or ""
            out = (round(float(top["lat"]), 5), round(float(top["lon"]), 5), country)
    except Exception:
        out = None
    try:
        json.dump({"lat": out[0] if out else None, "lng": out[1] if out else None,
                   "country": out[2] if out else ""}, open(cache, "w", encoding="utf-8"))
    except Exception:
        pass
    return out


def _sharpen_ai_place(aw, anchor_country, allow_ai):
    """The AI named 'City, Country' but the rules could only reach the region/country centroid. Pin the EXACT
    city: first from the learned gazetteer (free, cold-start), else a one-time Nominatim geocode (live pass
    only) whose result is anchored to the story's country and then LEARNED. Returns a specific
    (lat, lng, place, country) or None (leave the rules' answer in place)."""
    lp = _learned_place_lookup(aw)
    if lp and (not anchor_country or not lp[3] or _country_match(lp[3], anchor_country)):
        return lp
    if not allow_ai:
        return None
    geo = _geocode_nominatim(aw)
    if not geo:
        return None
    lat, lng, ncountry = geo
    # GUARD: a hallucinated town that geocodes to the wrong country is rejected — the point's country must
    # match the country the story anchored on (loose match; Nominatim's label may differ from ours).
    if anchor_country and ncountry and not _country_match(ncountry, anchor_country):
        return None
    place = aw if "," in aw else ((aw + ", " + anchor_country) if anchor_country else aw)
    country = anchor_country or ncountry
    _learn_place(aw, lat, lng, place, country)
    return (lat, lng, place, country)


def _geo_confidence(loc):
    """How precisely we know WHERE. 'low' ONLY when the dot fell back to a broad REGION or WATER centroid —
    a genuinely APPROXIMATE spot (a strike somewhere in 'Southern Lebanon') the UI marks so a reader isn't
    misled it's exact. A bare COUNTRY centroid is deliberately NOT flagged: for a NATIONAL story ('Ukraine at
    35: winter is coming…') the country IS the right level, and flagging all ~50 of them was pure noise. A
    specific city/facility/learned place is always 'high'."""
    if not loc:
        return "low"
    if _is_area_place(loc[2] if len(loc) > 2 else ""):
        return "low"
    return "high"


def _locate(title, sourcecountry, desc, url="", allow_ai=True):
    """The location for a dot. RULES first (free, deterministic, tested); only when they can't pin a
    specific place does the FREE AI read the whole story and name it — grounded back through the SAME
    gazetteer for coordinates, and anchored to a country the story actually names (so the model can't
    invent one). Purely additive: with no LLM this is exactly _geolocate.

    allow_ai=False keeps it to the RULES + the CACHED summary WHERE (no live network call) — the mode the
    synchronous cold-start build uses so it never blocks on hundreds of live geolocation calls. A live
    `_geolocate_ai` (uncached, one network round-trip EACH) is only worth it in a background/warm pass;
    on a cold cache 351 of them stacked to 6-12 min and the map showed no dots. A brand-new story is
    rule-placed on this build and upgraded to the AI's pinpoint on the next one (via the cached WHERE the
    summary prewarm fills in) — the same one-build lag summaries already have."""
    r = _geolocate(title, sourcecountry, desc, url)
    ment_list = _context_mentions((title or "") + " " + (desc or ""), url)
    ment = {co for (co, _t) in ment_list}
    # LEADER / OFFICIAL STATEMENT -> the CAPITAL. A quote, threat or ruling with no specific scene resolves to
    # a bare country centroid, but officials SPEAK from the capital — so pin it there (Putin -> Moscow,
    # Zelensky -> Kyiv): a specific dot the reader can place, and its OWN dot (statements are 'politics', which
    # no longer collapse). Only fires when the rules reached just the country AND the story is an on-record
    # statement; one that names a real scene ("Putin says Russia took Avdiivka") keeps that scene. The AI WHERE
    # for a statement returns the ACTOR's country, so this capital is not second-guessed downstream.
    if (r and _geo_is_weak(r) and r[3] in _CAPITAL_SEAT
            and _tg_is_statement((title or "") + ". " + (desc or ""))):
        _cla, _cln, _clbl = _CAPITAL_SEAT[r[3]]
        r = (_cla, _cln, _clbl + ", " + _co_short(r[3]), r[3])
    # AI PINPOINT (from the summary pass, once this story has been summarised): one AI call wrote the brief AND
    # named WHERE it happened. Trust it — grounded through the gazetteer, anchored to a country the story names
    # (or the rules' own, so the model can't invent one). A cached read (no live call needed) — the "all in one
    # go" path; on the build after a story is summarised, the dot moves to the AI's pinpoint.
    aw = _ai_where(title)
    if aw:
        g = _geolocate(aw, "", aw, "")
        # A WATER the AI names (Red Sea, Strait of Hormuz, Bab-el-Mandeb) is self-anchoring: a sea/strait is
        # an unambiguous global feature, so it needs no country-mention check. Its stored "country" is just
        # bookkeeping, so fly the story's own flag over it (the first country the text actually names).
        water = bool(g) and bool(g[2]) and (g[2].lower() in _WATER_NAMES or _is_water_place(g[2]))
        if water and g[3] not in ment:
            ctx_co = [c for (c, _t) in ment_list if c in COUNTRY_COORDS]
            if ctx_co:
                g = (g[0], g[1], g[2], ctx_co[0])
        if g and ((g[3] in ment) or (r and g[3] == r[3]) or water):
            # SELF-LEARNING SHARPEN: the AI named a specific 'City, Country' but the rules could only reach the
            # region/country centroid ('Deir Seryan, Lebanon' -> 'Southern Lebanon'). Pin the exact town from
            # the learned gazetteer (free, cold-start) or a one-time geocode (live), then remember it forever.
            if not water and "," in aw and (_geo_is_weak(g) or _is_area_place(g[2])):
                sharp = _sharpen_ai_place(aw, g[3] if (g[3] in ment or (r and g[3] == r[3])) else "", allow_ai)
                if sharp:
                    return sharp
            if not _geo_is_weak(g):
                # DON'T override a specific rule scene the HEADLINE itself names with a same-country AI guess
                # that the headline does NOT name — the AI is often misled by a place cited only for comparison
                # or distance. SHIPPED: "Komsomolsk-on-Amur refinery in Khabarovsk Krai … farther than Orsk"
                # -> the AI said Orenburg (Orsk) and overrode the correct Khabarovsk Krai.
                if (r and not _geo_is_weak(r) and r[3] == g[3]
                        and _place_in_title(r[2], title) and not _place_in_title(g[2], title)):
                    return r
                return g                              # a specific, anchored place -> use the AI's pinpoint
            if r is None or _geo_is_weak(r):
                return g                              # AI at least got the country; the rules had nothing better
    # DETERMINISTIC NAMESAKE GUARD (works on COLD START, no AI): the rules pinned a SPECIFIC town whose country
    # the story NEVER names, while it DOES name other countries — the classic namesake trap (a Ukraine war
    # capture dotting "Malaya, Philippines"; a Yemen clash dotting "Hays, Kansas"). Rather than ship the wrong
    # CONTINENT, drop to a country the story actually names. Prefer the LAST-mentioned (usually the target/scene:
    # "Russia captures a [Ukrainian] village" -> Ukraine; "Ukraine strikes a [Russian] refinery" -> Russia).
    if r and not _geo_is_weak(r) and r[3] not in ment:
        # require a DEMONYM (a nationality — "Russian", "Ukrainian") pointing elsewhere, not just a country
        # named in passing, so a genuine domestic story that merely mentions a foreign country never moves.
        _demco = [c for (c, _t) in ment_list if c in COUNTRY_COORDS and _t in DEMONYMS]
        # ...AND only when the resolved place is FAR (>2500 km) from EVERY named nationality's country — a
        # WRONG-CONTINENT namesake ("Malaya, Philippines" for a Ukraine war story), never a correct village
        # near the front whose own country simply went unnamed (Mala Tokmachka sits ~900 km from Russia).
        if _demco and all(_km(r[0], r[1], COUNTRY_COORDS[c][0], COUNTRY_COORDS[c][1]) > 2500 for c in _demco):
            _pick = _demco[-1]
            _la, _ln = COUNTRY_COORDS[_pick]
            r = (_la, _ln, _co_short(_pick), _pick)   # right country now; a live build refines it to the town
    if not allow_ai or not _llm_available():
        return r                                      # cold-start build (or no LLM): rules + cached WHERE only
    _txt = ((title or "") + ". " + (desc or "")).strip()
    # ENSEMBLE / SECOND OPINION — the rules pinned a SPECIFIC scene, but the AI's own location (named while it
    # wrote the brief) disagrees on the COUNTRY, and BOTH countries are named in the story. That is exactly the
    # ~1-in-5 dot a single pass gets wrong (a specific place that is really a rival/backdrop/what-it-talks-about).
    # Don't silently trust the rules: get a fresh, independent AI VOTE that sees both candidates and settles it.
    # Only overrides to a specific, story-anchored place; a confirmed or inconclusive vote leaves the rules.
    aw_g = _geolocate(aw, "", aw, "") if aw else None
    if r and not _geo_is_weak(r) and aw_g and aw_g[3] in ment and aw_g[3] != r[3]:
        _vote = _ai_locate_verify(title, _txt, [r[2] if r else "", aw, aw_g[2] if aw_g else ""])
        _gv = _geolocate(_vote, "", _vote, "") if _vote else None
        if _gv and _gv[3] in ment and not _geo_is_weak(_gv) and _gv[3] != r[3]:
            return _gv                                # the tiebreaker chose a different, anchored, specific scene
    # NAMESAKE MISMATCH: a SPECIFIC dot whose country the story never names, while it DOES name another
    # country, is almost always a US-town namesake matched for a foreign story ("Arab, AL" for an Israeli
    # story; "The Village, US" for a Greek island; "Hays, KS" for a Yemen clash). Let the AI arbitrate
    # those too — not just weak/None results — but only when a competing country is actually named, so a
    # plain domestic story (no foreign country in play) never triggers an extra call.
    namesake = (bool(r) and not _geo_is_weak(r) and r[3] not in ment and any(c != r[3] for c in ment))
    if not _geo_is_weak(r) and not namesake:
        return r
    place = _geolocate_ai(title, ((title or "") + ". " + (desc or "")).strip())
    if not place:
        return r
    g = _geolocate(place, "", place, "")          # ground the AI's NAME through the gazetteer
    if not g:
        return r
    # ANCHOR: the AI's country must be one the story actually mentions — never let it invent a country the
    # text never names. (When it agrees with the rules' own country, that's trivially anchored.)
    if g[3] != (r[3] if r else None) and g[3] not in ment:
        return r
    if not _geo_is_weak(g):
        return g                                  # AI pinned a specific place the rules missed
    if r is None or g[3] != r[3]:                 # AI named a country the rules missed or got wrong
        return g
    return r


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
    # "Georgia" is BOTH a country and a US state — the recurring false flag. In a US-context story (the event
    # is in the US, or the US is already a party) a bare "Georgia" is the STATE, so don't fly the country's
    # flag. SHIPPED BUG: "Hyundai's new Georgia plant" showed a 🇬🇪 country-Georgia chip next to 🇺🇸.
    _us_ctx = country == "United States of America" or "United States of America" in out
    for name in _COUNTRY_ALIAS_KEYS:
        if (" " + name + " ") in low:
            if name == "georgia" and _us_ctx:
                continue
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
             "you may also", "recommended", "advertisement", "skip to", "listen to this",
             # trailing byline / housekeeping fluff that pads the end of wire copy
             "contributed to this", "additional reporting", "with reporting by", "reporting by",
             "editing by", "this article was", "originally published", "editor's note", "editor’s note",
             "correction:", "this is a developing", "was updated", "support our journalism")


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
