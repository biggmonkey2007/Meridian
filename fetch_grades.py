"""Build country_grades.json — an authoritative political grading for EVERY country, so the app never
relies on hand-guessed 'political leaning' again.

Source: V-Dem (Varieties of Democracy) via Our World in Data — the standard academic regime dataset.
  * Regimes of the World: 0 Closed autocracy | 1 Electoral autocracy | 2 Electoral democracy | 3 Liberal democracy
  * Electoral democracy index (0-1): a continuous 'how democratic' score.
Takes the LATEST year available per country. Re-run yearly:  python fetch_grades.py
"""
import csv, io, json, urllib.request, datetime

OWID = "https://ourworldindata.org/grapher/%s.csv?csvType=full"
REGIME = {"0": "Closed autocracy", "1": "Electoral autocracy",
          "2": "Electoral democracy", "3": "Liberal democracy"}


def _get(slug):
    req = urllib.request.Request(OWID % slug, headers={"User-Agent": "Meridian/1.0"})
    return list(csv.DictReader(io.StringIO(urllib.request.urlopen(req, timeout=60).read().decode("utf-8"))))


def _latest(rows, col):
    """{ISO3: (value, year, name)} taking the most recent non-empty value per country."""
    out = {}
    for r in rows:
        code = (r.get("Code") or "").strip()
        val = (r.get(col) or "").strip()
        if not code or len(code) != 3 or not val:
            continue
        yr = int(r.get("Year") or 0)
        if code not in out or yr > out[code][1]:
            out[code] = (val, yr, r.get("Entity") or "")
    return out


def _norm(name):
    import re
    return re.sub(r"[^a-z]", "", (name or "").lower())


regime = _latest(_get("political-regime"), "Political regime")
edi = _latest(_get("electoral-democracy-index"), "Electoral democracy index")

grades, byname = {}, {}
for code, (rv, yr, name) in regime.items():
    n = int(rv)
    entry = {
        "name": name,
        "regime": REGIME.get(rv, "?"),          # e.g. "Electoral autocracy"
        "regime_n": n,                           # 0..3
        "camp": "Authoritarian" if n <= 1 else "Democratic",
        "year": yr,
    }
    if code in edi:
        try:
            entry["edi"] = round(float(edi[code][0]), 3)   # 0..1 democracy score
        except ValueError:
            pass
    grades[code] = entry
    byname[_norm(name)] = code

# common name variants the app might use
ALIAS = {"unitedstatesofamerica": "USA", "unitedstates": "USA", "russia": "RUS", "southkorea": "KOR",
         "northkorea": "PRK", "czechia": "CZE", "czechrepublic": "CZE", "turkey": "TUR", "turkiye": "TUR",
         "burma": "MMR", "myanmar": "MMR", "drcongo": "COD", "democraticrepublicofcongo": "COD",
         "republicofthecongo": "COG", "congo": "COG", "ivorycoast": "CIV", "cotedivoire": "CIV",
         "unitedkingdom": "GBR", "britain": "GBR", "uae": "ARE", "unitedarabemirates": "ARE",
         "iran": "IRN", "syria": "SYR", "laos": "LAO", "vietnam": "VNM", "cape verde": "CPV",
         "eswatini": "SWZ", "swaziland": "SWZ", "northmacedonia": "MKD", "macedonia": "MKD"}
for k, v in ALIAS.items():
    if v in grades:
        byname[_norm(k)] = v

out = {"generated": datetime.datetime.utcnow().strftime("%Y-%m-%d"),
       "source": "V-Dem (Varieties of Democracy) via Our World in Data",
       "grades": grades, "byname": byname}
json.dump(out, open("country_grades.json", "w", encoding="utf-8"), ensure_ascii=False)
print("wrote country_grades.json — %d countries (latest year %s)"
      % (len(grades), max(e["year"] for e in grades.values())))
