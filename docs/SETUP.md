# Setup

## Requirements

- A Linux or macOS machine that stays on (a home server, a mini PC, a VPS). About 2 GB of RAM and a few GB of disk. The
  images run on x86_64 and ARM64.
- Docker or Podman. `setup.sh` installs one if neither is present (Podman or Docker, your choice).
- Optional, and addable later: a Telegram bot token (from @BotFather) and your numeric Telegram id, an AI provider
  (any OpenAI-compatible address and key), a Gemini key for the better voice.

## Easy setup

```bash
./setup.sh
```

1. It looks for Docker or Podman. If there is none it asks which to install.
2. It asks **Easy** or **Advanced**. Choose **Easy**: Postgres, Redis, Garage S3 (the bucket and access key are created
   for you), LiteLLM, the API, the worker, the Telegram bot and the web dashboard all run as containers on this machine.
3. It asks for one AI provider address and key (you may skip it and add providers in the dashboard) and a Telegram token
   (skippable).
4. It shows a summary and asks before it starts anything. The first run builds the images and can take a few minutes.
5. It prints the dashboard address. Sign in with `INGEST_API_KEY` from the generated `.env`.

Run `./setup.sh` again later and it shows a menu: update (rebuild and restart), show status, change choices, stop everything.
It never overwrites an existing `.env`: your values are kept and only missing ones are added.

## Advanced setup

Choose **Advanced** to decide each part separately:

| Part | Container (default) | Your own |
|---|---|---|
| Database | Postgres 17 | `DATABASE_URL_OWNER=postgresql+asyncpg://...` (and `DATABASE_URL_APP` for a runtime role without DELETE; if empty the owner URL is used and the no-DELETE protection is lost) |
| Queue | Redis 7 | `REDIS_URL=redis://...` |
| Receipt photos | Garage S3 container, or this machine's disk | any S3: AWS, Cloudflare R2, Backblaze B2, MinIO... (`MINIO_*` variables) |
| AI gateway | LiteLLM container | `LITELLM_BASE_URL` and `LITELLM_MASTER_KEY` of a gateway you run |
| Ports | API 8000, web 3000 | `API_PORT`, `WEB_PORT` |

The choices are saved in `.env` as `DB_MODE`, `REDIS_MODE`, `S3_MODE`, `LITELLM_MODE`. With an external database the roles that
protect the ledger (a runtime role that can never DELETE) are not created for you: run the statements in
`infra/postgres/01-roles.sh` against your database yourself.

## Manual setup (no script)

```bash
cp .env.example .env          # fill in secrets and choose the *_MODE values
bash deploy/deploy.sh up      # podman or docker is detected; CONTAINER_ENGINE=docker forces one
```

`deploy.sh` commands: `up` (build and start everything the `.env` asks for), `web` (only the dashboard), `status`,
`down` (stops containers; volumes, so your data, are kept), `garage` (only the bundled S3). An optional
`infra/docker-compose.yml` exists for compose users but `deploy.sh` is the maintained path.

## First run

1. Open the dashboard, sign in, and look at **Get started**. It checks the database, queue, AI gateway, extra providers,
   storage, Telegram, Gemini, web search and Google Sheets, and says how to fix each missing one.
2. **AI Models**: add providers (address and key), press "Fetch model list", choose a model for each role and each agent.
   Roles you leave on "Default" use the LiteLLM routes in `infra/litellm/config.yaml`.
3. **Settings**: language, voice (press "Hear a sample"), news topics, words to follow, weather city or GPS.
4. **News and CVEs**: home country, feeds, your own topics, the CVE rule and watch list.
5. **Storage**: this machine's disk or S3, with a connection test.
6. Telegram: write to your bot. The bot only obeys `TELEGRAM_OWNER_ID`.
7. Install the phone app: [ANDROID.md](ANDROID.md).

### The AI gateway needs at least one working provider

- Blueprint default: put your kunciai and Osiris keys in `.env` (`KUNCI_API_KEY`, `OSIRIS_API_KEY`); `infra/litellm/config.yaml`
  is the blueprint's routing and fallbacks.
- Your own provider on a fresh install: `setup.sh` copies `infra/litellm/config.example.yaml` to `config.yaml` (when you have
  no kunciai or Osiris key) and writes `MY_API_BASE` and `MY_API_KEY`. The model names in that file are **placeholders**:
  replace them, or assign a model to each role in the dashboard (AI Models), which needs no restart.

## Update

```bash
git pull
./setup.sh        # choose "Update"
# or: bash deploy/deploy.sh up
```

Database migrations run automatically on every `up`.

## Backup

Everything lives in named volumes: `apa-pgdata` (Postgres), `apa-redisdata`, `apa-garage-meta` and `apa-garage-data` (S3), and
`apa-evidence` (photos in local mode).

**Database:** `./deploy.sh backup` writes a compressed dump to `BACKUP_DIR` (default `./backups`, readable only by root) and removes
dumps older than `BACKUP_KEEP_DAYS` (14). Run it nightly from cron, for example
`10 3 * * * cd /opt/apa && ./deploy.sh backup >> /var/log/apa-backup.log 2>&1`. Point `BACKUP_DIR` at another disk (an HDD) to keep
copies off the system disk, and set `BACKUP_REQUIRE_FILE` to a marker file on that disk so nothing is written to the system disk while it is unmounted, and copy the folder to another machine now and then: a backup on the same disk does not survive the disk.
Restore into an empty database with `gunzip -c apa-YYYYMMDD-HHMMSS.sql.gz | podman exec -i postgres psql -U apa_owner -d apa`.

**The keys:** copy `.env` somewhere safe, **separately from the dumps**. It holds `SECRETS_KEY`: the diary, memory and stored tokens in a
dump can only be read with it, and anyone who has both the dump and the key can read everything.

## Uninstall

`bash deploy/deploy.sh down` removes the containers. To delete the data as well, remove the volumes
(`podman volume rm apa-pgdata ...`). This cannot be undone.

## Troubleshooting

| Symptom | Likely cause and fix |
|---|---|
| `setup.sh` says Docker cannot be used by this user | run it with `sudo` or add the user to the `docker` group |
| Podman build fails with isolation or crun errors | `deploy.sh` already builds with `--isolation=chroot` when run as root; as a normal user use rootless podman |
| Dashboard says 401 | the key is `INGEST_API_KEY` in `.env`; after changing it restart with `deploy.sh up` |
| Get started shows the AI gateway as unreachable | the litellm container may still be starting (wait a minute) or `config.yaml` references an unset environment variable |
| The briefing voice is always the Microsoft voice | Gemini's free quota ran out (HTTP 429) or `GEMINI_API_KEY` is not set; the preview says when a backup voice spoke |
| An extra feed shows nothing | press Test next to it on the News page; the address must be a public RSS or Atom feed |
| No messages from the Telegram bot | `TELEGRAM_BOT_TOKEN` and `TELEGRAM_OWNER_ID` in `.env`, then `deploy.sh up` |

Logs: `podman logs api` (or `worker`, `bot`, `litellm`, `web`).
