#!/bin/sh
set -e
PUID=${PUID:-1000}
PGID=${PGID:-1000}
# Ensure /data is writable by the target user even when a volume is mounted
# with root-owned permissions (common with bind mounts from NAS / Portainer).
mkdir -p /data
chown "${PUID}:${PGID}" /data
exec gosu "${PUID}:${PGID}" "$@"
