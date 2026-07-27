"""Augment cities_gaz.json with small towns (population 1,000-15,000) from GeoNames cities1000, so the map
can pin places like Kyrylivka (a ~1,400-person Azov Sea resort) that only appear in an article body.

The existing 31k-city gazetteer (pop >= 15,000) is KEPT as-is; we only ADD smaller towns that aren't
already present. Small single-word towns are auto-treated as "weak" by the app (they only dot when the
sentence explicitly locates them), so the extra names don't become false-positive dots.

Re-run:  python fetch_cities.py     (writes cities_gaz.json in place; commit the result)
"""
import urllib.request, zipfile, io, json, re, os, sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import app  # for _fold and the app's canonical country names

GEO = "https://download.geonames.org/export/dump/%s"
APP_NAMES = set(app.COUNTRY_COORDS.keys())
POP_MIN, POP_MAX = 1000, 15000        # only ADD this band; >=15k is already in the file


def _get(fname):
    return urllib.request.urlopen(urllib.request.Request(GEO % fname, headers={"User-Agent": "Meridian/1.0"}),
                                  timeout=60).read()


def fold_key(name):
    """Match the app's own tokenisation exactly: fold accents, lower-case, keep [a-z0-9] words."""
    return " ".join(re.findall(r"[a-z0-9]+", app._fold(name).lower()))


# GeoNames country name -> the app's country name, only where they differ.
RECON = {
    "United States": "United States of America", "Czech Republic": "Czechia", "Czechia": "Czechia",
    "South Korea": "South Korea", "North Korea": "North Korea", "Korea, South": "South Korea",
    "Korea, North": "North Korea", "Cote d'Ivoire": "Ivory Coast", "Ivory Coast": "Ivory Coast",
    "Congo, Dem. Rep.": "Democratic Republic of the Congo",
    "Democratic Republic of the Congo": "Democratic Republic of the Congo",
    "Congo Republic": "Republic of the Congo", "Republic of the Congo": "Republic of the Congo",
    "Congo": "Republic of the Congo", "Russia": "Russia", "Turkey": "Turkey",
    "Myanmar": "Myanmar", "Burma": "Myanmar", "Cape Verde": "Cape Verde", "Cabo Verde": "Cape Verde",
    "Swaziland": "Eswatini", "Eswatini": "Eswatini", "Macedonia": "North Macedonia",
    "North Macedonia": "North Macedonia", "East Timor": "Timor-Leste", "Timor-Leste": "Timor-Leste",
    "Palestinian Territory": "Palestine", "Palestine": "Palestine", "Vatican": "Vatican City",
    "The Bahamas": "Bahamas", "The Gambia": "Gambia", "Syria": "Syria", "Laos": "Laos",
    "Moldova": "Moldova", "Tanzania": "Tanzania", "Vietnam": "Vietnam", "Iran": "Iran",
    "Bolivia": "Bolivia", "Venezuela": "Venezuela", "Brunei": "Brunei", "Micronesia": "Micronesia",
}


def iso2_to_app():
    txt = _get("countryInfo.txt").decode("utf-8")
    out, miss = {}, []
    for ln in txt.splitlines():
        if ln.startswith("#") or not ln.strip():
            continue
        f = ln.split("\t")
        if len(f) < 5 or not f[0]:
            continue
        iso2, gname = f[0], f[4]
        appname = RECON.get(gname, gname)
        if appname in APP_NAMES:
            out[iso2] = appname
        else:
            miss.append((iso2, gname))
    if miss:
        print("  UNMAPPED countries (cities in these are skipped):", ", ".join(f"{i}:{n}" for i, n in miss))
    return out


def main():
    print("1/4  country map ...")
    iso = iso2_to_app()
    print(f"     mapped {len(iso)} ISO2 codes to app country names")

    print("2/4  loading existing gazetteer ...")
    gaz = json.load(open("cities_gaz.json", encoding="utf-8"))
    have = set(gaz.keys())
    print(f"     existing: {len(gaz)} names")

    print("3/4  downloading + parsing cities1000 ...")
    if os.path.exists("cities1000.zip"):          # reuse a prior download
        data = open("cities1000.zip", "rb").read()
    else:
        data = _get("cities1000.zip")
        open("cities1000.zip", "wb").write(data)
    txt = zipfile.ZipFile(io.BytesIO(data)).read("cities1000.txt").decode("utf-8")
    alt_ok = re.compile(r"^[A-Za-z][A-Za-z '\-]{2,24}$")   # a plausible Latin spelling, not a code/URL
    added = 0
    for ln in txt.splitlines():
        f = ln.split("\t")
        if len(f) < 15 or f[6] != "P":           # feature class P = populated place
            continue
        try:
            pop = int(f[14] or 0)
        except ValueError:
            continue
        if not (POP_MIN <= pop < POP_MAX):
            continue
        co = iso.get(f[8])
        if not co:
            continue
        try:
            lat, lng = round(float(f[4]), 4), round(float(f[5]), 4)
        except ValueError:
            continue
        # name + asciiname ONLY. (Alternate spellings were tried but pulled in common-word junk like
        # "Port"/"The City" that dotted the wrong place — the cost outweighed the transliteration benefit.)
        for nm in (f[1], f[2]):
            k = fold_key(nm)
            if not k or len(k) < 3:
                continue
            if k in have:                         # never touch an existing (>=15k) entry
                continue
            lst = gaz.setdefault(k, [])
            if any(c[2] == co for c in lst):      # one entry per country per name (keep the biggest)
                for c in lst:
                    if c[2] == co and pop > c[3]:
                        c[0], c[1], c[3] = lat, lng, pop
                continue
            lst.append([lat, lng, co, pop])
            lst.sort(key=lambda c: -c[3])
            added += 1

    print("4/4  writing cities_gaz.json ...")
    json.dump(gaz, open("cities_gaz.json", "w", encoding="utf-8"), ensure_ascii=False)
    print(f"done. added {added} small-town entries; total names now {len(gaz)}")


if __name__ == "__main__":
    main()
