#!/usr/bin/env bash
# Install BoxServer HLS Relay v0.4.x over an existing v0.3 or older v0.4.x directory.
#   - full backup (tar.gz, outside the build context) + tag of the running image
#   - copies ONLY relay.py, Dockerfile, .dockerignore, README/DIAGNOSTICO, tests/, scripts/
#   - never touches .env, compose.yaml, volumes, networks or other containers
#   - rebuilds/recreates only the compose project of this directory
# Usage: sudo bash scripts/install_v04.sh [/mnt/sdb1/hls-relay]
set -euo pipefail

TARGET="${1:-/mnt/sdb1/hls-relay}"
PKG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_ROOT="${BACKUP_ROOT:-$(dirname "$TARGET")/hls-relay-backups}"
CONTAINER="${CONTAINER:-boxserver-hls-relay}"
STAMP="$(date +%Y%m%d-%H%M%S)"

die() { echo "ERRO: $*" >&2; exit 1; }
say() { echo "==> $*"; }

[ -d "$TARGET" ] || die "diretório $TARGET não existe"
[ -f "$TARGET/relay.py" ] || die "$TARGET/relay.py não encontrado"
[ -f "$TARGET/.env" ] || die "$TARGET/.env não encontrado (não será criado nem alterado)"
HAS_COMPOSE=0
for f in compose.yaml compose.yml docker-compose.yml docker-compose.yaml; do
  [ -f "$TARGET/$f" ] && HAS_COMPOSE=1
done
[ "$HAS_COMPOSE" -eq 1 ] || die "nenhum arquivo compose em $TARGET"
[ "$(cd "$PKG" && pwd)" != "$(cd "$TARGET" && pwd)" ] || die "extraia o pacote v0.4 em outro diretório"
command -v docker >/dev/null || die "docker não encontrado"
grep -qE '^VERSION = "0\.[45]\.' "$PKG/relay.py" || die "pacote inválido"
CURRENT="$(grep -m1 -oE '^VERSION = "[0-9.]+"' "$TARGET/relay.py" | cut -d'"' -f2 || true)"
NEW="$(grep -m1 -oE '^VERSION = "[0-9.]+"' "$PKG/relay.py" | cut -d'"' -f2)"
if [ -n "$CURRENT" ]; then
  LABEL="v$CURRENT"          # upgrade between 0.4.x releases
else
  LABEL="v0.3"
fi
[ "$CURRENT" != "$NEW" ] || die "a versão $NEW já está instalada em $TARGET"
echo "    instalado: ${CURRENT:-0.3}  ->  pacote: $NEW"

say "1/6 Validando o pacote"
for f in relay.py Dockerfile .dockerignore README.md DIAGNOSTICO.md .env.example; do
  [ -f "$PKG/$f" ] || die "pacote incompleto: falta $f"
done
python3 -c 'import ast,sys; ast.parse(open(sys.argv[1]).read())' "$PKG/relay.py"

say "2/6 Backup completo de $TARGET"
mkdir -p "$BACKUP_ROOT"
chmod 700 "$BACKUP_ROOT"
ARCHIVE="$BACKUP_ROOT/hls-relay-$LABEL-$STAMP.tar.gz"
tar -C "$(dirname "$TARGET")" -czf "$ARCHIVE" "$(basename "$TARGET")"
chmod 600 "$ARCHIVE"
tar -tzf "$ARCHIVE" >/dev/null || die "backup ilegível: $ARCHIVE"
sha256sum "$ARCHIVE" > "$ARCHIVE.sha256"
if [ "$LABEL" = "v0.3" ]; then
  echo "$STAMP" > "$BACKUP_ROOT/LAST_BACKUP"      # used by rollback_v03.sh
else
  echo "$STAMP" > "$BACKUP_ROOT/LAST_BACKUP_${LABEL}"
fi
echo "    backup: $ARCHIVE"

say "3/6 Marcando a imagem atual para rollback rápido"
IMAGE_ID="$(docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null || true)"
if [ -n "$IMAGE_ID" ]; then
  docker image tag "$IMAGE_ID" "boxserver-hls-relay:$LABEL-$STAMP"
  echo "    imagem: boxserver-hls-relay:$LABEL-$STAMP"
else
  echo "    container $CONTAINER não encontrado; pulando tag de imagem"
fi

say "4/6 Copiando arquivos (sem tocar .env e compose.yaml)"
install -m 0644 "$PKG/relay.py" "$TARGET/relay.py"
install -m 0644 "$PKG/Dockerfile" "$TARGET/Dockerfile"
install -m 0644 "$PKG/.dockerignore" "$TARGET/.dockerignore"
install -m 0644 "$PKG/README.md" "$TARGET/README.md"
install -m 0644 "$PKG/DIAGNOSTICO.md" "$TARGET/DIAGNOSTICO.md"
install -m 0644 "$PKG/.env.example" "$TARGET/.env.example.v0.4"
mkdir -p "$TARGET/tests" "$TARGET/scripts"
install -m 0644 "$PKG"/tests/*.py "$TARGET/tests/"
install -m 0755 "$PKG"/scripts/*.sh "$TARGET/scripts/"

say "4b/6 Modo multicanal: pasta de dados, senha do painel e compose.override.yaml"
# pasta gravável para o estado dos canais (o container é read_only e roda como 65534)
mkdir -p "$TARGET/data"
chown 65534:65534 "$TARGET/data"
chmod 700 "$TARGET/data"
NEW_PASSWORD=""
if ! grep -qE '^ADMIN_PASSWORD=.+' "$TARGET/.env" && [ ! -s "$TARGET/data/admin_password" ]; then
  NEW_PASSWORD="$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')"
  printf '%s\n' "$NEW_PASSWORD" > "$TARGET/data/admin_password"
  chown 65534:65534 "$TARGET/data/admin_password"
  chmod 600 "$TARGET/data/admin_password"
fi
OVERRIDE="$TARGET/compose.override.yaml"
if [ -f "$OVERRIDE" ]; then
  if grep -q 'RELAY_STATE_DIR' "$OVERRIDE"; then
    echo "    compose.override.yaml já configurado"
  else
    echo "    AVISO: $OVERRIDE já existe e não foi alterado. Acrescente ao serviço do relay:"
    echo "      volumes: [\"./data:/data\"]   environment: {RELAY_STATE_DIR: /data}   mem_limit: ${RELAY_MEM_LIMIT:-2g}"
  fi
else
  SERVICE="$(cd "$TARGET" && docker compose config --services 2>/dev/null | head -1)"
  SERVICE="${SERVICE:-hls-relay}"
  cat > "$OVERRIDE" <<YAML
# Criado pelo instalador da v0.5 (multicanal). O compose.yaml original não foi alterado.
# Para desfazer: apague este arquivo e rode "docker compose up -d --force-recreate".
services:
  $SERVICE:
    volumes:
      - ./data:/data
    environment:
      RELAY_STATE_DIR: /data
    mem_limit: ${RELAY_MEM_LIMIT:-2g}
YAML
  echo "    criado $OVERRIDE (serviço: $SERVICE, mem_limit: ${RELAY_MEM_LIMIT:-2g})"
fi

say "5/6 Rebuild e recriação somente deste projeto compose"
cd "$TARGET"
docker compose build
docker compose up -d --force-recreate

say "6/6 Aguardando /health (via docker exec; não depende da porta publicada)"
for _ in $(seq 1 30); do
  if docker exec "$CONTAINER" python -c "import urllib.request;import os;urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('PORT','8099'),timeout=3)" 2>/dev/null; then
    docker exec "$CONTAINER" python -c "import json,urllib.request;import os;h=json.load(urllib.request.urlopen('http://127.0.0.1:%s/health'%os.environ.get('PORT','8099'),timeout=3));print('    versão',h['version'],'| estado',h['status'])"
    echo
    echo "Instalado. Painel:     http://IP_DO_BOXSERVER:8099/admin"
    if [ -n "$NEW_PASSWORD" ]; then
      echo "Senha do painel:      $NEW_PASSWORD   (guardada em $TARGET/data/admin_password)"
    fi
    echo "Monitorar:            bash $TARGET/scripts/monitor.sh [canal]"
    echo "Rollback para v0.3:   sudo bash $TARGET/scripts/rollback_v03.sh $TARGET"
    exit 0
  fi
  sleep 2
done
echo "AVISO: /health não respondeu em 60 s. Veja: docker logs --tail 100 $CONTAINER" >&2
echo "Rollback para v0.3: sudo bash $TARGET/scripts/rollback_v03.sh $TARGET" >&2
exit 1
