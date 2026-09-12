#!/usr/bin/env bash
#
# Provision Splitter into a Debian LXC container (Proxmox CT).
# Idempotent: re-run it with newer wheels to upgrade in place.
#
# Usage (as root, inside the container):
#     ./install.sh [--wheels DIR] [--no-start]
#
set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

APP_USER=splitter
APP_ROOT=/opt/splitter
VENV="$APP_ROOT/venv"
DATA_DIR=/var/lib/splitter
CONF_DIR=/etc/splitter
ENV_FILE="$CONF_DIR/splitter.env"
UNIT=/etc/systemd/system/splitter.service

WHEEL_DIR=""
START=1

while [[ $# -gt 0 ]]; do
    case "$1" in
        --wheels)   WHEEL_DIR="$2"; shift 2 ;;
        --no-start) START=0; shift ;;
        -h|--help)  sed -n '2,9p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *)          echo "unknown argument: $1" >&2; exit 2 ;;
    esac
done

die() { echo "error: $*" >&2; exit 1; }
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

[[ $EUID -eq 0 ]] || die "run as root (inside the container)"

# ── locate the payload ───────────────────────────────────────────────
if [[ -z "$WHEEL_DIR" ]]; then
    for candidate in "$SCRIPT_DIR" "$SCRIPT_DIR/dist"; do
        if compgen -G "$candidate/splitter-*.whl" >/dev/null; then
            WHEEL_DIR="$candidate"; break
        fi
    done
fi
[[ -n "$WHEEL_DIR" ]] || die "no splitter-*.whl found; run build-bundle.sh first"

mapfile -t WHEELS < <(compgen -G "$WHEEL_DIR/*.whl" || true)
[[ ${#WHEELS[@]} -gt 0 ]] || die "no wheels in $WHEEL_DIR"
compgen -G "$WHEEL_DIR/velocidrone_ws-*.whl" >/dev/null \
    || die "velocidrone_ws-*.whl missing from $WHEEL_DIR (splitter depends on it and it is not on PyPI)"
compgen -G "$WHEEL_DIR/velocidrone_tracks-*.whl" >/dev/null \
    || echo "note: no velocidrone_tracks wheel in the bundle — online track picker disabled (manual ids only)"

ENV_TEMPLATE=""
for candidate in "$SCRIPT_DIR/splitter.env.example" "$SCRIPT_DIR/../../.env.example"; do
    [[ -f "$candidate" ]] && { ENV_TEMPLATE="$candidate"; break; }
done

UNIT_SRC="$SCRIPT_DIR/splitter.service"
[[ -f "$UNIT_SRC" ]] || die "splitter.service not found next to install.sh"

# ── system prerequisites ─────────────────────────────────────────────
PY=$(command -v python3) || die "python3 not installed"
"$PY" - <<'PYCHECK' || die "Python 3.11+ required (found $($PY --version))"
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PYCHECK

if ! "$PY" -c 'import ensurepip' 2>/dev/null; then
    say "installing python3-venv"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq
    apt-get install -y --no-install-recommends python3-venv ca-certificates
    "$PY" -c 'import ensurepip' 2>/dev/null \
        || die "python3-venv installed but ensurepip is still missing"
fi

# ── user, directories ────────────────────────────────────────────────
if ! id -u "$APP_USER" >/dev/null 2>&1; then
    say "creating system user $APP_USER"
    useradd --system --home-dir "$DATA_DIR" --shell /usr/sbin/nologin "$APP_USER"
fi

install -d -o root        -g root        -m 0755 "$APP_ROOT"
install -d -o "$APP_USER" -g "$APP_USER" -m 0750 "$DATA_DIR"
install -d -o root        -g "$APP_USER" -m 0750 "$CONF_DIR"

# ── virtualenv + packages ────────────────────────────────────────────
if [[ ! -x "$VENV/bin/python" ]]; then
    say "creating virtualenv at $VENV"
    "$PY" -m venv "$VENV"
fi

say "installing wheels"
"$VENV/bin/pip" install --quiet --no-input --upgrade pip
"$VENV/bin/pip" install --no-input --upgrade --upgrade-strategy only-if-needed "${WHEELS[@]}"

INSTALLED_VERSION=$("$VENV/bin/python" -c \
    'import importlib.metadata as m; print(m.version("splitter"))' 2>/dev/null || echo unknown)
say "splitter $INSTALLED_VERSION installed"

# ── configuration ────────────────────────────────────────────────────
if [[ ! -f "$ENV_FILE" ]]; then
    say "seeding $ENV_FILE"
    if [[ -n "$ENV_TEMPLATE" ]]; then
        install -o root -g "$APP_USER" -m 0640 "$ENV_TEMPLATE" "$ENV_FILE"
    else
        install -o root -g "$APP_USER" -m 0640 /dev/null "$ENV_FILE"
    fi
    # The DB lives outside the app tree; four slashes = absolute path.
    sed -i '/^DATABASE_URL=/d' "$ENV_FILE"
    printf '\n# Managed by deploy/lxc/install.sh\nDATABASE_URL=sqlite+aiosqlite:////var/lib/splitter/splitter.db\n' >> "$ENV_FILE"
else
    say "keeping existing $ENV_FILE"
    chown root:"$APP_USER" "$ENV_FILE"
    chmod 0640 "$ENV_FILE"
fi

# ── service ──────────────────────────────────────────────────────────
say "installing systemd unit"
install -o root -g root -m 0644 "$UNIT_SRC" "$UNIT"
systemctl daemon-reload
systemctl enable splitter.service >/dev/null

if [[ $START -eq 0 ]]; then
    say "skipping start (--no-start)"
else
    say "restarting splitter.service"
    systemctl restart splitter.service
    sleep 2
    systemctl --no-pager --lines=0 status splitter.service || true
    IP=$(hostname -I 2>/dev/null | awk '{print $1}')
    echo
    echo "Open http://${IP:-<container-ip>}:8100/settings and enter the gaming PC's LAN IP."
fi
