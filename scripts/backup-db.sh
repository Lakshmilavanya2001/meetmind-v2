#!/bin/sh
# Back up meetmind-v2's database (accounts, meetings, transcripts, assistant, bookings, reminders).
#   scripts/backup-db.sh            -> backups/meetmind_v2-<UTC time>.dump, keeps the newest $BACKUP_KEEP (14)
# Restore (stops nothing; replaces the database's contents — stop s2s-web and s2s-agent first):
#   docker compose exec -T db pg_restore -U meetmind -d meetmind_v2 --clean --if-exists < backups/<file>.dump
# Run daily from cron, e.g.:  15 2 * * *  cd /home/ubuntu/meetmind-v2 && scripts/backup-db.sh >> backups/backup.log 2>&1
set -eu
cd "$(dirname "$0")/.."
KEEP="${BACKUP_KEEP:-14}"
DIR="${BACKUP_DIR:-backups}"
umask 077                                  # transcripts and phone numbers: owner only
mkdir -p "$DIR"
OUT="$DIR/meetmind_v2-$(date -u +%Y%m%d-%H%M%S).dump"
docker compose exec -T db pg_dump -U meetmind -d meetmind_v2 -Fc > "$OUT.part"
mv "$OUT.part" "$OUT"
echo "$(date -u +%FT%TZ) backup $OUT ($(du -h "$OUT" | cut -f1))"
ls -1t "$DIR"/meetmind_v2-*.dump | tail -n +"$((KEEP + 1))" | xargs -r rm -f
