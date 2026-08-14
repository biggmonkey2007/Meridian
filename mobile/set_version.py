"""Sync the mobile app version to the ONE source of truth — APP_VERSION in ../app.py — so the desktop and
the store builds never drift. Patches:
  - mobile/package.json               "version"
  - android/app/build.gradle          versionName + versionCode   (only if android/ exists)
  - ios/App/App/Info.plist            CFBundleShortVersionString + CFBundleVersion  (only if ios/ exists)

versionCode / CFBundleVersion is an integer build number derived from the version (1.4.6 -> 10406), so every
store upload is strictly increasing. Run after `npx cap add ...` and before each store release:

    python set_version.py
"""
import os
import re
import json

HERE = os.path.dirname(os.path.abspath(__file__))


def app_version():
    src = open(os.path.join(HERE, "..", "app.py"), encoding="utf-8").read()
    m = re.search(r'APP_VERSION\s*=\s*"([\d.]+)"', src)
    return m.group(1) if m else "1.0.0"


def build_number(ver):
    a, b, c = (ver.split(".") + ["0", "0", "0"])[:3]
    return int(a) * 10000 + int(b) * 100 + int(c)


def main():
    ver = app_version()
    code = build_number(ver)

    pj = os.path.join(HERE, "package.json")
    d = json.load(open(pj, encoding="utf-8"))
    d["version"] = ver
    with open(pj, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2)
        f.write("\n")
    print("package.json         ->", ver)

    gradle = os.path.join(HERE, "android", "app", "build.gradle")
    if os.path.exists(gradle):
        t = open(gradle, encoding="utf-8").read()
        t = re.sub(r'versionName\s+"[^"]*"', 'versionName "%s"' % ver, t)
        t = re.sub(r"versionCode\s+\d+", "versionCode %d" % code, t)
        open(gradle, "w", encoding="utf-8").write(t)
        print("android build.gradle ->", ver, "(code %d)" % code)

    plist = os.path.join(HERE, "ios", "App", "App", "Info.plist")
    if os.path.exists(plist):
        t = open(plist, encoding="utf-8").read()
        t = re.sub(r"(<key>CFBundleShortVersionString</key>\s*<string>)[^<]*(</string>)",
                   r"\g<1>%s\g<2>" % ver, t)
        t = re.sub(r"(<key>CFBundleVersion</key>\s*<string>)[^<]*(</string>)",
                   r"\g<1>%d\g<2>" % code, t)
        open(plist, "w", encoding="utf-8").write(t)
        print("ios Info.plist       ->", ver, "(build %d)" % code)


if __name__ == "__main__":
    main()
