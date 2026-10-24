# Configuration

Two layers, on purpose:

1. **Defaults = the blueprint.** What you get if you change nothing.
2. **Everything else is dynamic**, set in the web dashboard after setup and stored on the server (table `policy_rules`).
   Changes apply within about ten seconds, with no restart. Only secrets read when a container starts stay in `.env`.

## The defaults (the blueprint)

| Area | Default |
|---|---|
| Briefing language and voice | Indonesian; Gemini voice Orus/Zephyr when a Gemini key exists, else Microsoft Gadis |
| Briefing time | prepared 25 minutes before 06:30 |
| News topics | National news and Cybersecurity and CVEs |
| National news country | Indonesia (Antara, CNN Indonesia, CNBC Indonesia, Detik, Tempo, Republika) |
| CVE rule | CVSS 7.0 or higher (high and critical) plus anything CISA lists as exploited, last 24 hours |
| AI providers | kunciai primary, Osiris fallback, through LiteLLM (`infra/litellm/config.yaml`) |
| Agents | the blueprint's agents with their roles; five debaters; Arbiter decides |
| Photo storage | Garage S3 (container) |
| Database, queue | Postgres 17, Redis 7 (containers) |

## Banks, wallets and currency (not only Indonesia)

- **Home currency:** one ledger, one currency, no conversion. Default IDR (the blueprint). Choose another in **Settings** (IDR, USD, EUR,
  GBP, JPY, SGD, MYR, AUD, INR, KRW, CNY, THB, PHP, VND). It sets how money is written (dashboard, Telegram, briefing) and which
  bank-notification amounts are read: a notification in another currency is not a ledger entry and goes to review.
- **Any bank or wallet app, any country:** on the phone, pick the apps to listen to. The picker lists apps that look like banks and has a
  "Show all apps" button, so any app can be ticked. A ticked app is an approved wallet.
- **Notification wording:** known Indonesian bank formats are read by rules. Anything else is read by the model **once** (money in or out, which
  merchant, or "not a payment", such as money moved between your own accounts), and that wording is remembered for the app: the next
  notification of the same format is handled from the remembered template with no model call. The amount is always taken from the text itself,
  nothing is learned when the model contradicts clear words in the text, and learned formats follow the home currency. If a learned format
  turns out wrong, clear it: `DELETE /templates?package=<app>` (or without `package` for all); `GET /templates` shows what was learned.
  Unfamiliar wording the model reads is capped per hour. English wording
  (spent, paid, received, deposited, refund...) is understood by the built-in rules.
- **Known limits:** the receipt-photo reading and the shorthand ("18k", "1,5jt") still assume whole numbers, so currencies with cents
  lose the decimals there; and the Indonesian bank labels (BCA, Mandiri...) are display names only. Changing the currency does not
  convert old entries.

## What you can change, and where

| What | Where |
|---|---|
| Language (Indonesian, English, Japanese, Chinese, Korean, German, French, Spanish), voice (Gemini's 30, Microsoft's per language, a custom OpenAI-speech provider), news topics, words to follow, weather city or GPS | dashboard **Settings**, or the Android app |
| AI providers (any number: address and key), a model for each role and each agent, switching agents off, your own agents, speech engine and model | dashboard **AI Models** |
| Home country for national news, switching built-in feeds off, your own feeds and topics, the CVE rule and watch list | dashboard **News and CVEs** |
| Photo storage: this machine's disk or any S3, with a connection test | dashboard **Storage** |
| What is working and what is missing | dashboard **Get started** |
| Budgets | Telegram, for example "budget food 1.5 million"; the AI asks you to confirm |
| Wake time, bank apps to listen to, sync interval, background settings, server address and key | the Android app |
| Secrets read at start (Telegram token, Gemini, Firecrawl, Brave, Sheets, Huawei, HMAC) and where each part runs | `.env`, then `bash deploy/deploy.sh up` |

### Agents

- **Core, never off or removed:** Master (`A-00`, routes every message), Operator (`A-01`, the code that runs the pipeline),
  Arbiter (`A-10`). You may change a core agent's model.
- **Built-in, can be switched off or given another model:** Finance, Health, Tech, Social, Mental Health, Planner, Macro and the
  rest. An agent's own model beats its role's model.
- **Your own (up to 12):** a name, how it behaves, when the Master should send a question to it, which data it may read
  (finance, health), and optionally its own provider and model. It reads and answers in Telegram chat; it cannot change the
  ledger and does not vote in transaction debates.
- Roles: routine (extraction, news, translation), vision (receipts, must read images), arbiter, mental health, macro, research.
  The mental-health and research roles carry `/private` content to the provider you choose.

### The assistant

Free messages in Telegram go to a conversational assistant that knows the context: it remembers the last 16 messages for 6 hours, sees
your latest transactions and the message you replied to, and can look things up (transactions, spending, budgets, balances, sleep, CVEs,
news) with read-only tools, so every figure comes from your data. It can talk about anything and asks one short question when unsure.
**It cannot change your ledger by itself**: recording, editing (including several transactions at once), undoing and budgets are
always a confirmation card. Tell it "remember that ..." to keep a short note about you (never passwords or card numbers); the notes are
listed, and deleted, in **Settings** in the dashboard.

### News and CVEs

Feeds are read every few minutes. A topic is a bucket of feeds. National news uses the home country's built-in feeds (12
countries) and its own ranking; every other topic is ranked by how many outlets cover a story and how fresh it is, and
the briefing takes only the top few. Foreign-language stories are summarized in your briefing language.

CVE rule: minimum CVSS, hours to look back, "always include CISA-exploited", and a watch list of vendor or product words
that always get through and are listed first.

## Dummy examples (for your own config)

`.env` (all values are dummies; see `.env.example` for every variable):

```
S3_MODE=external
MINIO_ENDPOINT=s3.example.com:443
MINIO_ACCESS_KEY=AKIAEXAMPLE
MINIO_SECRET_KEY=example-secret
MINIO_BUCKET_EVIDENCE=receipts
MINIO_SECURE=true
MY_API_BASE=https://api.example.com/v1
MY_API_KEY=sk-example
```

`infra/litellm/config.example.yaml` is a complete generic LiteLLM file with placeholder model names, one entry per AI role.
Copy it to `config.yaml`, replace the models (or leave it and choose models in the dashboard).

## `.env` reference

| Variable | Meaning | Default |
|---|---|---|
| `DB_MODE`, `REDIS_MODE`, `S3_MODE`, `LITELLM_MODE` | where each part runs | `bundled`, `bundled`, `garage`, `bundled` |
| `API_PORT`, `WEB_PORT` | published ports | `8000`, `3000` |
| `DATABASE_URL_OWNER`, `DATABASE_URL_APP`, `REDIS_URL` | external database and queue | |
| `POSTGRES_*`, `APP_DB_*` | bundled Postgres (owner migrates, app role never DELETEs) | generated |
| `INGEST_API_KEY` | the key for the dashboard and the Android app | generated |
| `INGEST_HMAC_SECRET`, `INGEST_RATE_PER_MIN` | signed requests; call cap | off, 120 |
| `LITELLM_MASTER_KEY`, `LITELLM_BASE_URL` | the AI gateway | generated, bundled |
| `KUNCI_API_*`, `OSIRIS_API_*`, `MY_API_*` | provider addresses and keys used by the LiteLLM files | |
| `LLM_MAX_CONCURRENCY`, `LLM_MAX_RPM`, `DEBATE_QUORUM` | AI call limits; votes needed to commit | 3, 48, 3 |
| `TELEGRAM_BOT_TOKEN`, `TELEGRAM_OWNER_ID` | the bot and the only account it obeys | |
| `EVIDENCE_BACKEND`, `EVIDENCE_DIR`, `MINIO_*` | photo storage (the dashboard overrides) | `local`, S3 values |
| `BRIEFING_WAKE_TIME`, `BRIEFING_LEAD_MINUTES` | when the briefing is prepared | `06:30`, 25 |
| `WEATHER_CITY`, `WEATHER_ENABLED` | weather line | Jakarta, on |
| `TTS_ENABLED`, `TTS_ENGINE`, `TTS_VOICE`, `GEMINI_API_KEY`, `GEMINI_TTS_MODEL` | the voice | on, auto |
| `FIRECRAWL_API_KEY` (+`FIRECRAWL_URL`), `BRAVE_API_KEY` | web search for `/private` research | Wikipedia only |
| `NVD_API_KEY` | higher NVD rate limit | |
| `PRICE_FEED_URL` | staple-price JSON feed | |
| `SHEETS_CREDENTIALS_FILE`, `SHEETS_SPREADSHEET_ID` / `SHEETS_URL` | one-way Google Sheets mirror | |
| `SECRETS_KEY` | **required**: encrypts the diary, the assistant's memory and stored tokens (`setup.sh` makes one). Back it up: without it they cannot be read | generated |
| `HUAWEI_CLIENT_ID`, `HUAWEI_CLIENT_SECRET` | Huawei Health Kit sync (untested live) | |
| `NOTIFICATION_LLM_PER_HOUR` | unrecognised bank notifications the model may read per hour | 30 |
| `PHONE_SILENT_HOURS` | hours without a check-in from the phone app before Telegram says so | 2 |
| `TZ` | timezone | `Asia/Jakarta` |
