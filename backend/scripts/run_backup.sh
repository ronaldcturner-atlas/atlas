#!/bin/sh
set -eu

backup_dir="${ATLAS_BACKUP_DIR:-/data/backups}"
retention_days="${ATLAS_BACKUP_RETENTION_DAYS:-30}"

mkdir -p "$backup_dir"
python manage.py backup_database

# Retain recent successful backups while preventing the Railway volume from
# filling indefinitely. Partial files are removed after a failed/interrupted run.
find "$backup_dir" -type f -name '.*.partial' -delete
find "$backup_dir" -type f \( -name 'atlas-*.dump' -o -name 'atlas-*.dump.sha256' \) \
  -mtime "+$retention_days" -delete
