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

# Apply DB migrations before serving traffic. A migration failure must fail fast;
# serving an app against a partially upgraded schema is worse than restarting.
gosu appuser python -m alembic upgrade head

# Seed the built-in administrator role/account after the schema is ready.
# The script is idempotent and leaves existing credentials unchanged.
gosu appuser python init_db.py

# Drop privileges and run the application (CMD)
exec gosu appuser "$@"
