# Splitter on Proxmox LXC

Docker stays the fast-iteration path (`docker compose up -d` from the repo
root). This directory is the long-term hosting path: a plain Debian CT with
Splitter in a virtualenv under systemd — the same shape as Marshal's deploy.

The unit of deployment is a **bundle**: one tarball with the wheels (`splitter`,
the vendored `velocidrone-ws`, and — in release bundles — the private
`velocidrone-tracks` client that powers the online track picker), the installer, the
systemd unit and the env template.

```
build-bundle.sh    # on your workstation: builds dist/splitter-lxc-<version>.tar.gz
install.sh         # inside the container: provisions or upgrades in place
splitter.service   # the systemd unit
```

## 0. What this install is for

An LXC Splitter is a good **web**: the one place every run lives. After
installing, open Settings → *Receive runs from other Splitters* → *Turn
receiving on*, and give the token to each Splitter that records (the desktop
app on the gaming PC): Settings → *Send runs to another Splitter*. Put Caddy
(`caddy/`) in front so the token travels over https; a plain-http LAN address
is accepted too.

## 1. Build the bundle

```sh
deploy/lxc/build-bundle.sh
# -> deploy/lxc/dist/splitter-lxc-<version>.tar.gz
```

Or skip the build and take `splitter-lxc-<version>.tar.gz` from the
[GitHub Release](../../../../releases/latest): the release bundles also carry
the private online track picker client, which a local build does not have.

Wheels are built inside a `python:3.12-slim` container when Docker is available
(nothing Python-related is needed on the host). `dist/` is gitignored.

## 2. Create the container

On the Proxmox host — adjust the ID, storage and bridge:

```sh
pct create 111 local:vztmpl/debian-13-standard_13.1-2_amd64.tar.zst \
    --hostname splitter \
    --cores 1 --memory 512 --swap 256 \
    --rootfs local-lvm:4 \
    --net0 name=eth0,bridge=vmbr0,ip=dhcp \
    --unprivileged 1 --features nesting=0 --onboot 1 --start 1
```

One asyncio process over SQLite; 1 core / 512 MB is plenty. The container must
be able to reach the gaming PC's LAN IP on TCP 60003 (same VLAN, or a route).

## 3. Install

```sh
V=0.5.1
pct push 111 deploy/lxc/dist/splitter-lxc-$V.tar.gz /root/splitter-lxc-$V.tar.gz
pct exec 111 -- bash -c "cd /root && tar xzf splitter-lxc-$V.tar.gz && cd splitter-lxc-$V && ./install.sh"
```

The installer creates a `splitter` system user, a venv at `/opt/splitter/venv`,
the data directory `/var/lib/splitter` and `/etc/splitter/splitter.env`, then
starts the service. There are no secrets, so it starts on the first install.

Then on the tablet open `http://<container-ip>:8100/settings`, enter the gaming
PC's **LAN IP** (the game never listens on localhost) and save. In the game,
turn on *Options → Main Settings → Websocket Communication* (and *Websocket
IMU* for telemetry). The header dot goes green when the game is reachable.

Health: `/healthz`. Logs: `journalctl -u splitter -f`.

To serve on port 80 instead of 8100 (it is the only service on the box), set
`PORT=80` in `/etc/splitter/splitter.env` and restart; the unit carries
`CAP_NET_BIND_SERVICE` so the unprivileged `splitter` user may bind it.

## 4. Upgrades

Re-running the installer is the upgrade. It keeps the env file and database,
replaces the wheels and restarts the unit:

```sh
V=0.5.1   # the new version
pct push 111 deploy/lxc/dist/splitter-lxc-$V.tar.gz /root/splitter-lxc-$V.tar.gz
pct exec 111 -- bash -c "cd /root && tar xzf splitter-lxc-$V.tar.gz && cd splitter-lxc-$V && ./install.sh"
```

Do not upgrade while a run is on: the restart cuts it. `/api/state` shows
`"race": null` when it is safe.

The schema is `create_all` at startup plus automatic `ADD COLUMN` for new
nullable/defaulted columns, so additive upgrades are safe. Snapshot the CT
before an upgrade that renames or drops columns.

## 5. Backups

SQLite in WAL mode; for a clean copy stop the service first:

```sh
pct exec 111 -- systemctl stop splitter
vzdump 111 --storage local --mode stop
pct exec 111 -- systemctl start splitter
```

The database is the only state (races, gate times, telemetry, the raw frame
log). `runuser -u splitter -- /opt/splitter/venv/bin/splitter races` lists
recent runs from inside the container.

## 6. HTTPS and installing it as an app (PWA)

The box has no public DNS name, so there is no Let's Encrypt. `caddy/` puts
Caddy in front of Splitter with a certificate from **your own ACME CA** — a
[step-ca](https://smallstep.com/docs/step-ca) on the LAN, say — keeping plain
http as well:

```sh
scp -r deploy/lxc/caddy root@<container>:/root/caddy
# inside the container:
ACME_CA=https://ca.example.lan/acme/acme/directory \
ROOTS_URL=https://ca.example.lan/roots.pem \
ROOT_SHA256=<sha-256 fingerprint of that root> \
./caddy/install-caddy.sh splitter.example.lan   # default name: hostname -f
```

The name you pass is what the certificate is issued for, so open Splitter by
that name (give it a DNS entry or a static lease on your router; the CA must
resolve it too, since it validates over TLS-ALPN-01 on 443 or HTTP-01 on 80).
The installer fetches the CA root, **refuses it unless the fingerprint
matches** `ROOT_SHA256`, adds it to the container's trust store, and serves
it at `http://<that name>/splitter-ca.crt`. Install that once on any device
that does not already trust your CA (Android: Settings → Security →
Encryption & credentials → Install a certificate → CA certificate; iPadOS:
open the file, then Settings → General → VPN & Device Management → install,
and Settings → General → About → Certificate Trust Settings → enable). After
that `https://<that name>/` is trusted, Chrome offers **Install app**, and
the service worker keeps the pages available if the server blips. Caddy
renews the leaf on its own. iPadOS also does "Add to Home Screen" over plain
http, without the certificate.

A Splitter that **sends runs** to this one verifies the certificate against
its own machine's trust store, so a PC that trusts your CA's root can use
the `https://` address; otherwise use the plain `http://` LAN address.

**Installed on Windows, the sender runs as a service** (LocalSystem), and a
service does not see the root you imported for *your* user — the one Chrome
uses. Put the root in the **machine** store instead, from an elevated prompt:

```powershell
Invoke-WebRequest http://<this host>/splitter-ca.crt -OutFile $env:TEMP\root.crt
certutil -addstore -f Root $env:TEMP\root.crt
```

The portable app runs as you, so the user store is enough there. When the
root is missing, the sender's Settings page says "certificate not trusted"
and repeats these steps.
