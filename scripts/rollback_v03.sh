#!/usr/bin/env bash
# Restore the v0.3 directory from the backup made by install_v04.sh.
#   - the current directory is kept aside as <dir>.v0.4-<date> (nothing deleted)
#   - the CURRENT .env is kept (v0.3 ignores the new v0.4 keys);
#     pass --restore-env to bring back the .env from the backup instead
#   - rebuilds/recreates only the compose project of this directory
# Usage: sudo bash scripts/rollback_v03.sh [/mnt/sdb1/hls-relay] [STAMP] [--to v0.4.3] [--restore-env]
set -euo pipefail

TARGET="/mnt/sdb1/hls-relay"
STAMP=""
LABEL="v0.3"
RESTORE_ENV=0
NEXT_IS_LABEL=0
for arg in "$@"; do
  if [ "$NEXT_IS_LABEL" -eq 1 ]; then LABEL="$arg"; NEXT_IS_LABEL=0; continue; fi
  case "$arg" in
    --restore-env) RESTORE_ENV=1 ;;
    --to) NEXT_IS_LABEL=1 ;;          # ex.: --to v0.4.3 (backup feito ao atualizar a partir dela)
    /*) TARGET="${arg%/}" ;;
    *) STAMP="$arg" ;;
  esac
done
BACKUP_ROOT="${BACKUP_ROOT:-$(dirname "$TARGET")/hls-relay-backups}"
CONTAINER="${CONTAINER:-boxserver-hls-relay}"
if [ -z "$STAMP" ]; then
  if [ "$LABEL" = "v0.3" ]; then
    STAMP="$(cat "$BACKUP_ROOT/LAST_BACKUP" 2>/dev/null || true)"
  else
    STAMP="$(cat "$BACKUP_ROOT/LAST_BACKUP_$LABEL" 2>/dev/null || true)"
  fi
fi
ARCHIVE="$BACKUP_ROOT/hls-relay-$LABEL-$STAMP.tar.gz"

die() { echo "ERRO: $*" >&2; exit 1; }
[ -n "$STAMP" ] && [ -f "$ARCHIVE" ] || die "backup não encontrado ($ARCHIVE). Backups: $(ls "$BACKUP_ROOT" 2>/dev/null | tr '\n' ' ')"
if [ -f "$ARCHIVE.sha256" ]; then sha256sum -c "$ARCHIVE.sha256" >/dev/null || die "checksum do backup não confere"; fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
tar -C "$WORK" -xzf "$ARCHIVE"
RESTORED="$WORK/$(basename "$TARGET")"
[ -f "$RESTORED/relay.py" ] || die "backup sem relay.py"

ASIDE="$TARGET.v0.4-$(date +%Y%m%d-%H%M%S)"
echo "==> Guardando a versão atual em $ASIDE"
cp -a "$TARGET" "$ASIDE"

if [ "$RESTORE_ENV" -eq 0 ] && [ -f "$TARGET/.env" ]; then
  cp -a "$TARGET/.env" "$RESTORED/.env"
fi

echo "==> Restaurando $LABEL ($STAMP)"
# replace directory contents in place (keeps the directory inode, path and compose project name)
find "$TARGET" -mindepth 1 -maxdepth 1 -exec rm -rf {} +
cp -a "$RESTORED"/. "$TARGET"/

cd "$TARGET"
docker compose build
docker compose up -d --force-recreate
sleep 5
docker ps --filter "name=$CONTAINER" --format '{{.Names}}  {{.Status}}'
echo "Rollback concluído. A versão que estava instalada ficou guardada em $ASIDE"
echo "Alternativa sem rebuild (imagem marcada na instalação): boxserver-hls-relay:$LABEL-$STAMP"
