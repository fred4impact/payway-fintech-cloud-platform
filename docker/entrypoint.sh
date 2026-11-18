#!/bin/sh
set -e

if [ "${RUN_MIGRATIONS:-0}" = "1" ]; then
    echo "Applying database migrations..."
    python manage.py migrate --noinput
fi

# /app/staticfiles is a shared named volume (nginx serves it). Docker only seeds
# a named volume from the image when it is empty, so refresh it on every start.
if [ "${RUN_COLLECTSTATIC:-0}" = "1" ]; then
    echo "Collecting static files..."
    python manage.py collectstatic --noinput --clear -v 0
fi

exec "$@"
