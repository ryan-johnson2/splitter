# Beta quick-start guide

`index.html` is the source of `Splitter-beta-quick-start.pdf`, the sheet handed to
Windows beta testers with `splitter-windows-x64.exe`. Screenshots sit beside it.
`game-settings.png` is the in-game *Options → Main Settings* screen; its callouts are CSS overlays
in `index.html` (percent positions), so a new screenshot needs the boxes nudged.

Regenerate the PDF with headless Chromium (no local install needed):

```sh
docker run --rm -v "$PWD/docs/beta-guide:/g" zenika/alpine-chrome --headless --no-sandbox \
  --disable-gpu --no-pdf-header-footer --print-to-pdf=/g/Splitter-beta-quick-start.pdf file:///g/index.html
```
