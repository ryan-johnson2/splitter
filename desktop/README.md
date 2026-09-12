# Splitter desktop (portable)

One standalone executable per OS: a small Tauri 2 shell with the Python server
frozen inside it. No installer, nothing to set up.

```
splitter-windows-x64.exe      # double-click
splitter-macos-arm64          # chmod +x, then open (Gatekeeper: right-click → Open once)
splitter-linux-x64            # chmod +x, run
```

On launch the shell:

1. picks a data folder — `splitter-data/` **beside the executable** when that is
   writable (a true portable install: copy the exe and the folder together),
   otherwise the per-user app-data directory;
2. extracts the embedded server (a PyInstaller build of `splitter`) into
   `splitter-data/bin/` once per version and starts it with that data folder;
3. waits for the server's `SPLITTER_READY <url>` line and opens a window there.

The server binds every interface on port 8100 (an ephemeral port if 8100 is
taken), so a tablet on the same LAN can open `http://<this pc>:8100/` while the
window is up. Closing the window stops the server. Diagnostics land in
`splitter-data/splitter-desktop.log`.

## Building locally

```sh
python desktop/sidecar/build.py                     # → desktop/sidecar/dist/splitter-sidecar[.exe]
cd desktop/src-tauri && npx -y @tauri-apps/cli@2 build --no-bundle
# → desktop/src-tauri/target/release/splitter-desktop[.exe]
```

Linux needs the WebKitGTK toolchain (`libwebkit2gtk-4.1-dev libgtk-3-dev
librsvg2-dev libsoup-3.0-dev libjavascriptcoregtk-4.1-dev libssl-dev`);
Windows uses the bundled WebView2, macOS WKWebView. Add
`--tracks <path-to-velocidrone-tracks> --protect` to the sidecar build to
include the private online track client, Cython-compiled. `cargo check` without
a sidecar: `SPLITTER_ALLOW_EMPTY_SIDECAR=1`.

CI does all of this per OS in `.github/workflows/release-builds.yml` and
attaches the binaries to the GitHub Release for a `v*` tag. On Linux the sidecar is
built inside a `python:3.12-bullseye` container so it links against glibc 2.31
(2020) and runs on practically any current distro; a fully static PyInstaller
build is not possible because the bundled C extensions are shared objects, and
the manylinux interpreters lack the shared libpython PyInstaller needs. The shell needs
WebKitGTK 4.1 installed on the target machine.

## Versions and dev builds

Every artifact reports a **build stamp** (`scripts/stamp.py`, GridFPV's rule):
a build made from a clean `v1.2.3` tag reports `1.2.3`; anything else reports
`<base>-dev-<short hash>` (`-dirty` if the tree had changes). It shows in the
page footer, `/healthz` and the desktop log.

Releases and dev builds are both tags:

```
git tag v0.2.0 && git push origin v0.2.0        # release: "Splitter 0.2.0", image :latest
git tag dev-2026-09-13 && git push origin dev-2026-09-13   # prerelease: "Dev build 2026-09-13"
```

Or run *Release builds* from the Actions tab with **publish = dev** and a short
note; it cuts the next free `dev-<today>[b,c…]` tag itself and publishes a
prerelease titled `Dev build <date> (<note>)`. Docker images for dev builds are
tagged with the dev tag and `edge`.
