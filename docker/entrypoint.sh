#!/bin/sh
# Bring the schema up to date before serving. Alembic is idempotent, so this is
# safe on every container start.
set -e

echo "==> Running database migrations"
alembic upgrade head

echo "==> Starting: $*"
exec "$@"
