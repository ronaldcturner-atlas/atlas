#!/bin/sh
set -eu

export DJANGO_SETTINGS_MODULE="${DJANGO_SETTINGS_MODULE:-config.settings.production}"

# The web service owns migrations. A worker exits and lets Railway restart it
# until the deployed schema is ready instead of running against an older one.
python manage.py migrate --check

exec python manage.py run_optimizer_worker
