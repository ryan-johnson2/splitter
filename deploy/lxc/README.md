# Splitter on Proxmox LXC

Docker stays the fast-iteration path (`docker compose up -d` from the repo
root). This directory is the long-term hosting path: a plain Debian CT with
Splitter in a virtualenv under systemd — the same shape as Marshal's deploy.

The unit of deployment is a **bundle**: one tarball with two wheels (`splitter`
and its sibling `velocidrone-ws`, which is not on PyPI), the installer, the
systemd unit and the env template.

```
build-bundle.sh    # on your workstation: builds dist/splitter-lxc-<version>.tar.gz
install.sh         # inside the container: provisions or upgrades in place
splitter.service   # the systemd unit
```

## 1. Build the bundle

```sh
deploy/lxc/build-bundle.sh
# -> deploy/lxc/dist/splitter-lxc-0.1.0.tar.gz
```

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
pct push 111 deploy/lxc/dist/splitter-lxc-0.1.0.tar.gz /root/splitter-lxc-0.1.0.tar.gz
pct exec 111 -- bash -c 'cd /root && tar xzf splitter-lxc-0.1.0.tar.gz && cd splitter-lxc-0.1.0 && ./install.sh'
```

The installer creates a `splitter` system user, a venv at `/opt/splitter/venv`,
the data directory `/var/lib/splitter` and `/etc/splitter/splitter.env`, then
starts the service. There are no secrets, so it starts on the first install.

Then on the tablet open `http://<container-ip>:8100/settings`, enter the gaming
PC's **LAN IP** (the game never listens on localhost) and save. In the game,
turn on *Options → Main Settings → Websocket Communication* (and *Websocket
IMU* for telemetry). The header dot goes green when the game is reachable.

Health: `/healthz`. Logs: `journalctl -u splitter -f`.

## 4. Upgrades

Re-running the installer is the upgrade. It keeps the env file and database,
replaces the wheels and restarts the unit:

```sh
deploy/lxc/build-bundle.sh
pct push 111 deploy/lxc/dist/splitter-lxc-0.2.0.tar.gz /root/splitter-lxc-0.2.0.tar.gz
pct exec 111 -- bash -c 'cd /root && tar xzf splitter-lxc-0.2.0.tar.gz && cd splitter-lxc-0.2.0 && ./install.sh'
```

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
