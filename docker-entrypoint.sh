#!/bin/sh
# Starts as root only long enough to make /data belong to PUID:PGID (Docker creates a missing
# appdata folder as root), then runs the app as that user. Default 99:100 = Unraid's nobody:users.
# /media is never chowned: it's your share and keeps its own owners.
set -e
PUID="${PUID:-99}"
PGID="${PGID:-100}"
if [ "$(id -u)" = "0" ]; then
  mkdir -p "$LA_DATA"
  find "$LA_DATA" \( ! -user "$PUID" -o ! -group "$PGID" \) -exec chown "$PUID:$PGID" {} + 2>/dev/null || true
  chmod 700 "$LA_DATA"
  exec setpriv --reuid="$PUID" --regid="$PGID" --clear-groups "$@"
fi
exec "$@"
