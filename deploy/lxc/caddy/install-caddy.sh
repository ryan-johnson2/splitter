#!/usr/bin/env bash
#
# Put Caddy in front of Splitter on the LXC: https (certificate from your own
# ACME CA — step-ca, say) + http on the standard ports, Splitter itself moved
# to loopback:8100. Idempotent.
#
#     ACME_CA=https://ca.example.lan/acme/acme/directory \
#     ROOTS_URL=https://ca.example.lan/roots.pem \
#     ROOT_SHA256=<sha-256 of that root, hex, no colons> \
#     ./install-caddy.sh [hostname]   # run as root inside the container
#
# The hostname (default: `hostname -f`) is the name the https certificate is
# issued for; open Splitter by that name on the tablet. The CA must be able to
# reach the box on 443 (TLS-ALPN-01) or 80 (HTTP-01).
#
#     ACME_CA      the CA's ACME directory URL                      (required)
#     ROOTS_URL    where to fetch the CA's root certificate (PEM)   (required)
#     ROOT_SHA256  the expected SHA-256 fingerprint of that root    (required)
#                  — the download is refused on a mismatch; this pin is what
#                  makes fetching the root over the network safe
#     ACME_EMAIL   ACME account email                               (optional)
#
# Nothing here names a particular CA on purpose: this file is public, your CA
# is not. Keep the three values in your own notes.
#
set -euo pipefail
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
HOST="${1:-$(hostname -f 2>/dev/null || hostname)}"
ACME_EMAIL="${ACME_EMAIL:-}"
for v in ACME_CA ROOTS_URL ROOT_SHA256; do
    [[ -n "${!v:-}" ]] || { echo "set $v (see the header of this script)" >&2; exit 1; }
done
ROOT_SHA256="$(tr -d ': ' <<<"$ROOT_SHA256" | tr 'A-F' 'a-f')"
ROOT_CRT=/usr/local/share/ca-certificates/splitter-acme-root.crt
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

say "trusting the CA root from $ROOTS_URL"
# Fetched without TLS verification on purpose: the CA's own site is signed by
# the very root we are about to install. The SHA-256 pin is the check.
curl -sSk "$ROOTS_URL" -o /tmp/splitter-acme-root.pem
FP=$(openssl x509 -in /tmp/splitter-acme-root.pem -noout -fingerprint -sha256 | cut -d= -f2 | tr -d : | tr 'A-F' 'a-f')
[[ "$FP" == "$ROOT_SHA256" ]] || { echo "root fingerprint mismatch: got $FP, expected $ROOT_SHA256" >&2; exit 1; }
install -o root -g root -m 0644 /tmp/splitter-acme-root.pem "$ROOT_CRT"
update-ca-certificates >/dev/null
install -d -o root -g root -m 0755 /etc/caddy/public
install -o root -g root -m 0644 /tmp/splitter-acme-root.pem /etc/caddy/public/splitter-ca.crt
rm -f /tmp/splitter-acme-root.pem

say "installing Caddyfile for $HOST (ACME at $ACME_CA)"
EMAIL_LINE="email $ACME_EMAIL"; [[ -n "$ACME_EMAIL" ]] || EMAIL_LINE="# no account email"
sed -e "s/__HOST__/$HOST/g" -e "s|__ACME_CA__|$ACME_CA|g" -e "s|\temail __EMAIL__|\t$EMAIL_LINE|" \
    "$SCRIPT_DIR/Caddyfile" > /etc/caddy/Caddyfile
chown root:root /etc/caddy/Caddyfile && chmod 0644 /etc/caddy/Caddyfile
caddy validate --config /etc/caddy/Caddyfile >/dev/null

systemctl restart splitter
systemctl enable --now caddy >/dev/null
# restart, not reload: Go loads the system trust store once per process
systemctl restart caddy
sleep 4
say "listening:"; ss -ltnp | awk 'NR>1 && ($4 ~ /:(80|443|8100)$/) {print "   ", $4, $6}'
say "certificate:"; echo | openssl s_client -connect 127.0.0.1:443 -servername "$HOST" 2>/dev/null \
    | openssl x509 -noout -issuer -dates | sed 's/^/    /'
say "CA root at http://$HOST/splitter-ca.crt — install it on the tablet, then open https://$HOST/"
