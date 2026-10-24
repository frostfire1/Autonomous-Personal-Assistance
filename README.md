# Autonomous Personal Assistance

A self-hosted personal finance and daily-briefing assistant. Your bank notifications and receipts become ledger entries
through a debate of AI agents with hard safety checks; every morning it prepares a voice and text briefing (your money,
sleep, weather, news, CVEs); it answers questions in Telegram; and a web dashboard and an Android app control it.
It implements `system_architecture_blueprint_v4.1`; [docs/decisions.md](docs/decisions.md) records where it deliberately
differs from the blueprint.

**The defaults are the blueprint's** (Indonesian briefing, Indonesian national news, national and cybersecurity topics,
the kunciai and Osiris AI providers, Garage S3, Postgres, Redis). **Everything else is changeable** from the web
dashboard after setup: AI providers and the model of every agent, your own agents, news countries, feeds and topics, the
CVE rule, voices and languages, storage.

## Quick start

On a Linux or macOS server:

```bash
git clone <this repo> apa && cd apa
./setup.sh
```

It checks for Docker or Podman (and offers to install one), asks **Easy** (everything in containers) or **Advanced**
(choose each part, or use your own database, Redis, S3 or AI gateway), writes `.env` with fresh random secrets, and
starts everything. Then open the dashboard it prints, sign in with the `INGEST_API_KEY` from `.env`, and follow the
**Get started** page. Details: [docs/SETUP.md](docs/SETUP.md).

For the phone: build or download the APK and follow [docs/ANDROID.md](docs/ANDROID.md).

## Documentation

| | |
|---|---|
| [docs/SETUP.md](docs/SETUP.md) | install (Easy, Advanced, manual), first run, update, backup, troubleshooting |
| [docs/CONFIG.md](docs/CONFIG.md) | what the defaults are, what is changeable where, every `.env` variable, dummy examples |
| [docs/ANDROID.md](docs/ANDROID.md) | build the APK, install, connect, permissions, background use |
| [docs/decisions.md](docs/decisions.md) | architecture decisions and differences from the blueprint |

## What is in the repo

| Path | What |
|---|---|
| `setup.sh` | the interactive installer |
| `deploy/deploy.sh` | podman or docker, no compose: `up`, `web`, `down`, `status`, `garage` |
| `backend/` | Python 3.12, FastAPI gateway, arq worker, aiogram Telegram bot, SQLAlchemy models, Alembic |
| `web/` | Next.js dashboard (overview, transactions, decisions, health, security, conversations, plus the setup pages) |
| `android/` | Kotlin app: bank notification listener, morning briefing playback, health, settings |
| `infra/` | LiteLLM routes (`config.yaml` is the blueprint's, `config.example.yaml` is a generic dummy), Postgres role bootstrap, Caddy, an optional compose file |

## How it works, in short

1. The Android app (or a Telegram message or photo) sends an event. Anything recognised by a rule or a learned
   template is parsed in code; only the unrecognised go to a model, capped per hour.
2. Circuit breakers run first. Then the debaters vote, the Arbiter decides, and the result is committed to the ledger with a
   hash-chained audit log. `/undo` is soft and logged.
3. A model never writes a figure: money, sleep and CVE numbers come from SQL and code. A model only words news items, and
   its numbers are checked against the sources.
4. `/private` conversations live in RAM only, never in the database, Redis or logs.
5. The core agents (Master, Operator, Arbiter) cannot be switched off or removed. Every other agent can be given a different
   model or switched off, and you can add your own.

## Development

```bash
cd backend
python -m venv .venv && . .venv/bin/activate          # Windows Git Bash: . .venv/Scripts/activate
pip install -e ".[dev]"
ruff check src tests && pytest tests --ignore=tests/integration
```

Integration tests need a throwaway Postgres and Redis (the database is wiped):

```bash
podman run -d --name apa-test-pg -e POSTGRES_PASSWORD=testpw -e POSTGRES_DB=apa_test -p 55432:5432 docker.io/library/postgres:17
podman run -d --name apa-test-redis -p 56379:6379 docker.io/library/redis:7-alpine
TEST_DATABASE_URL=postgresql+asyncpg://postgres:testpw@127.0.0.1:55432/apa_test \
TEST_REDIS_URL=redis://127.0.0.1:56379/15 pytest tests/integration -v
```

Use `127.0.0.1`, not `localhost` (arq's 1 s connect timeout trips when `localhost` resolves to IPv6 first). Web:
`cd web && npm install && npx next build`. Android: see [docs/ANDROID.md](docs/ANDROID.md).

## Status and honesty

Tested: the whole backend with unit tests and, against a real Postgres and Redis, integration tests with a fake model;
text extraction, the debate and the vision reading live against real providers; the briefing in English and Japanese,
topics, feeds and other countries live on a running server; the Android app builds, passes its unit tests and runs on a
phone. Not tested: Huawei Health Kit against the real service, `setup.sh` on a clean machine and its Docker or Podman
install step, the optional S3 providers other than Garage, and the GitHub Actions workflow. Real receipt photos have only
been run through synthetic fixtures. Treat anything under "Not tested" as needing a first-run check.

Security notes: plain HTTP to a private address is allowed in the app, and every request carries the API key, so reach the
server over your home network or a VPN such as WireGuard, never the open internet. Rotate `INGEST_API_KEY` if a phone is lost.
Briefing text leaves the server only for the voice and the AI providers you chose.
