#!/usr/bin/env bash
# Run from the project folder (the one holding .env, backend/, web/ and infra/), or from deploy/ inside it.
# Works with podman or docker (CONTAINER_ENGINE=podman|docker to force one); no compose needed.
#
#   ./deploy.sh up       build, then (re)start everything the .env asks for
#   ./deploy.sh web      build and (re)start only the web dashboard
#   ./deploy.sh status   show containers
#   ./deploy.sh backup   dump the database to BACKUP_DIR (default ./backups), keep BACKUP_KEEP_DAYS (14); run it from cron
#   ./deploy.sh down     stop and remove the containers (volumes, so your data, are kept)
#
# What runs where is chosen in .env (setup.sh writes it; the web dashboard covers the rest):
#   DB_MODE=bundled|external      bundled = a Postgres container.  external = your own: set DATABASE_URL_OWNER
#                                 (and DATABASE_URL_APP for a no-DELETE runtime role; else the owner URL is used)
#   REDIS_MODE=bundled|external   external: set REDIS_URL
#   S3_MODE=garage|external|local garage = a Garage container (bucket and key made for you); external = your S3, set
#                                 MINIO_ENDPOINT/MINIO_ACCESS_KEY/MINIO_SECRET_KEY/MINIO_BUCKET_EVIDENCE;
#                                 local = photos on this server's disk
#   LITELLM_MODE=bundled|external external: set LITELLM_BASE_URL and LITELLM_MASTER_KEY
#   API_PORT=8000  WEB_PORT=3000
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -f "$HERE/.env" ]; then ROOT="$HERE"; else ROOT="$(cd "$HERE/.." && pwd)"; fi
cd "$ROOT"
[ -f .env ] || { echo "no .env here: run ./setup.sh first"; exit 1; }
set -a; . ./.env; set +a

# ---- the container engine ----
CE="${CONTAINER_ENGINE:-}"
if [ -z "$CE" ]; then
  if command -v podman >/dev/null 2>&1; then CE=podman; elif command -v docker >/dev/null 2>&1; then CE=docker; fi
fi
[ -n "$CE" ] || { echo "neither podman nor docker is installed: run ./setup.sh, it can install one"; exit 1; }
BUILD_FLAGS=""
# podman as root cannot always build with its default isolation (crun/pasta); chroot works everywhere
[ "$CE" = podman ] && [ "$(id -u)" = 0 ] && BUILD_FLAGS="--isolation=chroot"

NET=apa
IMG=localhost/apa-backend:latest
WEB_IMG=localhost/apa-web:latest
PG_USER="${POSTGRES_USER:-apa_owner}"; PG_DB="${POSTGRES_DB:-apa}"
APP_USER="${APP_DB_USER:-apa_app}"
TZV="${TZ:-Asia/Jakarta}"
DB_MODE="${DB_MODE:-bundled}"; REDIS_MODE="${REDIS_MODE:-bundled}"; LITELLM_MODE="${LITELLM_MODE:-bundled}"
S3_MODE="${S3_MODE:-}"
if [ -z "$S3_MODE" ]; then case "${EVIDENCE_BACKEND:-local}" in s3|minio) S3_MODE=external;; *) S3_MODE=local;; esac; fi
API_PORT="${API_PORT:-8000}"; WEB_PORT="${WEB_PORT:-3000}"

rmc() { $CE rm -f "$@" >/dev/null 2>&1 || true; }
setenv() {  # setenv KEY VALUE: write or replace a line in .env
  if grep -q "^$1=" .env; then sed -i "s|^$1=.*|$1=$2|" .env; else echo "$1=$2" >> .env; fi
}
net() { $CE network inspect $NET >/dev/null 2>&1 || $CE network create $NET >/dev/null; }

# ---- connection strings by mode ----
db_urls() {
  if [ "$DB_MODE" = external ]; then
    [ -n "${DATABASE_URL_OWNER:-}" ] || { echo "DB_MODE=external needs DATABASE_URL_OWNER in .env"; exit 1; }
    OWNER_URL="$DATABASE_URL_OWNER"; APP_URL="${DATABASE_URL_APP:-$DATABASE_URL_OWNER}"
  else
    OWNER_URL="postgresql+asyncpg://${PG_USER}:${POSTGRES_PASSWORD}@postgres:5432/${PG_DB}"
    APP_URL="postgresql+asyncpg://${APP_USER}:${APP_DB_PASSWORD}@postgres:5432/${PG_DB}"
  fi
}
backend_env() {
  db_urls
  local redis="redis://redis:6379/0" llm="http://litellm:4000/v1" key="$LITELLM_MASTER_KEY" ev=s3
  [ "$REDIS_MODE" = external ] && redis="$REDIS_URL"
  [ "$LITELLM_MODE" = external ] && llm="$LITELLM_BASE_URL"
  [ "$S3_MODE" = local ] && ev=local
  ENVARGS=(--env-file .env -e TZ="$TZV" -e DATABASE_URL="$APP_URL" -e REDIS_URL="$redis"
    -e LITELLM_BASE_URL="$llm" -e LITELLM_MASTER_KEY="$key" -e EVIDENCE_BACKEND="$ev")
  [ "$S3_MODE" = local ] && ENVARGS+=(-e EVIDENCE_DIR=/data/evidence -v apa-evidence:/data/evidence)
  return 0
}

# ---- bundled Garage (S3): one container, a bucket and a key made for you the first time ----
start_garage() {
  mkdir -p infra/garage
  if [ ! -f infra/garage/garage.toml ]; then
    local rpc admin
    rpc="$(openssl rand -hex 32)"; admin="$(openssl rand -hex 16)"
    cat > infra/garage/garage.toml <<EOF
metadata_dir = "/var/lib/garage/meta"
data_dir = "/var/lib/garage/data"
db_engine = "sqlite"
replication_factor = 1
rpc_bind_addr = "[::]:3901"
rpc_public_addr = "127.0.0.1:3901"
rpc_secret = "$rpc"
[s3_api]
s3_region = "garage"
api_bind_addr = "[::]:3900"
[admin]
api_bind_addr = "[::]:3903"
admin_token = "$admin"
EOF
  fi
  rmc garage
  $CE run -d --name garage --network $NET --restart unless-stopped \
    -v "$ROOT/infra/garage/garage.toml:/etc/garage.toml:ro" -v apa-garage-meta:/var/lib/garage/meta -v apa-garage-data:/var/lib/garage/data \
    docker.io/dxflrs/garage:v1.0.1 >/dev/null
  local g="$CE exec garage /garage" id="" i
  for i in $(seq 30); do id="$($g status 2>/dev/null | grep -Eo '^[0-9a-f]{16}' | head -1 || true)"; [ -n "$id" ] && break; sleep 1; done
  [ -n "$id" ] || { echo "garage did not start"; exit 1; }
  if $g status 2>/dev/null | grep -q "NO ROLE ASSIGNED"; then  # first start: give the node a role
    $g layout assign -z dc1 -c 1G "$id" >/dev/null 2>&1
    $g layout apply --version 1 >/dev/null 2>&1
    sleep 2
  fi
  local bucket="${MINIO_BUCKET_EVIDENCE:-evidence}"
  $g bucket create "$bucket" >/dev/null 2>&1 || true
  if [ -z "${MINIO_ACCESS_KEY:-}" ] || [[ "${MINIO_ACCESS_KEY}" == apa-minio ]] || ! grep -q '^MINIO_GARAGE=1' .env; then
    local out kid sec
    out="$($g key create apa-key-$(date +%s) 2>/dev/null)"
    kid="$(echo "$out" | sed -n 's/^Key ID: *//p' | head -1)"; sec="$(echo "$out" | sed -n 's/^Secret key: *//p' | head -1)"
    [ -n "$kid" ] && [ -n "$sec" ] || { echo "could not create a Garage key"; exit 1; }
    $g bucket allow --read --write --owner "$bucket" --key "$kid" >/dev/null 2>&1
    setenv MINIO_ENDPOINT garage:3900; setenv MINIO_ACCESS_KEY "$kid"; setenv MINIO_SECRET_KEY "$sec"
    setenv MINIO_REGION garage; setenv MINIO_BUCKET_EVIDENCE "$bucket"; setenv MINIO_SECURE false; setenv MINIO_GARAGE 1
    set -a; . ./.env; set +a
    echo "garage ready: bucket $bucket and a key were created and saved in .env"
  fi
}

down() { rmc web api worker bot migrate litellm redis postgres garage; }

up() {
  net
  $CE build -q $BUILD_FLAGS -t $IMG backend
  rmc litellm migrate api worker bot
  if [ "$DB_MODE" = bundled ]; then
    rmc postgres
    $CE run -d --name postgres --network $NET --restart unless-stopped --env-file .env -e TZ="$TZV" \
      -v apa-pgdata:/var/lib/postgresql/data -v "$ROOT/infra/postgres/01-roles.sh:/docker-entrypoint-initdb.d/01-roles.sh:ro" \
      docker.io/library/postgres:17 >/dev/null
  fi
  if [ "$REDIS_MODE" = bundled ]; then
    rmc redis
    $CE run -d --name redis --network $NET --restart unless-stopped -v apa-redisdata:/data \
      docker.io/library/redis:7-alpine redis-server --appendonly yes >/dev/null
  fi
  if [ "$LITELLM_MODE" = bundled ]; then
    [ -f infra/litellm/config.yaml ] || cp infra/litellm/config.example.yaml infra/litellm/config.yaml
    $CE run -d --name litellm --network $NET --restart unless-stopped --env-file .env \
      -v "$ROOT/infra/litellm/config.yaml:/app/config.yaml:ro" \
      ghcr.io/berriai/litellm:main-stable --config /app/config.yaml --port 4000 >/dev/null
  fi
  [ "$S3_MODE" = garage ] && start_garage

  if [ "$DB_MODE" = bundled ]; then
    echo -n "waiting for postgres"
    for _ in $(seq 60); do $CE exec postgres pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1 && break; echo -n .; sleep 2; done; echo
  fi
  backend_env
  $CE run --rm --name migrate --network $NET --env-file .env -e TZ="$TZV" -e DATABASE_URL="$OWNER_URL" $IMG alembic upgrade head

  local secrets=(); [ -d "$ROOT/secrets" ] && secrets=(-v "$ROOT/secrets:/run/secrets/apa:ro")
  $CE run -d --name api --network $NET --restart unless-stopped "${ENVARGS[@]}" -p "$API_PORT:8000" \
    $IMG uvicorn apa.gateway.main:app --host 0.0.0.0 --port 8000 >/dev/null
  $CE run -d --name worker --network $NET --restart unless-stopped "${secrets[@]}" "${ENVARGS[@]}" \
    $IMG arq apa.workers.settings.WorkerSettings >/dev/null
  $CE run -d --name bot --network $NET --restart unless-stopped "${ENVARGS[@]}" \
    $IMG python -m apa.bot.main >/dev/null
  web
}

web() {
  net
  $CE build -q $BUILD_FLAGS -t $WEB_IMG web
  rmc web
  $CE run -d --name web --network $NET --restart unless-stopped -p "$WEB_PORT:3000" $WEB_IMG >/dev/null
  status
$CE image prune -f >/dev/null 2>&1 || true  # every build leaves the previous image behind (about 850 MB each): drop the unused ones
  echo "dashboard: http://$(hostname -I 2>/dev/null | awk '{print $1}' || echo localhost):$WEB_PORT (sign in with INGEST_API_KEY from .env)"
}

status() { $CE ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.Ports}}'; }

backup() {  # a compressed dump of the database into BACKUP_DIR (default ./backups); files older than BACKUP_KEEP_DAYS (14) are removed
  [ "$DB_MODE" = bundled ] || { echo "backup: DB_MODE=external, dump your own database with your own tools"; exit 1; }
  local dir="${BACKUP_DIR:-$ROOT/backups}" f
  mkdir -p "$dir"; chmod 700 "$dir"
  f="$dir/apa-$(date +%Y%m%d-%H%M%S).sql.gz"
  $CE exec postgres pg_dump -U "$PG_USER" -d "$PG_DB" --no-owner | gzip > "$f.tmp" && mv "$f.tmp" "$f" || { rm -f "$f.tmp"; echo "backup failed"; exit 1; }
  chmod 600 "$f"
  find "$dir" -name 'apa-*.sql.gz' -mtime +"${BACKUP_KEEP_DAYS:-14}" -delete
  echo "backup: $f ($(du -h "$f" | cut -f1))"
}

case "${1:-}" in up) up;; web) web;; down) down;; status) status;; backup) backup;; garage) net; start_garage;; *) echo "usage: $0 up|web|down|status|backup|garage"; exit 2;; esac
