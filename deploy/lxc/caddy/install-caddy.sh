#!/usr/bin/env bash
#
# Put Caddy in front of Splitter on the LXC: https (internal CA) + http on the
# standard ports, Splitter itself moved to loopback:8100. Idempotent.
#
#     ./install-caddy.sh            # run as root inside the container
#
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
say() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
[[ $EUID -eq 0 ]] || { echo "run as root" >&2; exit 1; }

if ! command -v caddy >/dev/null; then
    say "installing caddy"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -qq && apt-get install -y -qq --no-install-recommends caddy >/dev/null
fi

say "moving splitter to 127.0.0.1:8100"
sed -i 's/^HOST=.*/HOST=127.0.0.1/; s/^PORT=.*/PORT=8100/' /etc/splitter/splitter.env
grep -q '^HOST=' /etc/splitter/splitter.env || echo 'HOST=127.0.0.1' >> /etc/splitter/splitter.env
grep -q '^PORT=' /etc/splitter/splitter.env || echo 'PORT=8100' >> /etc/splitter/splitter.env

say "installing Caddyfile"
install -o root -g root -m 0644 "$SCRIPT_DIR/Caddyfile" /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile >/dev/null

systemctl restart splitter
systemctl enable --now caddy >/dev/null
systemctl reload caddy || systemctl restart caddy
sleep 2
say "listening:"; ss -ltnp | awk 'NR>1 && ($4 ~ /:(80|443|8100)$/) {print "   ", $4, $6}'
ROOT=/var/lib/caddy/.local/share/caddy/pki/authorities/local/root.crt
if [[ -f "$ROOT" ]]; then
    cp "$ROOT" /root/splitter-ca.crt
    say "CA root exported to /root/splitter-ca.crt — install it on the tablet, then open https://splitter.home.ntninja.com/"
fi
