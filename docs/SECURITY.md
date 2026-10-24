# Security and privacy

What is protected, how, and what is not. The diary and the assistant's memory are the most personal data in the system, so they
get the strictest handling.

## Diary and memory

| | |
|---|---|
| **Encrypted at rest** | Every note, learned item, diary summary and journal entry is stored as Fernet ciphertext (AES-128-CBC with an HMAC), made with `SECRETS_KEY`. A database dump, a backup or a stolen disk shows nothing readable. |
| **The key is not in the database** | `SECRETS_KEY` lives in `.env`. **Back it up.** If it is lost the diary cannot be read by anyone, and a changed key shows `[cannot be read]`. |
| **No plaintext fallback** | With no valid key nothing is written (the app says so); it never quietly stores the text unencrypted. |
| **Private journal** | Your own diary entries (dashboard **Diary** page, or `/diary <text>` in Telegram). **Private by default: the AI never sees them.** You can mark one entry "Let the assistant read this" and take it back at any time; even then long numbers (cards, accounts, phones) are hidden from it. |
| **`/diary` in Telegram** | Saved straight to the encrypted store: it is not put in the event store, the queue, the chat history or any AI prompt, and the bot deletes your message from the chat. The reply never repeats the text. A failure never reaches the logs with the text in it. |
| **Not logged, not indexed** | Journal text is never logged (only its length). There is no text index on it (that would index the ciphertext); search decrypts in the app. |
| **Deleting is deleting** | Deleting an entry removes its row. |
| **/private chats** | Never stored anywhere (RAM only), so they can never reach the diary or the memory. |

## What the AI provider sees

The assistant (a model run by the provider you chose) sees: your recent chat messages, your latest transactions and budgets, the
notes you asked it to keep, what it learned from your chats, diary summaries of earlier days, the profile computed from your data,
**only the journal entries you marked as shared** (those are also read by the nightly diary summary, to write the day's summary and observations; private entries never are), and, if you use the YouTube features, **channel names and video titles** of what you watch, like and follow (the nightly `ai.content` summary). That text is sent to the provider to produce a reply. If that is not
acceptable for something, keep it in the private journal (never sent) or do not type it in the chat.

## What is NOT protected, honestly

- **Normal chat messages** you type in Telegram are stored in plain text in the database (the assistant reads them as context) and
  they are on Telegram's servers. Do not write private diary entries in the chat: use `/diary` or the Diary page. The assistant tells
  you the same if you start a confidence in the chat.
- **Raw chat is short-lived.** Chat text older than `CHAT_KEEP_DAYS` (default 7) is erased every night once the diary has captured
  the day; what remains is the encrypted diary, learned items and observations (see ACTIVITYWATCH.md).
- **App usage** (ActivityWatch) stores app names and seconds only, in plain columns: no window titles, no URLs.
- **Telegram** keeps a message you type until it is deleted. `/diary` deletes it straight away, but Telegram's servers saw it.
- **The connection to the dashboard** is plain HTTP on your network. Use your home network or a VPN (Tailscale, WireGuard); never the
  open internet. Anyone who has your API key (`INGEST_API_KEY`) can read the diary through the dashboard: keep it secret, rotate it
  if a phone is lost, and sign out on shared computers.
- **Someone with the server** (root) can read `SECRETS_KEY` from `.env` and so decrypt the diary. Encryption at rest protects the
  database and the backups, not a compromised server.
- **Shared entries and chat-derived memory** that the assistant already used live on in the provider's logs according to the
  provider's own policy.

## Other protections

- Bank notifications are scrubbed of card, account, phone, ID and e-mail numbers on the phone before they leave it.
- The AI cannot change the ledger by itself: every change is a confirmation card you tap.
- Requests to the API need the key; signed requests (`INGEST_HMAC_SECRET`) and a rate limit are available.
- The AI gateway never writes prompts to its logs (`turn_off_message_logging`), and has no database.
