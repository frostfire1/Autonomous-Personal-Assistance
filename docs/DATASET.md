# The dataset about you ("About me")

Everything the assistant knows about you lives in **one card**, grouped by domain: `identity`, `comfort` (how you like to be talked
to), `personality`, `hobbies`, `tastes`, `routines`, `wellbeing`, `money`, `people`, `goals`, `plans`, `notes`, `other`.

| Entry type | Where it comes from | Who can change it |
|---|---|---|
| **Fact** (domain, key, value) | you on the dashboard, or the assistant when you tell it something lasting (`about_set`) | facts you write or edit are **locked**: the assistant can read them but never changes or removes them |
| **Note** and **learned item** | "remember that...", and what it noticed in chats | you (edit, delete); the assistant can add |
| **Trait** | computed from your data (sleep, food, app time, mood, writing style...) and the AI's summaries (media taste, day character, personality) | read-only; correct one by writing a fact with the same domain and key, which then wins |

Every entry shows its **source**, the **evidence** behind it and when it was last updated. The assistant reads the whole card as one
block before it answers, so what you correct on the dashboard changes how it talks to you from the next message.

**Comfort.** Write under `comfort`: `language` ("Indonesian, casual"), `tone`, `length`, `emoji`, and under `identity`: `name`,
`nickname`. These beat what was measured from how you write. Telegram's profile name is kept as `identity.name` until you set one.

**The dashboard.** *About me* lists everything; edit an entry in place, lock or unlock a fact, delete anything that is wrong, or add
a fact. Edits and deletes apply at once.

## API

| | |
|---|---|
| `GET /about` | all entries: `ref`, `domain`, `key`, `value`, `why` (evidence), `source`, `confidence`, `locked`, `updated`, `editable` |
| `POST /about/fact` | `{domain, key, value, locked}`: write a fact as the owner |
| `PUT /about/item` | `{ref, value, locked?}`: correct a fact, note or learned item |
| `DELETE /about/item?ref=...` | remove one |
| `GET /about/export` | JSON lines, one entry per line, the same fields as above (`?format=card` gives the text block the assistant reads) |

## Using it with a real model later

The export is one flat, stable shape, so it can feed either of these without changing the app:

- **Retrieval (RAG):** embed each line as `"{domain} / {key}: {value}"` and retrieve the closest ones for a question. `source`,
  `confidence` and `locked` let you trust owner-locked facts over guesses.
- **Fine-tuning or a persona prompt:** use `GET /about/export?format=card` as the system prompt, and build examples from the diary and
  day snapshots (`GET /memory/export`, `GET /days`). Raw chat is erased after `CHAT_KEEP_DAYS`, so only what was kept is available.

The export is **decrypted**: it is your private data in plain text. Keep the file private, and do not send it to a provider you do not
trust. Private journal entries are never part of it.
