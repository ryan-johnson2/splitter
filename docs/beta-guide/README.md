# Beta quick-start guide

`index.html` is the source of `Splitter-beta-quick-start.pdf`, the sheet handed to
Windows beta testers with `splitter-windows-x64.exe`. Screenshots sit beside it.
The two dashed placeholders in section 2 are for in-game screenshots of
*Options → Main Settings* (Websocket Communication / Websocket IMU).

Regenerate the PDF with headless Chromium (no local install needed):

```sh
docker run --rm -v "$PWD/docs/beta-guide:/g" zenika/alpine-chrome --headless --no-sandbox \
  --disable-gpu --no-pdf-header-footer --print-to-pdf=/g/Splitter-beta-quick-start.pdf file:///g/index.html
```
