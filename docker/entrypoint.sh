#!/bin/sh
# Bring the schema up to date before serving. Alembic is idempotent, so this is
# safe on every container start.
set -e

# --- preflight -------------------------------------------------------------
# The most common deployment failure is the data directory not being writable
# by the uid the container runs as. Left to itself that surfaces as a 30-line
# SQLAlchemy traceback ending in "unable to open database file", and the
# container exits - which a host like TrueNAS reports only as "Stopped".
# Check it up front and say exactly what to fix.
DB_URL="${DATABASE_URL:-sqlite:////app/data/malesevich.db}"
# Strip the "sqlite:///" prefix (10 chars). A fourth slash is part of the path,
# so this yields an absolute path for sqlite://// and a relative one for sqlite:///.
case "$DB_URL" in
    sqlite:///*)  DB_PATH="$(echo "$DB_URL" | cut -c11-)" ;;
    *)            DB_PATH="" ;;   # not sqlite; nothing local to check
esac

if [ -n "$DB_PATH" ]; then
    DB_DIR=$(dirname "$DB_PATH")
    UID_NOW=$(id -u)
    GID_NOW=$(id -g)

    if [ ! -d "$DB_DIR" ]; then
        echo "ERROR: the database directory $DB_DIR does not exist." >&2
        echo "       Create it on the host and mount it at $DB_DIR." >&2
        exit 1
    fi

    if [ ! -w "$DB_DIR" ]; then
        OWNER=$(stat -c '%u:%g' "$DB_DIR" 2>/dev/null || echo "unknown")
        MODE=$(stat -c '%a' "$DB_DIR" 2>/dev/null || echo "unknown")
        echo "ERROR: $DB_DIR is not writable." >&2
        echo "" >&2
        echo "    container runs as:  uid=$UID_NOW gid=$GID_NOW" >&2
        echo "    directory owned by: $OWNER (mode $MODE)" >&2
        echo "" >&2
        echo 'The database cannot be created, so the app would exit with an' >&2
        echo '"unable to open database file" error a moment from now.' >&2
        echo "" >&2
        if [ "$OWNER" = "$UID_NOW:$GID_NOW" ]; then
            # Ownership is already right, so the mount itself must be read-only.
            echo "Ownership looks correct, so the mount is probably read-only." >&2
            echo "Check for a trailing :ro on the volume line in your compose" >&2
            echo "file, or a read-only dataset/share on the host." >&2
        else
            echo "Fix it on the host by giving that uid ownership of the" >&2
            echo "directory bound to $DB_DIR:" >&2
            echo "" >&2
            echo "    chown -R $UID_NOW:$GID_NOW <host path>" >&2
            echo "" >&2
            echo "On TrueNAS that host path is DATA_PATH from your .env, and" >&2
            echo "the uid comes from PUID/PGID there (568:568 is the apps user)." >&2
        fi
        exit 1
    fi
fi

echo "==> Running database migrations"
alembic upgrade head

echo "==> Starting: $*"
exec "$@"
