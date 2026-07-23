# Auto-update (GitHub Releases)

Meridian can update itself from your GitHub Releases. It's **built and dormant** — it turns on the moment
you point it at a repo. No update check runs until then.

## How it works
On launch the installed `Meridian.exe` asks GitHub for your repo's **latest release**. If the release tag
(e.g. `v1.1.0`) is newer than the running `APP_VERSION`, a small **"Update to vX.Y.Z"** pill appears.
Clicking it downloads the new `Meridian.exe`, waits for the app to close, swaps the file in, and relaunches.

## Turn it on (one time)
1. Create a GitHub repo for Meridian (public or private-with-a-token works; public is simplest).
2. Tell Meridian the repo, either way:
   - **No rebuild:** create a text file `%LOCALAPPDATA%\Meridian\update_repo.txt` containing just `owner/name`
     (e.g. `mintahockey/meridian`). The installed app reads it on next launch.
   - **Or at build time:** set `UPDATE_REPO_DEFAULT = "owner/name"` in `app.py` and rebuild.

## Publish a new version
1. Bump `APP_VERSION` in `app.py` (e.g. `"1.1.0"`).
2. Run `release.ps1` (needs the GitHub CLI `gh`, authenticated). It builds the exe and creates a Release
   tagged `v1.1.0` with `Meridian.exe` attached.
   - No `gh`? Build with `build_exe.ps1`, then on GitHub make a Release tagged `v1.1.0` and upload
     `dist\Meridian.exe` as an asset.
3. Installed copies show the Update pill within a few seconds of their next launch.

## Publish with zero local build (GitHub Actions — the smooth path)
`.github/workflows/release.yml` builds the exe on a cloud Windows runner and publishes the Release for
you, so you never run the 4-minute local build again. After the one-time setup below:
1. Bump `APP_VERSION` in `app.py`, commit, push.
2. Tag and push it: `git tag v1.2.0 && git push origin v1.2.0`.
3. The workflow builds `Meridian.exe` and attaches it to a Release named after the tag. Installed copies
   pick it up on next launch. (Keep the tag == `v<APP_VERSION>`.)

## One-time GitHub setup
1. Create a **public** GitHub repo (public = token-free auto-update; a private repo would need a token
   embedded in the app to download releases).
2. Push this folder to it, then point the app at the repo by setting
   `UPDATE_REPO_DEFAULT = "owner/name"` in `app.py` (baked into every future build). To flip on the
   *already-installed* copy without rebuilding, drop `owner/name` into `%LOCALAPPDATA%\Meridian\update_repo.txt`.

## Notes
- The release **must** attach a `.exe` asset (the updater looks for the first `*.exe`).
- Update checks are cached 6h, so they don't hit GitHub on every launch.
- Only the packaged `.exe` updates — running from `python app.py` never does.
- Secrets are gitignored (`tg_session*`, `telegram*.txt`, `*.key`, …) — verify with
  `git ls-files | grep -i session` returning nothing before the first push.
