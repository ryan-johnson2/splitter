# Windows flags the desktop app: why, what to do, and the signing options

The portable Windows build is **not code-signed**. Two separate things follow
from that, and only one of them is fixed by a certificate.

## What users see

1. **SmartScreen: "Windows protected your PC"** on first run. Click *More info*
   → *Run anyway*. SmartScreen is a reputation system: a signed publisher with
   enough downloads stops triggering it; an unsigned binary always does.
2. **Microsoft Defender flags or quarantines the exe** on download or copy.
   Open *Windows Security → Virus & threat protection → Protection history*,
   find the Splitter entry, *Actions → Allow*, and copy the file again. This is
   a heuristic false positive, not reputation, and signing only helps it
   indirectly.

The [quick-start guide](beta-guide/index.html) walks non-technical users
through both.

## What we do about it without a certificate

- **One-dir, not one-file, PyInstaller build** (since 0.5.2). A one-file exe
  unpacks itself into a temp folder on every launch, which is exactly what
  packers and droppers do and is the main reason unsigned PyInstaller builds
  trip Defender. The sidecar is now an executable plus an `_internal/` folder,
  shipped inside the shell as a tar.gz and unpacked **once per version** into
  `splitter-data/bin/splitter-sidecar-<version>/` (`desktop/sidecar/build.py`,
  `desktop/src-tauri/src/lib.rs`).
- **No UPX.** Compressed executables are another classic heuristic hit; the
  spec sets `upx=False` everywhere.
- **Report false positives per release** at
  <https://www.microsoft.com/en-us/wdsi/filesubmission>. Detections on that
  hash usually clear within a day or two.

## Why the obvious shortcuts do not work

- **Let's Encrypt** only issues TLS server certificates; they carry no
  code-signing key usage and Windows rejects them for Authenticode. They have
  said they will never offer code signing.
- **Self-signed** is no better than unsigned. SmartScreen and the publisher
  dialog only trust chains to a root in the Windows store, nobody will import
  ours, and a signature that fails validation can look worse to Defender than
  none.
- **EV certificates** used to skip SmartScreen outright; Microsoft removed that
  instant reputation around 2024, so EV buys little over OV now.

## Signing routes

| Route | Cost | Notes |
|---|---|---|
| **SignPath Foundation** | free for open source | Apply with the repo; a GitHub Action submits the CI build; the signature uses their certificate with the project named in it, and reputation accrues across their projects. **Catch:** the signed artifact must be built entirely from public source in public CI. Our Windows build embeds the private `velocidrone-tracks` client, so the signable build would be the one *without* it (the picker is already optional) — or the client goes public. |
| Azure Trusted Signing | ~$10/month | Identity validation; individuals need several years of verifiable history and only some countries are open. Short-lived certificates, timestamped signatures, good SmartScreen treatment. Tauri hooks it via `bundle.windows.signCommand`. |
| Certum open-source certificate | ~€70/year | For OSS authors; delivered on a card or cloud HSM (hardware keys are mandatory for all code-signing certificates since June 2023). |
| Sectigo / SSL.com OV | $200–400/year | The standard commercial route. |

Whichever route: **sign both binaries.** The shell unpacks the sidecar and runs
it, so an unsigned sidecar gets flagged on its own at that point. Sign
`splitter-sidecar.exe` (and ideally the `.pyd`/`.dll` files beside it) before
`build.py` packs the folder, then sign `splitter-desktop.exe` after the Tauri
build. Both SignPath and Trusted Signing have GitHub Actions that slot into
`.github/workflows/release-builds.yml`. Even a correctly signed first release
warns in SmartScreen until reputation builds, so keep submitting false-positive
reports for the first few.

macOS has the same story with Gatekeeper (notarisation needs an Apple Developer
account, US$99/year); right-click → *Open* once is the workaround.
