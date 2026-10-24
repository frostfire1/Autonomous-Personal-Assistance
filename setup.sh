#!/usr/bin/env bash
# One-step installer for a Linux or macOS server. Just run it: it asks everything as you go.
#
#   ./setup.sh
#
# It checks for Docker or Podman (and offers to install one), asks Easy or Advanced, writes .env with fresh random
# secrets, shows a summary, and starts everything. Run it again later for a menu: update, status, stop, change choices.
# It never overwrites an existing .env: it keeps your values and only adds what is missing.
set -euo pipefail
cd "$(dirname "${BASH_SOURCE[0]}")"

MODE=""; ENGINE="${CONTAINER_ENGINE:-}"
TTY="${APA_TTY:-/dev/tty}"  # where answers are read from (a file in tests)
YES="${APA_UNATTENDED:-0}"  # 1 = no questions, all defaults (Easy); for scripts and tests only
exec 3< "$TTY" 2>/dev/null || YES=1  # one stream for every answer; no terminal at all means defaults

say() { printf '%s\n' "$*"; }
ask() {  # ask "question" "default" -> answer in $REPLY
  local q="$1" d="${2:-}"
  if [ "$YES" = 1 ]; then REPLY="$d"; return; fi
  read -r -p "$q${d:+ [$d]}: " REPLY <&3 || REPLY="$d"
  REPLY="${REPLY:-$d}"
}
choose() {  # choose "question" opt1 opt2 ... -> chosen option in $REPLY (first is the default)
  local q="$1"; shift
  if [ "$YES" = 1 ]; then REPLY="$1"; return; fi
  say "$q"; local i=1 o; for o in "$@"; do say "  $i) $o"; i=$((i + 1)); done
  local n; read -r -p "Choose a number [1]: " n <&3 || n=1; n="${n:-1}"
  if [[ "$n" =~ ^[0-9]+$ ]] && [ "$n" -ge 1 ] && [ "$n" -le "$#" ]; then REPLY="${!n}"; else REPLY="$1"; fi
}
rand() { openssl rand -hex "${1:-16}" 2>/dev/null || head -c "${1:-16}" /dev/urandom | od -An -tx1 | tr -d ' \n'; }
SUDO=""; [ "$(id -u)" != 0 ] && command -v sudo >/dev/null 2>&1 && SUDO="sudo"

# ---------- 1. Docker or Podman ----------
detect_engine() {
  [ -n "$ENGINE" ] && command -v "$ENGINE" >/dev/null 2>&1 && return 0
  for e in podman docker; do command -v "$e" >/dev/null 2>&1 && { ENGINE="$e"; return 0; }; done
  return 1
}
install_engine() {
  local which="$1"
  say "Installing $which..."
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq && $SUDO apt-get install -y -qq "$([ "$which" = docker ] && echo docker.io || echo podman)" curl openssl
  elif command -v dnf >/dev/null 2>&1; then
    if [ "$which" = docker ]; then curl -fsSL https://get.docker.com | $SUDO sh; else $SUDO dnf install -y podman curl openssl; fi
  elif command -v yum >/dev/null 2>&1; then
    if [ "$which" = docker ]; then curl -fsSL https://get.docker.com | $SUDO sh; else $SUDO yum install -y podman curl openssl; fi
  elif command -v pacman >/dev/null 2>&1; then
    $SUDO pacman -Sy --noconfirm "$which" openssl curl
  elif command -v zypper >/dev/null 2>&1; then
    $SUDO zypper --non-interactive install "$which" curl openssl
  elif command -v brew >/dev/null 2>&1; then
    brew install "$([ "$which" = docker ] && echo --cask docker || echo podman)"
    [ "$which" = podman ] && podman machine init >/dev/null 2>&1 && podman machine start >/dev/null 2>&1 || true
  else
    say "Cannot install automatically on this system. Install $which yourself (https://docs.docker.com/engine/install/ or https://podman.io/docs/installation) and run this again."
    exit 1
  fi
  if [ "$which" = docker ] && command -v systemctl >/dev/null 2>&1; then $SUDO systemctl enable --now docker >/dev/null 2>&1 || true; fi
  ENGINE="$which"
}
if ! detect_engine; then
  say "Neither Docker nor Podman is installed."
  if [ "$YES" = 1 ]; then pick=podman; else
    choose "Which one do you want to install?" "Podman (no daemon, recommended)" "Docker" "Cancel, I will install it myself"
    case "$REPLY" in Podman*) pick=podman;; Docker) pick=docker;; *) say "OK. Install one of them, then run ./setup.sh again."; exit 0;; esac
  fi
  install_engine "$pick"
  detect_engine || { say "Installing $pick did not work. Try a new terminal, or install it manually."; exit 1; }
fi
say "Using: $ENGINE"
if [ "$ENGINE" = docker ] && ! docker info >/dev/null 2>&1; then
  say "Docker is installed but this user cannot use it. Run with sudo ($SUDO ./setup.sh) or add the user to the docker group."
  exit 1
fi

# ---------- 1b. Already installed? offer the everyday actions ----------
if [ -f .env ] && [ -f deploy/deploy.sh ] && $ENGINE ps -a --format '{{.Names}}' 2>/dev/null | grep -qx api; then
  say ""; say "APA is already installed here."
  choose "What do you want to do?" "Update: rebuild and restart" "Show container status" "Change install choices (database, S3, etc.)" "Stop everything" "Quit"
  case "$REPLY" in
    Update*) CONTAINER_ENGINE="$ENGINE" bash deploy/deploy.sh up; exit 0;;
    Show*) CONTAINER_ENGINE="$ENGINE" bash deploy/deploy.sh status; exit 0;;
    Stop*) CONTAINER_ENGINE="$ENGINE" bash deploy/deploy.sh down; say "All containers stopped. Your data is kept."; exit 0;;
    Quit) exit 0;;
  esac
fi

# ---------- 2. Easy or Advanced ----------
if [ -z "$MODE" ]; then
  choose "Install type?" "Easy: everything automatic, in containers on this machine (database, queue, S3 storage, AI gateway, web)" "Advanced: choose each part, a container or your own (database, Redis, S3, external AI gateway)"
  case "$REPLY" in Easy*) MODE=easy;; *) MODE=advanced;; esac
fi

# ---------- 3. .env (existing values are kept) ----------
[ -f .env ] || { [ -f .env.example ] && cp .env.example .env || : > .env; NEW_ENV=1; }
NEW_ENV="${NEW_ENV:-0}"
getv() { grep -E "^$1=" .env | tail -1 | cut -d= -f2- || true; }
setv() { if grep -qE "^$1=" .env; then sed -i.bak "s|^$1=.*|$1=$2|" .env && rm -f .env.bak; else echo "$1=$2" >> .env; fi; }
keep_or() {  # keep a real value already in .env, else set the generated one
  local cur; cur="$(getv "$1")"
  case "$cur" in ""|change-me*|apa-minio) setv "$1" "$2";; esac
}
keep_or POSTGRES_PASSWORD "$(rand 16)"; keep_or APP_DB_PASSWORD "$(rand 16)"
keep_or LITELLM_MASTER_KEY "sk-$(rand 16)"; keep_or INGEST_API_KEY "$(rand 20)"
keep_or SECRETS_KEY "$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '[:space:]')"  # encrypts the diary and the assistant's memory: back it up, losing it loses them
[ -n "$(getv POSTGRES_USER)" ] || setv POSTGRES_USER apa_owner
[ -n "$(getv POSTGRES_DB)" ] || setv POSTGRES_DB apa
[ -n "$(getv APP_DB_USER)" ] || setv APP_DB_USER apa_app
[ -n "$(getv TZ)" ] || setv TZ "$(cat /etc/timezone 2>/dev/null || echo Asia/Jakarta)"

if [ "$MODE" = easy ]; then
  setv DB_MODE bundled; setv REDIS_MODE bundled; setv S3_MODE garage; setv LITELLM_MODE bundled
else
  choose "Database (PostgreSQL)?" "A container on this machine" "My own (connection URL)"
  if [ "$REPLY" = "My own (connection URL)" ]; then
    setv DB_MODE external
    ask "Database owner URL, like postgresql+asyncpg://user:password@host:5432/name" "$(getv DATABASE_URL_OWNER)"; setv DATABASE_URL_OWNER "$REPLY"
    ask "Runtime role URL without DELETE rights (empty = use the owner URL)" "$(getv DATABASE_URL_APP)"; [ -n "$REPLY" ] && setv DATABASE_URL_APP "$REPLY"
  else setv DB_MODE bundled; fi
  choose "Queue (Redis)?" "A container on this machine" "My own (URL)"
  if [ "$REPLY" = "My own (URL)" ]; then
    setv REDIS_MODE external; ask "Redis URL, e.g. redis://host:6379/0" "$(getv REDIS_URL)"; setv REDIS_URL "$REPLY"
  else setv REDIS_MODE bundled; fi
  choose "Receipt photo storage?" "Automatic S3 in a container (Garage)" "This machine's disk (no S3)" "My own S3 (AWS, R2, MinIO, B2...)"
  case "$REPLY" in
    "Automatic S3"*) setv S3_MODE garage;;
    "This machine"*) setv S3_MODE local;;
    *) setv S3_MODE external
       ask "S3 address (host:port, without http)" "$(getv MINIO_ENDPOINT)"; setv MINIO_ENDPOINT "$REPLY"
       ask "Access key" "$(getv MINIO_ACCESS_KEY)"; setv MINIO_ACCESS_KEY "$REPLY"
       ask "Secret key" ""; [ -n "$REPLY" ] && setv MINIO_SECRET_KEY "$REPLY"
       ask "Bucket" "$(getv MINIO_BUCKET_EVIDENCE)"; setv MINIO_BUCKET_EVIDENCE "${REPLY:-evidence}"
       ask "Region (empty if unsure)" "$(getv MINIO_REGION)"; setv MINIO_REGION "$REPLY"
       ask "Use HTTPS? (true/false)" "$(getv MINIO_SECURE)"; setv MINIO_SECURE "${REPLY:-false}";;
  esac
  choose "AI model gateway (LiteLLM)?" "A container on this machine" "My own (address)"
  if [ "$REPLY" = "My own (address)" ]; then
    setv LITELLM_MODE external; ask "Address, e.g. http://host:4000/v1" "$(getv LITELLM_BASE_URL)"; setv LITELLM_BASE_URL "$REPLY"
    ask "Master key of that gateway" ""; [ -n "$REPLY" ] && setv LITELLM_MASTER_KEY "$REPLY"
  else setv LITELLM_MODE bundled; fi
  ask "API port" "$(getv API_PORT)"; setv API_PORT "${REPLY:-8000}"
  ask "Web port" "$(getv WEB_PORT)"; setv WEB_PORT "${REPLY:-3000}"
fi

# the AI gateway needs at least one provider: one address and key here, the rest is done in the web dashboard
# (the questions below are the same whichever mode was chosen)
if [ "$(getv LITELLM_MODE)" = bundled ]; then
  if [ -z "$(getv KUNCI_API_KEY)" ] && [ -z "$(getv OSIRIS_API_KEY)" ]; then
    # a fresh install gets the generic template; an existing one keeps its own infra/litellm/config.yaml
    [ "$NEW_ENV" = 1 ] && cp infra/litellm/config.example.yaml infra/litellm/config.yaml
    if [ -z "$(getv MY_API_KEY)" ]; then
      say ""; say "AI provider (you can leave this empty now and add providers later on the web dashboard, AI models page):"
      ask "OpenAI-compatible API address, e.g. https://api.openai.com/v1" ""; [ -n "$REPLY" ] && setv MY_API_BASE "$REPLY"
      [ -n "$REPLY" ] && { ask "API key" ""; [ -n "$REPLY" ] && setv MY_API_KEY "$REPLY"; }
    fi
    [ -n "$(getv MY_API_BASE)" ] || setv MY_API_BASE https://api.example.com/v1
    [ -n "$(getv MY_API_KEY)" ] || setv MY_API_KEY not-set-yet
  fi
fi

if [ -z "$(getv TELEGRAM_BOT_TOKEN)" ] && [ "$YES" != 1 ]; then
  say ""; say "Telegram bot (optional, you can fill this in .env later):"
  ask "Bot token from @BotFather" ""; if [ -n "$REPLY" ]; then setv TELEGRAM_BOT_TOKEN "$REPLY"; ask "Owner Telegram ID (a number)" ""; setv TELEGRAM_OWNER_ID "${REPLY:-0}"; fi
fi

# ---------- 4. summary, then start ----------
A_PORT="$(getv API_PORT)"; W_PORT="$(getv WEB_PORT)"
say ""; say "Your choices:"
say "  Container engine: $ENGINE"
say "  Database        : $([ "$(getv DB_MODE)" = external ] && echo "your own" || echo "a container on this machine")"
say "  Queue (Redis)   : $([ "$(getv REDIS_MODE)" = external ] && echo "your own" || echo "a container on this machine")"
case "$(getv S3_MODE)" in garage) s3="automatic S3 (Garage) in a container";; local) s3="this machine's disk";; *) s3="your own S3 ($(getv MINIO_ENDPOINT))";; esac
say "  Receipt photos  : $s3"
say "  AI gateway      : $([ "$(getv LITELLM_MODE)" = external ] && echo "your own" || echo "a container on this machine")"
say "  Ports           : API ${A_PORT:-8000}, web ${W_PORT:-3000}"
if [ "$YES" != 1 ]; then
  ask "Start now? (y/n)" "y"
  case "$REPLY" in y|Y|yes|Yes|YES) ;; *) say "Cancelled. Your choices are saved in .env; run ./setup.sh again any time."; exit 0;; esac
fi
say ""; say "Starting everything (the first time can take a few minutes: downloads and builds)..."
CONTAINER_ENGINE="$ENGINE" bash deploy/deploy.sh up

HOST="$(hostname -I 2>/dev/null | awk '{print $1}')"; HOST="${HOST:-localhost}"
say ""
say "Done."
say "  Web dashboard : http://$HOST:$(getv WEB_PORT || echo 3000)"
say "  Sign in with  : the API key in the .env file (INGEST_API_KEY)"
say "  Next          : open the "Get started" page in the dashboard. AI providers and models, agents, voice, news and storage are set there."
say "  IMPORTANT     : back up SECRETS_KEY from .env. It encrypts your diary and the assistant's memory; without it they cannot be read."
say "  Android app   : enter http://$HOST:$(getv API_PORT || echo 8000) and the same key."
