#!/bin/sh
# Entrypoint script for DCN backend container.
# Runs as root to fix mounted-volume permissions + apply DB migrations, then
# drops to the unprivileged appuser for the server process.

set -e

# Scope the permission fix to mounted data/log volumes only — NOT the whole
# /app tree (the image already chowns /app at build; a recursive chown on every
# start is slow and churns the layered filesystem).
for dir in /app/data /app/logs; do
    if [ -d "$dir" ]; then
        chown -R appuser:appgroup "$dir" 2>/dev/null || true
    fi
done

# Apply DB migrations (Alembic) before serving traffic. Best-effort: on failure
# the container still starts so the issue is visible in logs; the legacy
# auto-create path remains available via DCN_AUTO_CREATE=true.
gosu appuser python -m alembic upgrade head || \
    echo "[entrypoint] WARNING: alembic upgrade head failed — check DATABASE_URL / migration status"

# Drop privileges and run the application (CMD)
exec gosu appuser "$@"
