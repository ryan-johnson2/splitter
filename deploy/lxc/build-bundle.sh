#!/usr/bin/env bash
#
# Build the LXC deployment bundle: wheels for splitter and the vendored
# velocidrone-ws (not on PyPI), plus — when available — the private
# velocidrone-tracks wheel that enables the online track picker, plus the
# installer, unit file and env template, tarred into one artifact.
#
# velocidrone-tracks is looked for at $TRACKS_SRC, else
# ../velocidrone-libraries/velocidrone-tracks, else ./wheels/velocidrone_tracks-*.whl;
# absent all three the bundle still builds (picker disabled, manual ids only).
#
# Usage (from anywhere):
#     deploy/lxc/build-bundle.sh
# Produces:
#     deploy/lxc/dist/splitter-lxc-<version>.tar.gz
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd -- "$SCRIPT_DIR/../.." && pwd)"
WS_SRC="$REPO_ROOT/libs/velocidrone-ws"
TRACKS_SRC="${TRACKS_SRC:-$REPO_ROOT/../velocidrone-libraries/velocidrone-tracks}"
DIST="$SCRIPT_DIR/dist"

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[[ -f "$REPO_ROOT/pyproject.toml" ]] || die "cannot locate the splitter repo root"
[[ -f "$WS_SRC/pyproject.toml" ]] || die "vendored velocidrone-ws not found at $WS_SRC"

rm -rf "$DIST"
mkdir -p "$DIST"

# ── pick a build frontend ────────────────────────────────────────────
# Docker is preferred (nothing is installed on the host); uv/hatch next;
# else an ephemeral venv rather than touching system pip.
BUILD_VENV=""
PARENT="$(cd -- "$REPO_ROOT/.." && pwd)"  # mount the parent so ../velocidrone-libraries is reachable too
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    say "building with docker (python:3.12-slim)"
    build_wheel() {
        local rel="${1#"$PARENT"/}"
        docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
            -v "$PARENT:/work" -w /work python:3.12-slim sh -c "
                pip install --quiet --user --disable-pip-version-check build hatchling >/dev/null &&
                python -m build --wheel --no-isolation --outdir /work/${DIST#"$PARENT"/} /work/$rel >/dev/null"
    }
elif command -v uv >/dev/null 2>&1; then
    build_wheel() { uv build --wheel --out-dir "$DIST" "$1" >/dev/null; }
    say "building with uv"
elif command -v hatch >/dev/null 2>&1; then
    build_wheel() { (cd "$1" && hatch build --target wheel "$DIST" >/dev/null); }
    say "building with hatch"
else
    BUILD_VENV="$(mktemp -d)/buildenv"
    say "no uv/hatch found — bootstrapping a throwaway build venv"
    python3 -m venv "$BUILD_VENV" >/dev/null 2>&1 || die "python3 -m venv failed; install uv or python3-venv and re-run"
    "$BUILD_VENV/bin/pip" install --quiet --upgrade pip build hatchling
    build_wheel() { "$BUILD_VENV/bin/python" -m build --wheel --outdir "$DIST" "$1" >/dev/null; }
fi

say "building velocidrone-ws wheel"
build_wheel "$WS_SRC"
if [[ -f "$TRACKS_SRC/pyproject.toml" ]]; then
    say "building velocidrone-tracks wheel (online track picker)"
    build_wheel "$TRACKS_SRC"
elif compgen -G "$REPO_ROOT/wheels/velocidrone_tracks-*.whl" >/dev/null; then
    say "using prebuilt velocidrone-tracks wheel"
    cp "$REPO_ROOT"/wheels/velocidrone_tracks-*.whl "$DIST/"
else
    say "velocidrone-tracks not available — bundle will have no online track picker"
fi
STAMP="$(python3 "$REPO_ROOT/scripts/stamp.py" --write ${SPLITTER_BUILD:+--stamp "$SPLITTER_BUILD"})"
say "build stamp: $STAMP"
say "building splitter wheel"
build_wheel "$REPO_ROOT"

[[ -n "$BUILD_VENV" ]] && rm -rf "$(dirname "$BUILD_VENV")"

# ── stage the rest of the payload ────────────────────────────────────
install -m 0755 "$SCRIPT_DIR/install.sh"       "$DIST/install.sh"
install -m 0644 "$SCRIPT_DIR/splitter.service" "$DIST/splitter.service"
install -m 0644 "$REPO_ROOT/.env.example"      "$DIST/splitter.env.example"

VERSION="$(python3 - "$REPO_ROOT/pyproject.toml" <<'PY'
import re, sys, pathlib
text = pathlib.Path(sys.argv[1]).read_text()
print(re.search(r'^version\s*=\s*"([^"]+)"', text, re.M).group(1))
PY
)"

echo "$STAMP" > "$DIST/BUILD.txt"
BUNDLE="$DIST/splitter-lxc-$STAMP.tar.gz"
tar -czf "$BUNDLE" -C "$DIST" \
    --transform "s,^,splitter-lxc-$STAMP/," \
    install.sh splitter.service splitter.env.example BUILD.txt \
    $(cd "$DIST" && ls ./*.whl | sed 's,^\./,,')

say "bundle ready: $BUNDLE"
echo
echo "Copy it to the container and run:"
echo "    tar xzf splitter-lxc-$STAMP.tar.gz && cd splitter-lxc-$STAMP && ./install.sh"
