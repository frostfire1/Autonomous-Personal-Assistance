# Architecture Decisions — resolving Blueprint v4.1 gaps

Status: **draft for review** · Source: `system_architecture_blueprint_v4_1.pdf`

Decisions D1–D3 were confirmed by the owner. Everything in §4 (missing tables) is a proposal
to review before it becomes the first Alembic migration.

---

## D1. Job queue & scheduler: **arq** (replaces BullMQ)

BullMQ is Node.js-only; the backend is Python/FastAPI. arq is Redis-backed, async-native
(the 5-agent debate is `asyncio.gather` over LiteLLM calls) and has cron built in.

Jobs are plain `async def` functions taking a context dict, so a later switch to Taskiq stays cheap.

| Job | Schedule | Owner |
|---|---|---|
| `process_inbound_event` | on enqueue | A-01 → debate → A-10 |
| `sheets_sync` | every 60 s | Sheets worker |
| `cyberramen_poll` | every 10 min | A-13 |
| `news_ingest` | every 15 min | A-09 / A-13 |
| `huawei_healthkit_sync` | every 30 min | A-03 / A-06 |
| `pihps_collect` | Mon–Fri 08:00 WIB | A-09 |
| `weekly_digest` | Sun 20:00 WIB | A-12 |
| `weekly_reconciliation` | Sun 22:00 WIB *(time not in blueprint — proposed)* | A-11 |
| `monthly_digest` | daily 20:00 WIB, runs only when tomorrow is day 1 (arq cron has no "last day of month") | A-12 |
| `reminder_sweep` | daily 07:00 WIB *(proposed)* | A-08 |
| `supplement_stock_tick` | daily 00:05 WIB | A-03 |
| `cold_storage_archive` | daily 03:00 WIB | infra |

**Rule:** `/private` (A-06) content is **never** enqueued. Redis persists to disk (RDB/AOF), which
would break the zero-disk guarantee. Private sessions run in-process in the bot, and the 5-minute
idle wipe is an in-memory `asyncio` timer, not an arq job.

---

## D2. Deliberation policy: **always debate**

Every ledger-affecting event goes through the full parallel debate (A-02, A-03, A-04, A-05, A-07)
and the A-10 Arbiter, as §4.2 specifies. The Phase 2 "fast path" now applies only to **parsing**:

- Regex/shorthand parsing and `merchant_cache` replace LLM *extraction* (no deepseek call to pull
  amount/merchant/category out of text). They never skip deliberation.
- `merchant_cache` stats are injected into agent prompts as baseline context.
- **Phase 2 gate restated:** "≥90% of routine bank notifications and shorthand entries parsed
  deterministically (no LLM extraction call); 0% parse errors."
- **Phase 1 gate restated:** "< 200 ms from notification to `inbound_events` row." The ledger commit
  happens after the debate (target < 1.5 s per §4.8), so it cannot share the 200 ms budget.
- `transactions.execution_mode` drops `cache_hit` and `single_auto`.

Pipeline order (a circuit-breaker check runs **before** the debate, so quarantined events don't spend tokens):

```
inbound_events → A-01 sanitize + PII strip + injection filter → deterministic parse (LLM fallback)
  → circuit breakers (velocity / single-tx ceiling / liquidity floor)
      ├─ tripped → status = 'quarantined', Telegram alert, debate deferred
      └─ clear   → parallel debate → A-10 consensus → single DB transaction:
                    ledger + wallets + budgets + health accumulators + explainability_log
                 → one Telegram summary with /undo token
```

---

### D2 addendum (Phase 3 implementation): what is and is not debated

Implemented in `backend/src/apa/pipeline/commit.py`. Decisions taken while building it:

- **Not debated: income and ATM withdrawals.** They are not spending, so there is nothing for the
  five agents to judge. They commit deterministically (`execution_mode = 'deterministic_transfer'`,
  `agents_invoked = []`) and the audit row says so. Every *expense* is still debated.
  `transactions.direction` (`out`/`in`) was added; income has `net_user_expense = 0`.
- **Quarantine, not rejection.** A bank notification means the money has already moved, so a breaker
  trip (including the liquidity floor) stores the row as `status = 'quarantined'`: no debate, no
  wallet/budget/merchant effect, one alert. `net_user_expense` keeps the held amount so a later
  release is a status flip; **every spend report must filter `status = 'committed'`**.
- **Lockdown is persistent.** A velocity trip sets `policy_rules.lockdown_active = 1`; until it is
  cleared, every expense and withdrawal is quarantined without a debate. Income still commits.
  Nothing clears it yet (Web UI, Phase 4+).
- **Liquidity floor only applies to a cash wallet that exists.** Bank balances are not tracked
  (the schema has no balance table), so an unknown balance never trips the floor, and a cash
  expense with no `cash_wallets` row is recorded without debiting anything.
- **Debate quorum (replaces "commit flagged on a total outage"):** an expense needs at least 3 of
  the 5 debaters to give a valid vote (`DEBATE_QUORUM`, clamped to 1..5; invalid JSON is not a vote).
  Short of that the job retries (3 tries, 30 s apart) and nothing is committed or announced. If the
  last try still has no quorum the transaction is **held**, not committed: `status = 'quarantined'`,
  reason `no_quorum`, an alert with `/release_<token>`, and the audit row records "N/5 agents
  answered, quorum is 3". `/release` re-runs the debate once the LLM is healthy; if it still has no
  quorum it stays held and tells the owner. Committing on 0 or 2 votes would put an `Uncategorized`
  entry into budgets and merchant history on the luck of which agents answered. The money has already
  moved either way; a held row records that it did without applying it. Costs: each retry re-asks all
  five (partial votes are not kept between tries), and `/release` is now a human step after an outage.
- **Telegram line is formatted deterministically**, not phrased by the Arbiter LLM: cheaper, and
  it cannot hallucinate an amount. Sent after commit; a Telegram failure never fails or re-runs a
  committed transaction (the `/undo` token is already stored).
- **Fees:** a parsed admin fee is added to the total and noted. Splitting it into a `Bank Fees`
  line is left to reconciliation (blueprint 4.6 tier 2) to avoid double counting.

### Model routing and fallbacks (probed and verified 2026-10-23)

`infra/litellm/config.yaml` was rebuilt from what the key really serves. The Osiris dashboard lists
18 *allowed* models, but allowed is not served: 8 returned `model_not_found`
(`glm-4.6`, `glm-4.6v`, `glm-4.7`, `minimax-m3`, `osiris-claude-haiku-4.5`,
`osiris-claude-sonnet-4.5`, `qwen3.7-max`, `qwen3.7-plus`) and 3 were erroring
(`deepseek-v4-flash` 502, `qwen3.8-max` 502, `glm-5.1`). Re-probe with `scripts/probe_models.py`
after any key or plan change, then update the config comment and `tests/test_litellm_config.py`.

- **Chains** (3 fallbacks each, fastest valid-JSON model first, the pricier `deepseek-v4-pro` as a late
  resort; every vision hop verified to read an image): routine `deepseek-v4-flash` -> glm-5.3 ->
  glm-5.2 -> deepseek-v4-pro. Vision `glm-5v-turbo` -> glm-5.3 -> glm-5.2 -> kimi-k2.5.
- **Primaries that are not served were replaced, not kept:** Arbiter is now `glm-5.3` (blueprint:
  haiku-4.5) and the `/private` route is `glm-5.2` (blueprint: sonnet-4.5). The `/private` choice is
  **provisional**: nobody has judged glm-5.2's quality for emotional support. Revisit before A-06.
- **Verified against the real LiteLLM Router and the live endpoint** (scratch environment, not the
  project): a 502 primary retries once, then falls back and succeeds; a `model_not_found` (400)
  primary falls back immediately; a dead primary is then skipped by the cooldown (it did not appear
  in a later burst); the vision route reads a receipt. `num_retries` is 1 because retries spend from
  the rate limit.
- **Rate limit finding:** Osiris states 30 requests/minute, but it rejects parallel bursts well
  before that: of 5 parallel calls to one model, 2 got `RateLimitError` in an otherwise quiet minute.
  The chain absorbed it (5/5 answered) at the cost of extra upstream calls. The debate sends exactly
  that pattern (5 parallel calls on one route). **Done:** `LLMClient` caps in-flight requests at
  `LLM_MAX_CONCURRENCY` (default 3). The cap is process-wide on the shared client, not per debate,
  so several transactions arriving together share it. Tested against a fake provider that returns 429
  above 3 concurrent requests: with the cap a debate is never rejected; without it, exactly 2 of 5
  calls were. The debate quorum above closes the "2 agents decide" gap. **Cost:** with a cap of 3 a
  debate runs in two waves, so it is slower than five parallel calls would be if the provider allowed
  them; the real effect on latency has not been measured against Osiris.
- **Known weaknesses of this test:** one model-quality judgement was not made (see `/private`
  above); latency is slow (`glm-5.3` 1-3 s per call, slowest debater 16 s in an early run), so the
  blueprint's "under 1.5 s per debate" does not hold with these models; fallback-on-bad-JSON is
  not a LiteLLM feature (a model that answers with invalid JSON is not retried on another model).

### Receipt photos (Phase 2b, first slice: ONE image per receipt)

`bot/photo.py` -> `ingestion/photo_intake.py` -> job `read_receipt` (`pipeline/receipt.py`,
`ingestion/receipt_extract.py`) -> the normal `commit_event` (breakers, debate, Arbiter, ledger).
Verified live on 2026-10-24: a synthetic printed receipt was read exactly (merchant, 3 items, subtotal,
tax, 58.300, QRIS, 12:20) in 5-7 s, and the whole path photo -> committed transaction took 23 s.

- **Gated, not trusted.** The vision model's reading must pass: strict schema; an arithmetic gate (items,
  discount, tax and service charge must add up to the total, tax-inclusive receipts allowed), so a
  hallucinated total cannot also fit the line items it read; a typed caption amount must roughly match
  (Rp 500 or 2%, people round); confidence >= 0.85 (capped at 0.95). **Limits:** the tolerance is Rp 100
  (cash rounding), so a misread digit smaller than that passes; a receipt with no readable item lines
  cannot be cross-checked and goes to review; it was only tried on a synthetic, cleanly drawn image,
  not on crumpled, skewed or thermal-paper photos.
- **Wallet:** a cash receipt (or a caption naming a wallet) fixes it; a card/QRIS receipt does not say
  which bank, so it is stored as `unknown` until a bank notification completes it (below).
- **Fusion, so a purchase is never counted twice.** Same total, within 12 h. A receipt that matches ONE
  committed bank transaction is attached to it (items, evidence, `dual_verified`, no debate, no new
  transaction). A bank notification that matches ONE receipt transaction with an unknown wallet completes
  that wallet. Cash receipts never fuse; a named wallet must agree. **More than one match is held**
  (`quarantined`, reason `ambiguous_fusion`) with `/release` and `/dismiss`, because guessing either
  double counts or loses a purchase. `/release` commits it as its own purchase with its items and evidence.
  Known gap: a receipt photographed more than 12 h after the purchase will not fuse and will double count.
- **The same photo is never counted twice:** content-addressed storage plus an intake check (a recorded
  or in-flight image is refused) plus a unique index on the image checksum as the database backstop for
  races. A photo that was rejected or failed can be resent. **An undone receipt's photo cannot be
  resubmitted** (its checksum is still recorded): take a new photo.
- **Bytes never travel through Redis or the events table**, only the URI and checksum. Items and OCR text
  are sanitised (PII, injection phrases) before storage and sit inside the debate prompt's untrusted fence.
- **Not built yet:** nutrition labels and warranty cards (blueprint cases 3 and 4), and the health/nutrition
  use of the items. Albums are now supported: see the next section.
- **Object store: MinIO cannot be pulled.** `minio/minio` is refused on docker.io and on quay.io (latest and a
  pinned 2025 release), so `MinioEvidenceStore` was tested only against a fake client, and the compose
  `minio` service will not start. Compose now defaults `EVIDENCE_BACKEND=local` on a shared `evidence`
  volume so receipts work today. **Choose a replacement S3 store** (and re-point the WAL archive) before
  production; the blueprint's cold-storage and PITR plans depend on it.

### Multi-image receipts (Phase 2b, second slice): albums

A Telegram album of up to 4 photos is ONE purchase: a long receipt photographed in pages, a receipt plus its
payment slip (EDC / QRIS / e-wallet / transfer proof), or both. Verified live on 2026-10-24 with 3 drawn
photos (2 overlapping pages + an EDC slip): the model separated the pages, code removed the repeated lines
(7 items), the slip matched, and the album committed as `dual_verified` in 19 s.

- **Collecting:** every photo of an album is its own concurrent Telegram message. All append to ONE event keyed
  on the album id (the first photo creates it with an upsert, so two photos racing to be first cannot make two
  events; appends take a row lock). Pages are ordered by Telegram message id, never arrival order. One read job
  is queued, deferred a few seconds, and it waits until the photos have been quiet for
  `ALBUM_SETTLE_SECONDS` (default 3; on the last try it reads what it has). A photo that arrives after the
  reading started is refused as late; a 5th photo is refused; a photo already counted is refused.
- **The model does not merge.** It classifies each photo (receipt page / slip / other) and reports each photo's
  lines as printed. **Stitching is deterministic code** (`ingestion/receipt_assemble.py`): it finds the
  overlap between consecutive pages by item equality and keeps the interpretation whose items add up to the
  grand total, so a genuine repeat at a page boundary (the same item bought twice) is kept when the arithmetic
  says so, and an album where no interpretation adds up is refused.
- **Dual verification:** the slip's total is the amount actually charged. It must match the receipt within
  rounding (`dual_verified`); a surplus up to 25% of the receipt or Rp 10.000, whichever is larger, is a tip
  or service charge that only the slip shows and is split out as its own line linked to the slip
  (`split_reconciled`); anything else, or a slip whose amount cannot be read, is refused. The slip's
  authorisation time, not the send time, becomes the purchase time. A slip means the payment was not cash.
- **Evidence:** each image keeps its role (`receipt_primary`, `receipt_continuation`, `payment_proof_slip`;
  a stray photo is stored but not filed as evidence), each item links to the page it was read from, and the
  slip's card digits and batch are kept on its file record. A bank-notification fusion keeps a slip-based
  verification and upgrades the others to `dual_verified`. The "one image backs one transaction" index now covers
  every page and slip, not just the first.
- **Not verified:** only drawn, cleanly printed fixtures were tried, never real thermal-paper photos; albums
  of 3 and 4 photos were exercised with a fake model, only 2 pages + a slip with the real one; two real
  receipts mistakenly sent in one album would be refused (their items cannot sum to one total), not split.
  Only 4 of 5 debaters voted in the live run, as before.

### First live run on real Osiris (2026-10-24) and the category constraint it forced

Run through the **real LiteLLM proxy** with `infra/litellm/config.yaml`, my real `LLMClient` and a real
Postgres (`tests/integration/test_live_osiris.py`, skipped unless `LIVE_LLM=1` because it spends tokens).
Result: extraction was right on every case that parsed (30.000, 5.000.000, 200.000 + fee 2.500) and the
prompt-injection attempt was ignored; a transaction went through 4 to 5 debaters, committed, and the audit
chain verified. Everything was served by `glm-5.3`: the primary `deepseek-v4-flash` was still returning 502,
so the proxy's fallback was exercised for real.

- **Defect found and fixed:** the agents answered `food_beverage`, `coffee` and `Food & Beverage` for one
  coffee, and the winner `food_beverage` matched no budget, so the spend would never have been charged.
  The debate prompt now lists the allowed categories (the user's `budget_rules` categories for that month,
  or a built-in list when there are none) and each vote's category is mapped onto that list, ignoring only
  case, punctuation and `&` vs `and`. **Nothing is guessed by similarity**: an unknown category simply
  does not vote, and if no vote names one the transaction is `Uncategorized` (a merchant default that is
  not on the list is ignored too, and `Uncategorized` is never learned as a merchant's default). The
  model's literal answer stays in `agent_debate_transcripts.structured_output`.
- **Measured, not assumed:** a debate took 5 to 10 s on a quiet run (cap of 3 means two waves), so the
  blueprint's "under 1.5 s" does not hold with these models. Single calls took 1.7 to 8 s.
- **The "missing debater" was a bug of mine, now explained and fixed.** Six real debates with every raw answer
  recorded: 7 of 10 failed votes were valid votes wrapped in a markdown fence (```` ```json ... ``` ````)
  despite JSON mode, and it hit every agent, not A-04 in particular. The extraction and receipt parsers
  stripped fences, the debate's did not. `LLMResult.json()` now strips them for every consumer. Re-measured:
  format failures 0 (from about 7 in 30 calls), 5 of 6 debates with all five votes (from 3 of 6).
- **Still happening, not fixed: provider stalls.** About 3% of calls (3 of 60 in two runs) hang until the 45 s
  client timeout (`ReadTimeout`); one debate got 0 of 5 votes after 49 s and another took 50 s with 3 of 5.
  The proxy's own timeout is also 45 s, so the client gives up at the instant the proxy would start its
  fallback, and a stalled call holds one of the 3 concurrency slots for the whole 45 s. A reasonable fix is a
  shorter proxy timeout (about 20-25 s, so a stalled call falls back quickly), a longer client timeout than the
  proxy's (so it can wait for that fallback), a per-deployment timeout for the slow vision route, and no
  retry of the same stalled model. **Not done**: it is a tuning decision and I could not provoke a real stall
  on demand to verify it. Also seen: 1 reply in 60 was malformed JSON (a bad delimiter), which only a retry
  could recover.
- **Still not verified live:** the vision route on a real receipt, any real bank notification, the `/private`
  model, and the debate under real concurrent load.

### Blueprint v2 is the base; "self-finance with no manual command" (2026-10-24)

You supplied `blueprint (1).pdf` (v2) and said v4.1 was derived from it and had drifted. This section records
what that changed. Principle: the system runs itself; every manual command from v2 becomes an automatic
behaviour. Chat, the menu and confirmation cards stay as **optional extras** (your answer), never required.

| v2 says | Now |
|---|---|
| Merchant/pattern known -> fast path, no debate; new/ambiguous -> full debate (4.2) | **Restored** (D2 had become "always debate"): `pipeline/commit.py::fast_path`. Breakers first; then a merchant verified by a debate or by you within 30 days, 3+ visits, amount within 2 sigma (or 2x the mean with few samples), no receipt/split, within budget -> categorised from the cache with **no model call**. Anything else debates. Releases always debate. `FAST_PATH_ENABLED=false` switches it off. Verified in tests with 12 deliberate breakages. |
| Dynamic Agent Router: only relevant agents are called | `chat/master.py`: one small call decides intent and which specialists to run (code has already tried every deterministic path, A-01 style). A greeting costs 1 call (was 3), a question 2. Measured live: 1.3-2.1 s per decision. |
| Weekly/monthly digest pushed (4.7) | **Built** (`reporting/digest.py`, A-12): numbers only from SQL, Sunday 20:00 and the last day of the month, once per period, silent for an empty period. Goal progress is missing because the goals table is not built. |
| `/private` ephemeral mode (4.3) | **Built** (`private_mode/`): RAM only, no DB/Redis/log content, `deleteMessage` of every message on `/selesai`, 5 min idle, 60 min max or bot stop; fixed crisis message (code-side wording check plus model flag); autonomous recovery reallocation (<= 15% of the source's base, <= Rp 200.000, never below what was spent, only from non-essential discretionary budgets) recorded with a neutral label. The LiteLLM proxy it passes through has message logging switched off (`turn_off_message_logging`, no spend-log prompts, no database behind it, no callbacks or cache), pinned by a test, and the web UI's logs page states that `/private` never appears there. **Caveats:** the text of each turn goes to the model provider; Python cannot zero memory (best effort); sessions are lost on a bot restart without deleting their messages; verify the 119 ext. 8 number before relying on it. |
| Confirmation buttons `[Terapkan]/[Abaikan]` | Not used for automatic behaviours. Kept only where *you* ask the AI to change data in chat (see below). |
| A-11 reconciliation | **Built** (`reconcile/`): a bank statement (PDF or photo, or `/mutasi`-style caption) is read by vision, must pass an exact arithmetic check (opening + lines = closing, running balances) or it is refused whole, then matched to the ledger in three tiers (exact within 2 days, known fee folded in, catch-up entry). Safety stops: more than 60% unmatched on 5+ lines writes nothing unless you say "catat semua"; at most 40 catch-ups per run; the same file is never processed twice. Entries are flagged `reconciliation_catchup`, audited, undoable 24 h. Weekly ledger self-audit runs Sunday 22:00. Verified with 18 deliberate breakages. |
| A-09 news / cyber / briefing | **Built** (`news/`): RSS and CVE (NVD, CISA KEV, EPSS) ingestion through an SSRF-safe fetcher (public addresses only, DNS64 judged by the embedded IPv4), ranking by code, the model only summarises text it was given and ambiguous stories are triaged (cached). Urgent items push once; the short briefing is precomputed 05:30 and served at `/briefing/short` for the Android app. |
| A-09 price intelligence | **Built** (`prices/`): staple unit prices come from your own receipts (size must be printed: "Beras 5kg"), an optional `PRICE_FEED_URL`, or `POST /prices/ingest`. A rise counts only above 10% and seen in two consecutive observations. The monthly cost is basket quantity x rise per unit (the basket quantities and baselines are editable guesses in `price_basket`). Budget shifts to Groceries from Social, Entertainment, Shopping in that order, each limited to 15% of the source's base and never below spent, within the target's own band; essential and sinking-fund budgets never give. Logged in `budget_rebalance_log` with an audit row, told once a day, with a news headline offered only as a labelled guess. **Caveat:** no official PIHPS feed was usable (Bank Indonesia's pages are web forms, the public API needs a key), so until you provide a feed the signal is your own receipts. |
| Health | **Built** (`health/`): Health Connect payloads from the Android app, intake estimates and warnings at 80% of a limit, stress shield, fatigue defence (feeds the debate). **Huawei Health Kit client is written from the documented REST shape and is untested against the real service** (no credentials existed); it idles until `HUAWEI_CLIENT_ID/SECRET` and `SECRETS_KEY` are set. |
| Sheets sync | **Built, untested against real Google** (`sheets/`): one-way mirror using a service account (`SHEETS_CREDENTIALS_FILE` + `SHEETS_SPREADSHEET_ID` or `SHEETS_URL`; share the sheet with the service account). The sheet's first column decides what is already there, so there is no cursor; changed rows are corrected in place; values are sent RAW so nothing can run as a formula. |
| HMAC and rate limits (v2 4.5) | **Built** (`gateway/routers/ingest.py`): the API key, plus (when `INGEST_HMAC_SECRET` is set) a signature over `timestamp.METHOD.path.` and the exact body within 5 minutes, plus 120 calls/min (`INGEST_RATE_PER_MIN`), failing open if Redis is down. The Android app signs when given the secret. |
| Android | `android/` (package `id.frostfire.apa`): compiles into a debug APK (AGP 8.13.2, Kotlin 2.2.20, compileSdk 36), lint 0 errors, 23 unit tests pass. **Never installed or run on a device**, so expect small runtime fixes. |
| Learned notification templates | `ingestion/templates.py`: wording the fixed rules cannot read goes to the model; when the model reads one correctly its wording is saved as a template (merchant, amounts and reference numbers become slots). After **two** agreeing examples (same direction and channel) later notifications of that wording are read by the rules with no model call. The amount always comes from the text, never the template; templates never apply to text the promo/OTP/failure filters reject; a contradiction retires a template; everything still goes through the full commit pipeline. Stored in `policy_rules`. |
| `/private` companion | Warmer prompt (reacts to what was said, remembers earlier details, one gentle question, no rushing to fixes) with skills the model names and code runs: breathing, grounding, journaling prompts (fixed text), and web search, only when the owner explicitly asks, with the query cleaned of personal data, shown in the reply, never logged, as a research session of its own: a planner agent sees only the topic (never the conversation), plans up to 3 queries written for the engine in use, each is searched exactly once in parallel (never retried with other wording), results are merged and summarised without greetings or medication advice. Engines in order: Firecrawl (`FIRECRAWL_API_KEY`), Brave (`BRAVE_API_KEY`), Wikipedia. `googlesearch-python` was tested and does not work: Google now answers its text-browser identity with an unsupported-browser page and everything else with a JavaScript-only page. Wikimedia returns 403 unless the HTTP library's default Accept-Encoding is replaced (measured), which the Wikipedia calls now do. One quiet retry of a failed model call. |
| ARM | The phone app has no native code, so it runs on ARM phones. The server is x86_64; every backend dependency has an aarch64 wheel for Python 3.12 (checked), so the same image builds on an ARM host, but ARM images were not built or run here. |
| Web dashboard | `web/` (Next.js, container `web`, port 3000, LAN only): read-only pages over `/dashboard/*` and `/inspect/*`, login with the same API key (kept in the browser, sent as a header; no accounts). Nothing on it can change the ledger. The page set is basic (tables and bars, no charts); there is no HTTPS, so use it on the home network or over WireGuard only. |
| Briefing voice | Spoken briefing audio is generated on the server, cached, and played by the phone (`tts/`): Gemini speech when `GEMINI_API_KEY` is set (written from Google's documented API, **not yet run against the real service**), else Microsoft neural voices via `edge-tts` (unofficial endpoint, works today), else the phone's own voice. Text is first rewritten for speech (`tts/speakable.py`: money, numbers, percentages, versions in words). **Privacy:** the section text, including money and health lines, is sent to Google/Microsoft; `TTS_ENABLED=false` turns it off. |
| Morning schedule | The briefing is prepared `BRIEFING_LEAD_MINUTES` (25) before `BRIEFING_WAKE_TIME` (06:30): short-briefing voice clips are cached and a detailed text briefing is sent to Telegram once a day. The server does not read the phone's alarm, so the wake time is a setting. |
| Telegram briefing | `news/telegram.py`: more than the voice says: yesterday's spend by category and merchant, budgets above 70%, sleep detail, 8 national headlines with links, the day's CVE counts, up to 12 high/critical CVEs with ID, CVSS, EPSS, exploited flag, technology and versions, and security headlines. Facts only from SQL; no model writes any figure. |
| CVE detail | NVD is read in up to three pages (was capped at 200). For high/critical CVEs the affected technology and version ranges come from NVD's CPE data; CISA-exploited CVEs, which carry neither, are looked up in NVD one by one (5 per poll) and merged, and a later bare re-read never erases that detail. The voice says technology, version and score for the worst three, never the CVE number; the daily total is not spoken. |
| Notification safety | Server-side promo/offer/reminder filter (the server had none before): offers ("hingga Rp...", cashback, loans, vouchers) and bills are skipped even when they carry an amount, while a real payment with a promo line appended is kept; unusual wording from a listened app still goes to the model (dropping a possible payment is worse) but at most `NOTIFICATION_LLM_PER_HOUR` (30) per hour. Listened apps: a built-in list of Indonesian banks and wallets (package names written from memory) plus apps the owner picks on the phone (scan of installed apps by name, stored via `POST /ingest/packages`). |

Additions that are **not** in either blueprint, because you asked for them:
- **Inspector** (`inspector/`, `/log`, `GET /inspect/...`): read-only conversation log: what you sent, every
  reply, the route (regex / fast path / debate / chat agent), latency, and per transaction the audit rows and
  each agent's score and argument. Replies now carry the id of the message they answer (migration 0005).
  `/private` text is never stored, so it can never appear.
- **Batch drafts**: a message with several payments becomes a numbered draft (read by code); `1-5 breakfast`,
  `hapus 2`, `ok` are decided by code with no model call; one confirmation records all through the normal
  pipeline; the velocity breaker counts a confirmed batch once (migration 0003).
- **Data steward (A-14)**, strictly get / add / edit (`steward/`): every 10 minutes it finds messages stuck
  15+ minutes, photos that produced no transaction, spending left "Uncategorized", and holds caused only by an
  AI outage, and repairs them **through the guarded pipeline**: stuck jobs are re-enqueued (3 tries), a failed
  photo is read again quietly and then, as a weaker fallback, only its printed total is recorded (confidence
  capped at 0.8, the model must quote the printed total and it must agree, a typed caption must still match,
  and the transaction notes say its lines were unreadable), a missing category comes from merchant memory or a
  confident model answer (<= 1 try), an AI-outage hold is released through the normal job (<= 3 tries; holds for
  a real risk are never touched). One message reports what it did and what it could not fix, once. There is no
  delete anywhere in the package (a test scans for it); every action is one `repair` audit row (migration 0006).
  It never decides a photo that contradicts its own caption.

Deviations from the blueprint you should know about: Telegram is no longer silent for *your own* taps and
messages (blueprint 4.2c: only the Arbiter summary and emergency alerts); AI-requested edits and batch drafts
ask for a confirmation tap (blueprint: "Zero Manual Confirmation"); the steward's total-only fallback relaxes
the "100% accurate total" gate for photos that would otherwise record nothing; vision measured 4-6 s per
receipt against the blueprint's 3 s gate.

### Conversational bot: menu, buttons, chat agents and confirmed write tools (2026-10-24)

The bot was deliberately silent (blueprint 4.2c: only the Arbiter summary and emergency alerts). The owner
asked for an interactive bot, so it now also **answers the owner's own taps and messages**. It still never
volunteers anything, and debate content is still never sent.

- **Menu and buttons.** `/start` `/menu` and a Telegram command menu; inline buttons for today, recent
  (tap to undo), held (release / dismiss), budget, unlock, help. Summaries and alerts get ↩️ Undo / ✅ Lepas /
  🗑️ Buang buttons **derived from the command links already in their text** (`buttons_for`), so the commit
  pipeline and its tests are untouched. Callback data is `m:` (menu), `c:` (token commands), `t:` (by id),
  `a:` (action confirmation), all under Telegram's 64-byte limit.
- **Chat.** Text the extractor calls "not a transaction" no longer gets «Belum bisa mencatat»; it goes to a
  `chat_event` job. A router picks a role from the existing agent registry: A-02 Finance and A-07 Planner
  answer from a **code-computed ledger snapshot** (month by category vs budget, last 7 days, last 10
  transactions with ids, wallets, held count) and may use only those numbers; `action` plans a write;
  `general` is small talk; anything else (health, research, tech, news, mental health) is refused honestly
  because nothing real stands behind it yet. `/private` content never enters this path or Redis. Short
  history (8 turns, 30 min) lives in Redis. A message containing an injection phrase reaches no model.
- **Write tools, MCP-style but in-process:** `add_transaction`, `undo_transaction`, `edit_transaction`
  (merchant, category, notes), `set_budget`. There is no raw SQL and no hard delete (the app role has no
  DELETE; "delete" is the existing soft undo).
- **Security model.** The model only *proposes*: tool name + arguments, validated by pydantic (`extra=forbid`,
  ranges, patterns, the transaction must exist and be `committed`). Nothing is shown unless valid. The owner
  sees a **card whose text is built from the database**, never from model prose, with ✅/❌ (or
  `/confirm_<nonce>`). The proposal sits in Redis under a random 48-bit nonce for 10 minutes and is taken
  with an atomic `GETDEL`, so it runs at most once; the actor must be the owner; arguments are re-validated at
  confirmation (a planted or tampered Redis entry is refused); at most 6 proposals a minute. A new
  transaction is not inserted by the tool: it enters as a `parsed` event and goes through the **same breakers,
  debate, quorum and summary**, so asking the AI cannot bypass the single-transaction ceiling. Edits reuse
  `apply_effects` (reverse, change, re-apply) and merchant learning. Every confirmed edit and budget change
  writes an `edit` / `budget_set` row in the hash-chained audit log (migration 0002 widens that CHECK).
- **Tests.** `tests/integration/test_chat.py` (50) and `test_menu.py`; ten deliberate breakages (GETDEL→GET, owner
  check removed, extra fields allowed, no re-validation, no status check, no rate limit, no amount ceiling,
  no audit row, pipeline bypass, no TTL) were each caught by a failing test.
- **Not done.** Prompt-injection through merchant names can still make the *model* propose something odd, but
  it can only become a card you read and confirm. Budget edits ignore the elastic band (they set base and
  current together). No bulk operations, no editing the amount (undo and re-add instead).

### Provider decision (2026-10-24): **kunciai primary, Osiris fallback**

You said: "if osiris is very bad use kunci AI as main". On the evidence below it is, so `infra/litellm/config.yaml`
now leads with kunciai and falls back to Osiris. Both providers rate-limit **per key**, so the first hop of every
chain is on the other provider (a fallback on the throttled key is refused too); the vision chain falls back on
Osiris only, because kunciai has one image-reading model. `tests/test_litellm_config.py` pins this per provider.

| Measured, same load | kunciai | Osiris |
|---|---|---|
| Debate votes | 60 of 60, ~4 s per debate, 0 failures | 29 of 30 at 6-9 s, throttled to 24 rpm; one 33 s outlier and one malformed reply |
| `deepseek-v4-flash` | works | 502 (as is `qwen3.8-max`); 8 of its 18 allowed models are "not found" |
| Rate limit | ~60 rpm (inferred: 13 of 30 refused in a third burst) | 30 rpm per key (stated) |
| Vision, single receipt / 3-photo album | 4.4 s / 9.9 s, correct | 6-7 s / 12 s, correct |
| Image tokens for the same receipt | 2,961 | 976 (**kunciai bills ~3x**) |

- **Timeouts** (router `timeout: 22`, `num_retries: 0`, vision/kimi-k3 `timeout: 60`, client 60 s / 150 s). Against
  a deliberately stalling upstream: old settings (45 s, one retry) took **93.3 s** to answer and hit the stalled
  model twice; new settings **22.9 s**, once. `allowed_fails` alone does not cool a model down after a timeout;
  `allowed_fails_policy: {TimeoutErrorAllowedFails: 1}` does (the third call was instant).
- **Client rpm limiter** (`llm_max_rpm`, now 48; was 24 for Osiris). Cause: a 5-request debate burst exceeded
  Osiris's 30 rpm, LiteLLM cooled every same-key model down for 60 s and the 429s cascaded. Lower it to ~24 if
  Osiris ever becomes primary again.
- **Verified live through the proxy on the new config:** all 6 live tests pass (extraction 1.7-6.8 s, debate
  commit 3.9 s with 5/5 votes, receipt 10 s, 3-photo album `dual_verified` 14.7 s), and a 90-call unthrottled
  burst had **90 of 90 succeed**, 48 of them absorbed by the cross-provider fallback after kunciai refused.
- **Caveats:** kunciai is a reseller (transaction text and receipt photos go to a third party with unknown
  retention and upstreams; its price is unknown to me, and the image tokens are 3x); samples are small and from
  one afternoon; kunciai's `glm-5.3` is text-only; `router-mental-health` (glm-5.2) is still a provisional pick.

### kunciai.id (OpenAI-compatible reseller): probed 2026-10-24 (now the primary, see above)

`https://kunciai.id/v1`, key in `.env` as `KUNCI_API_KEY` (a `sk-live-` key: real money). It lists 88 models,
a third of them image/video generators. Model ids carry a `kunciai/` prefix (`kunciai/deepseek-v4-flash`).
Probed with 5-token pings, a JSON-mode request and the drawn receipt (26 calls, ~29k tokens, almost all of
it the three image requests; a text ping costs 5 tokens, so there is no hidden prompt overhead).

| Model | Result |
|---|---|
| deepseek-v4-flash, deepseek-v4.1-flash, deepseek-v4-pro | ping 1.5-2.6 s, JSON valid. **v4-flash is the model Osiris keeps 502ing on** |
| glm-5.3, glm-5.2 | ping 1.8-1.9 s, JSON valid |
| qwen3.8-max, qwen3.8-flash | ping 1.4 s, JSON valid (qwen3.8-max is 502 on Osiris) |
| glm-5v-turbo | ping 1.2 s, JSON valid but fenced; **read the receipt correctly** (58.300, 2.8 s) |
| kimi-k2.5 | ping 1.8 s, JSON valid but fenced |
| claude-haiku-5.5 | works but slow and erratic: ping 27 s, JSON 6.4 s |
| glm-5.3-flash, kimi-k2.7, kimi-k3 | 502 (an HTML gateway error page) at probe time |
| **glm-5.3 with an image** | **answered "I can't see the receipt": text-only here**, unlike on Osiris |
| deepseek-v4-flash-vision-exp | 503 "model sedang riset" |

- **Do not reuse Osiris's vision chain on it.** `glm-5.3` and `glm-5.2` read images on Osiris and glm-5.3 does
  not on kunciai: capability is per provider, so every model needs its own image test (only glm-5v-turbo
  passed here).
- **It would fix real gaps:** a working `deepseek-v4-flash` (the cheap routine primary) and `qwen3.8-max`, and
  a second provider with its own rate limit. Wired as `model_list` entries with
  `api_base: os.environ/KUNCI_API_BASE`, `api_key: os.environ/KUNCI_API_KEY` and `model: openai/kunciai/<id>`.
- **Same caveat as any reseller:** transaction text and receipt photos would go to a third party whose
  retention and upstreams are unknown to me. Treat it as an allow-listed fallback after you have decided
  you are comfortable with that, not as a silent default.
- One 3-call sample per model on one afternoon: latency and 502s are not a reliability measurement.

### 9router (LAN OpenAI-compatible router): evaluated 2026-10-23, **not usable yet**

`http://192.168.1.20:20128/v1` is reachable and accepts the key (`ROUTER9_API_BASE` /
`ROUTER9_API_KEY` in `.env`; a keyless template is in `.env.example`). It catalogues 824 model ids from
83 provider prefixes, but **no provider answers**: one representative model per prefix was pinged and
81 returned `404 No active credentials for provider: <name>`, `mmf` returned 401 invalid token, and `oc`
returned unsupported / deprecated / unavailable. The listing cannot tell live from dead (`?active=true`
is ignored, `/health` and `/providers` are not API routes), so a catalogue entry means nothing.
Credentials have to be added on the router itself; re-probe after that.

How to wire it in once something works (not done: there is nothing to wire):
- LiteLLM deployments can each carry their own `api_base`, so a router model is one more
  `model_list` entry with `api_base: os.environ/ROUTER9_API_BASE`, `api_key: os.environ/ROUTER9_API_KEY`
  and `model: openai/<provider>/<id>` (e.g. `openai/deepseek/deepseek-v4-flash`), then added to a
  `fallbacks` chain. `tests/test_litellm_config.py` currently asserts every deployment uses the Osiris
  env vars; that assertion must be relaxed deliberately, not silently.
- It is a second provider and a second rate limit, so it genuinely adds failover for outages and for
  Osiris's burst throttling, if its upstreams are healthy.

**Decide this before routing real data through it.** The transaction text, merchant names, amounts and
(later) meal photos and biometrics would travel to whichever upstream the router picks, and the
blueprint's premise is that sensitive data stays on licensed models. A router that fans out to 80
providers, some of which are consumer-account style (`grok-web`, `perplexity-web`, `cc`, `kr`) or free
tiers with unknown retention, breaks that premise unless the config is restricted to a short allow-list
of providers you have vetted. Treat it as an allow-listed fallback, never as "any model".

### D2 addendum: LLM extraction fallback (`needs_llm` events)

Implemented in `ingestion/llm_extract.py` and `pipeline/extract.py`. When both regex parsers give
up, an `extract_event` job asks `deepseek-v4-flash` (route `router-routine-classifier`, JSON mode,
temperature 0) for one transaction and hands the result to the *same* `commit_event` pipeline, so
breakers, debate, Arbiter and audit apply unchanged. The model's output is never trusted alone:

- **Strict schema:** anything malformed, out of range (amount outside 0 to Rp 10 miliar, unknown
  wallet or channel) or missing direction/amount becomes `needs_review`, never a guess.
- **Amount must appear in the original text.** The extracted amount (and fee) has to match a number
  actually written in the message ("18k", "35.000", "Rp 1,5jt"), so a hallucinated figure is
  rejected by arithmetic. Text with no digits at all ("dua puluh ribu") goes to review: the model
  does not convert number words.
- **Confidence:** below 0.85 (the blueprint's NLP threshold) goes to review; accepted confidence is
  capped at 0.95, and 0.90 when the wallet was assumed to be cash. 1.0 stays reserved for regex.
- **Bank notifications take the wallet from the sending app's package,** not from the model, and a
  notification from a package that is not a known bank/e-wallet is `skipped` (`unknown_package`)
  rather than sent to the model.
- **Known limit:** if an attacker writes a figure into the text *and* an obedient model echoes it,
  the amount is "in the text" and extraction passes. Intake strips injection phrases first, and the
  circuit breakers hold the result (single-transaction ceiling, liquidity floor): tested end to end.
- **Outcomes:** `parsed` (then `commit_event`), `skipped`, `needs_review` (dead end by design:
  nothing is committed on a guess; missed money surfaces later through statement reconciliation).
  An LLM outage retries 3 times, 15 s apart, then parks the event as `needs_review`.
- **User feedback:** only for text the user *typed* in Telegram, one short reply saying why it could
  not be recorded (stored as `telegram_messages.kind = 'reply'`). An unreadable bank notification
  fails silently: it is not worth a ping.
- **Still open:** `needs_review` events have no UI and no re-queue. The Web UI should list them, and
  a sweep could re-enqueue the ones parked for `llm_unavailable`.

## D3. Reversibility: **soft reversal** (implemented in Phase 4)

Implementation notes and the places it differs from the list below
(`backend/src/apa/pipeline/{effects,undo}.py`, `bot/commands.py`):

- **Commit and undo share one effects function** (`apply_effects`, sign +1 / -1), so a reversal
  is exactly symmetric with the original. Row locks are taken in one order everywhere
  (transaction, wallet, budget, merchant, then the audit-chain lock last).
- **Telegram commands:** `/undo_<token>` or `/undo <token>`; the 6-hex token is on the summary line.
  Numeric ids are not accepted there (a token like `123456` would be ambiguous); the Web UI will undo
  by id through `undo_transaction(..., via='web_ui')`. A double tap is safe: the transaction row is
  locked, so exactly one tap reverses it.
- **Quarantined transactions** get their own token on the alert: `/release_<token>` (owner override:
  runs the debate, if it is an expense, and commits the *same row*, skipping the breakers, with a
  fresh undo window) and `/dismiss_<token>` (marks it `rolled_back`; nothing was ever applied, so
  nothing is reversed). `/unlock` clears the emergency lockdown. Release does not clear lockdown.
  New audit decision types: `release`, `dismiss`.
- **ATM withdrawals store their bank fee as `net_user_expense`**, so the cash credited is exactly
  `total - net` and undo reverses it precisely. Income paid into the cash wallet now credits it
  (Phase 3 missed this).
- **Budget clamp:** undo never pushes `budget_rules.spent_amount` below zero, because a budget
  created after the spend never counted it.
- **Known gaps:**
  1. D3 step 5 (subtract sugar/caffeine/sodium from `biomarker_metrics`) is not implemented
     because nothing adds to them yet. Do it when the health engine (Phase 2c) starts writing.
  2. `remove_visit` cannot restore `merchant_cache.last_seen_at`, so `visits_per_week` can read
     slightly high after an undo. Count, average and stddev are restored exactly.
  3. No Web UI endpoints exist. They need an authentication design first; unauthenticated
     endpoints that move money state were deliberately not added.

Original design (for reference):

`/undo <tx_id>` never deletes rows. Within `reversibility_deadline` (timestamp + 24 h), in one DB
transaction it:

1. sets `transactions.status = 'rolled_back'`, `rolled_back_at`, `rolled_back_via`;
2. restores `cash_wallets.current_balance` (cash and ATM-withdrawal entries);
3. decrements `budget_rules.spent_amount` for the affected category/month;
4. marks `transaction_splits` for that transaction as `waived`;
5. subtracts the purchase's sugar, caffeine and sodium from `biomarker_metrics` for that day;
6. appends an `explainability_log` row with `decision_type = 'undo'`.

The application DB role has **no DELETE** on ledger tables. `ON DELETE CASCADE` stays on the
FKs, but only for an admin-only hard purge (e.g. test data), never for normal operation.

### Changes to `transactions`

```sql
ALTER TABLE transactions
  ADD COLUMN status            VARCHAR(16) NOT NULL DEFAULT 'committed'
      CHECK (status IN ('committed','quarantined','rolled_back')),
  ADD COLUMN debate_id         UUID,                 -- -> explainability_log.debate_id
  ADD COLUMN rolled_back_at    TIMESTAMPTZ,
  ADD COLUMN rolled_back_via   VARCHAR(16) CHECK (rolled_back_via IN ('telegram','web_ui')),
  ADD COLUMN inbound_event_id  BIGINT;               -- -> inbound_events.id

-- execution_mode values after D2:
--   'consensus_auto' | 'circuit_override' | 'reconciliation_catchup'
-- source_channel gains 'reconciliation' (A-11 synthetic catch-up entries)
```

---

## 4. Missing table definitions (proposed)

These 9 tables are referenced in the blueprint but never defined. Conventions follow §5B:
money is `NUMERIC(14,2)` IDR, timestamps are `TIMESTAMPTZ`, agent ids are `'A-02'` style.

### 4.1 `explainability_log` — append-only, hash-chained

One row per autonomous decision, so it also serves as the debate header.

```sql
CREATE TABLE explainability_log (
  id                BIGSERIAL PRIMARY KEY,
  debate_id         UUID UNIQUE,                       -- NULL for non-debate decisions (undo, config)
  transaction_id    BIGINT REFERENCES transactions(id),
  decision_type     VARCHAR(32) NOT NULL CHECK (decision_type IN (
                      'ledger_commit','quarantine','undo','budget_rebalance',
                      'health_interlock','reconciliation','config_change')),
  agents_invoked    VARCHAR(5)[] NOT NULL DEFAULT '{}',
  agent_scores      JSONB NOT NULL DEFAULT '{}',       -- {"A-02":{"score":0.4,"weight":0.3,"stance":"object"},...}
  consensus_formula VARCHAR(32),                       -- versioned id, e.g. 'weighted_v1'
  final_score       NUMERIC(4,3),
  verdict           VARCHAR(32) NOT NULL,              -- e.g. 'commit:Food & Dining', 'quarantine:velocity'
  justification     TEXT NOT NULL,                     -- Arbiter's rationale (neutral label only for /private)
  policy_version    INT,                               -- policy_rules snapshot used
  total_tokens      INT,
  cost_idr          NUMERIC(10,4),
  created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
  prev_hash         CHAR(64),
  row_hash          CHAR(64) NOT NULL                  -- sha256(prev_hash || canonical row) — tamper evidence
);
-- Enforced append-only: REVOKE UPDATE, DELETE from the app role, plus a BEFORE UPDATE OR DELETE
-- trigger that raises. A weekly job re-verifies the hash chain.
```

### 4.2 `agent_debate_transcripts` — one row per agent turn

Feeds the Web UI Live Debates tab (Debate Replay and Scoring Matrix).

```sql
CREATE TABLE agent_debate_transcripts (
  id                 BIGSERIAL PRIMARY KEY,
  debate_id          UUID NOT NULL,                    -- -> explainability_log.debate_id
  subject_type       VARCHAR(16) NOT NULL CHECK (subject_type IN ('transaction','meal','health_event')),
  subject_ref        BIGINT,                           -- transactions.id / meal_logs.id / ...
  round_no           SMALLINT NOT NULL DEFAULT 1,
  agent_id           VARCHAR(5) NOT NULL,
  model              VARCHAR(48) NOT NULL,             -- model actually used (after LiteLLM fallback)
  stance             VARCHAR(8) CHECK (stance IN ('approve','object','abstain')),
  score              NUMERIC(4,3),
  confidence         NUMERIC(4,3),
  argument           TEXT NOT NULL,
  structured_output  JSONB,                            -- agent's validated JSON response
  prompt_tokens      INT,
  completion_tokens  INT,
  cached_tokens      INT,
  latency_ms         INT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON agent_debate_transcripts (debate_id, round_no);
-- A-06 /private sessions never write here.
```

### 4.3 `policy_rules` — Policy Matrix (Web UI › Settings)

```sql
CREATE TABLE policy_rules (
  id            SERIAL PRIMARY KEY,
  rule_key      VARCHAR(48) NOT NULL,   -- 'single_tx_ceiling','velocity_max_tx','velocity_window_min',
                                        -- 'liquidity_floor','category_autonomy_cap','deviation_sigma',
                                        -- 'caffeine_daily_mg','sugar_daily_g','sodium_daily_mg',
                                        -- 'early_warning_pct','fatigue_sleep_score','stress_shield_score'
  scope         VARCHAR(12) NOT NULL DEFAULT 'global' CHECK (scope IN ('global','category','wallet')),
  scope_ref     VARCHAR(48) NOT NULL DEFAULT '',      -- category or wallet name; '' for global
  value_numeric NUMERIC(14,2),
  value_json    JSONB,
  unit          VARCHAR(16),                          -- 'IDR','count','minutes','mg','g','pct','sigma'
  is_active     BOOLEAN NOT NULL DEFAULT true,
  version       INT NOT NULL DEFAULT 1,
  updated_by    VARCHAR(32) NOT NULL,                 -- 'web_ui' | 'seed' | 'agent:A-09'
  updated_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (rule_key, scope, scope_ref)
);
-- Every change bumps version and appends explainability_log(decision_type='config_change').
-- Seed values from the blueprint: velocity 5 tx / 10 min, single tx 2,500,000, sugar 30 g,
-- caffeine 400 mg, sodium 2,000 mg, early warning 80%, TruSleep < 70 / deep < 15%, stress > 75 for 2 h.
```

### 4.4 `merchant_cache` — deterministic merchant knowledge (debate context only)

```sql
CREATE TABLE merchant_cache (
  id               SERIAL PRIMARY KEY,
  merchant_key     VARCHAR(96) NOT NULL UNIQUE,       -- normalized, e.g. 'indomaret'
  display_name     VARCHAR(128) NOT NULL,
  match_patterns   TEXT[] NOT NULL DEFAULT '{}',      -- aliases / regexes seen in notifications
  default_category VARCHAR(48),
  default_agent    VARCHAR(5),
  visit_count      INT NOT NULL DEFAULT 0,
  visits_per_week  NUMERIC(5,2),
  avg_amount       NUMERIC(14,2),
  stddev_amount    NUMERIC(14,2),                     -- feeds the >3σ deviation check
  health_profile   JSONB,                             -- typical sugar/caffeine for this merchant
  source           VARCHAR(8) NOT NULL DEFAULT 'learned' CHECK (source IN ('learned','manual')),
  last_seen_at     TIMESTAMPTZ,
  updated_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 4.5 `budget_rules` — monthly category budgets with elastic bands

```sql
CREATE TABLE budget_rules (
  id                SERIAL PRIMARY KEY,
  category          VARCHAR(48) NOT NULL,
  period_month      DATE NOT NULL,                    -- first day of month
  base_limit        NUMERIC(14,2) NOT NULL,
  current_limit     NUMERIC(14,2) NOT NULL,           -- after rebalancing
  spent_amount      NUMERIC(14,2) NOT NULL DEFAULT 0, -- maintained in the commit/undo transaction
  is_flexible       BOOLEAN NOT NULL DEFAULT false,   -- can be a rebalance source
  is_essential      BOOLEAN NOT NULL DEFAULT false,   -- can be a rebalance target (Groceries)
  elastic_band_pct  NUMERIC(4,2) NOT NULL DEFAULT 15, -- ±% around base_limit
  is_sinking_fund   BOOLEAN NOT NULL DEFAULT false,   -- e.g. 'Preventive Health', 'Mental Care Buffer'
  UNIQUE (category, period_month),
  CHECK (current_limit BETWEEN base_limit * (1 - elastic_band_pct/100)
                           AND base_limit * (1 + elastic_band_pct/100) OR is_sinking_fund)
);
```

### 4.6 `budget_rebalance_log`

```sql
CREATE TABLE budget_rebalance_log (
  id                 BIGSERIAL PRIMARY KEY,
  trigger_type       VARCHAR(24) NOT NULL CHECK (trigger_type IN (
                       'pihps_spike','health_tax','stress_shield','fatigue_defense','web_ui')),
  from_category      VARCHAR(48) NOT NULL,
  to_category        VARCHAR(48) NOT NULL,
  period_month       DATE NOT NULL,
  amount             NUMERIC(14,2) NOT NULL CHECK (amount > 0),
  from_limit_before  NUMERIC(14,2) NOT NULL,
  from_limit_after   NUMERIC(14,2) NOT NULL,
  to_limit_before    NUMERIC(14,2) NOT NULL,
  to_limit_after     NUMERIC(14,2) NOT NULL,
  trigger_ref        JSONB,                            -- e.g. {"commodity":"cabai_merah","rolling7d_pct":12.4}
  explainability_id  BIGINT NOT NULL REFERENCES explainability_log(id),
  created_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);
```

### 4.7 `price_basket` — personal inflation basket (plus its time series)

```sql
CREATE TABLE price_basket (
  id              SERIAL PRIMARY KEY,
  commodity_code  VARCHAR(32) NOT NULL UNIQUE,        -- PIHPS commodity id
  commodity_name  VARCHAR(64) NOT NULL,               -- 'Beras Medium', 'Telur Ayam Ras', ...
  monthly_qty     NUMERIC(8,2) NOT NULL,
  unit            VARCHAR(8) NOT NULL,                -- 'kg','liter','butir'
  pihps_region    VARCHAR(48) NOT NULL,               -- province/city used for prices
  baseline_price  NUMERIC(14,2) NOT NULL,             -- price when the budget was set
  budget_category VARCHAR(48) NOT NULL DEFAULT 'Groceries',
  is_active       BOOLEAN NOT NULL DEFAULT true
);

-- Also needed (implied by "7-day rolling average", not named in the blueprint):
CREATE TABLE commodity_prices (
  commodity_code  VARCHAR(32) NOT NULL REFERENCES price_basket(commodity_code),
  price_date      DATE NOT NULL,
  pihps_region    VARCHAR(48) NOT NULL,
  price           NUMERIC(14,2) NOT NULL,
  PRIMARY KEY (commodity_code, pihps_region, price_date)
);
```

### 4.8 `macro_assumptions` — versioned macro inputs (A-09)

```sql
CREATE TABLE macro_assumptions (
  id                         SERIAL PRIMARY KEY,
  assumption_key             VARCHAR(48) NOT NULL,   -- 'bbm_pertalite_per_l','tdl_per_kwh','bi_rate',
                                                     -- 'usd_idr','ppn_rate','cpi_yoy'
  value                      NUMERIC(14,4) NOT NULL,
  unit                       VARCHAR(16) NOT NULL,
  effective_from             DATE NOT NULL,
  source                     VARCHAR(64) NOT NULL,   -- 'bps' | 'bi' | 'news:<article_id>'
  projected_monthly_impact   NUMERIC(14,2),          -- IDR change to the user's cost of living
  explainability_id          BIGINT REFERENCES explainability_log(id),
  created_at                 TIMESTAMPTZ NOT NULL DEFAULT now(),
  UNIQUE (assumption_key, effective_from)
);
-- Current value = latest effective_from <= today. Rows are appended, never updated.
```

### 4.9 `reconciliation_log` — A-11 line-level results

```sql
CREATE TABLE reconciliation_log (
  id                       BIGSERIAL PRIMARY KEY,
  run_id                   UUID NOT NULL,               -- one weekly run
  wallet                   VARCHAR(32) NOT NULL,        -- 'bank_bca', ...
  statement_storage_uri    TEXT NOT NULL,               -- e-statement PDF in MinIO
  statement_sha256         CHAR(64) NOT NULL,
  statement_line           JSONB NOT NULL,              -- raw OCR row (date, description, debit/credit)
  statement_date           DATE NOT NULL,
  statement_amount         NUMERIC(14,2) NOT NULL,
  match_tier               SMALLINT CHECK (match_tier IN (1,2,3)),
  match_type               VARCHAR(20) NOT NULL CHECK (match_type IN (
                             'exact','fuzzy_fee','split','synthetic_catchup','unmatched')),
  matched_transaction_id   BIGINT REFERENCES transactions(id),
  fee_amount               NUMERIC(14,2),               -- tier 2: BI-FAST 2,500, top-up 1,000, ...
  created_transaction_id   BIGINT REFERENCES transactions(id), -- fee entry or tier 3 catch-up
  date_diff_days           SMALLINT,                    -- must be within ±2 for tier 1
  created_at               TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX ON reconciliation_log (run_id);
```

---

## 5. Other implied tables (to define during scaffolding)

The blueprint's behaviour needs these, but it never names them:

| Table | Why |
|---|---|
| `inbound_events` | raw sanitized event store; target of the Phase 1 "< 200 ms" gate and an idempotency key for the offline buffer replay |
| `telegram_messages` | maps outbound summaries to `transaction_id` so `/undo_8f9a2` short tokens resolve |
| `oauth_tokens` | Huawei Health Kit refresh tokens (encrypted at rest) |
| `news_articles` | trafilatura output plus title/URL hash for de-duplication |
| `cyber_advisories` | NVD/KEV/EPSS rows with tier (`urgent`/`normal`/`archive`) |
| `homelab_watchlist` | software the Urgent tier matches against (Proxmox, Docker, pfSense, …) |
| `research_reports` | archived `/riset` output with citations |
| `savings_goals` | A-07 goals and deadlines; the monthly digest evaluates them |

## 6. Remaining gaps — proposed defaults (not yet confirmed)

- **Meal-photo model:** use `glm-5v-turbo` (fallback `glm-5.3`) per §4.8. `glm-5.3-flash` and `deepseek-v4.1-flash` are not on the 18-model whitelist.
- **LiteLLM fallbacks:** give each fallback model its own `model_list` entry (e.g. `fallback-qwen3.7-plus`) and reference it by `model_name`, not by raw model string.
- **Schema source of truth:** Pydantic models in `backend/src/apa/domain/`; Web UI TypeScript types generated from the FastAPI OpenAPI spec.
- **Morning briefing trigger:** don't rely on `ALARM_DISMISSED`. Use a scheduled `AlarmManager` alarm at the user's wake time, with the Bluetooth `ACL_CONNECTED` trigger as the second path.
