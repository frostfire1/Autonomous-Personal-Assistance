# App usage with ActivityWatch

[ActivityWatch](https://activitywatch.net) records which app you use and for how long, on Android and on desktop, and keeps it on
your own devices. The assistant reads its totals so it knows your screen time, your busiest apps and when you are up late, and
uses that in its advice (profile trait `screen_time`, and your "when active" estimate).

**What leaves your devices:** app names and seconds per day. Never window titles, URLs or document names.

## Android

1. Install ActivityWatch for Android and grant it the usage-access permission. Leave it installed.
2. Newer ActivityWatch versions answer only with an API key: copy it from the ActivityWatch app's settings into this app's
   settings (**ActivityWatch API key**) and save. This app reads ActivityWatch on the phone (`127.0.0.1:5600`) and sends today and yesterday with every sync, at
   most every 30 minutes. If ActivityWatch is not installed or not running, it does nothing.

## Desktop (Windows, macOS, Linux)

Run ActivityWatch with its window and AFK watchers (the AFK watcher keeps idle time out), then:

```bash
python tools/aw_push.py --server http://YOUR-SERVER:8000 --key YOUR_INGEST_API_KEY
```

Schedule it every 30 minutes (Task Scheduler, cron or a systemd timer). Add `--aw-key KEY` if your ActivityWatch server has an `api_key`, `--hmac SECRET` if the server uses
`INGEST_HMAC_SECRET`, `--device NAME` to label the machine. Any other tool can push the same shape to `POST /ingest/usage`:
`{"device": "...", "kind": "android|desktop|ios|web|other", "day": "YYYY-MM-DD", "apps": [{"app": "...", "seconds": 600}]}`;
a push replaces that device's day.

Read it back: `GET /usage` (the dashboard API) shows per-day totals per device and the most used apps.

### ActivityWatch's own settings (optional)

`aw_push.py` talks to the ActivityWatch server on the same computer (`localhost:5600`), so you do not have to change anything and
nothing is exposed on your network. Only if you want to run the script on another machine, point `--aw` at that server and set
its bind address in its config file ([configuration docs](https://docs.activitywatch.net/en/latest/configuration.html)):
`host` under `[server]` in `aw-server.toml` (Python server), or `address` in `config.toml` (Rust server), default localhost.
That exposes the server to your network: keep it on a VPN (Tailscale), never the open internet.

## Desktop: keep it running

Install ActivityWatch on the computer (it starts the window and AFK watchers by itself), then run `aw_push.py` every 30 minutes. On
Windows, once:

```
schtasks /Create /SC MINUTE /MO 30 /TN APA-usage /TR "python C:\path\to\tools\aw_push.py --server http://YOUR-SERVER:8000 --key YOUR_KEY"
```

## YouTube: what you watch

YouTube's API does not share watch history, so you export it yourself: Google Takeout > YouTube and YouTube Music > History > JSON, then
choose `watch-history.json` on the dashboard's **Diary** page (or `POST /import/youtube`). Channel and title of the last 90 days are
kept, encrypted; the profile (`youtube`) names the channels and topics you come back to. Repeat the export now and then.

### YouTube account (liked videos and follows)

1. In [Google Cloud Console](https://console.cloud.google.com/) create a project and enable **YouTube Data API v3**.
2. Configure the OAuth consent screen (External, add your own Google account as a test user).
3. Create credentials > OAuth client ID > type **TVs and Limited Input devices**. Copy the client ID and secret.
4. Put them in `/opt/apa/.env` as `YOUTUBE_CLIENT_ID` and `YOUTUBE_CLIENT_SECRET`, then `./deploy.sh up`.
5. Dashboard > **Diary** > **Connect YouTube**: type the shown code at google.com/device and allow read-only access.

The server then reads your liked videos (up to 200) and followed channels every night (a few quota units of the free 10,000). The
account can be disconnected on the same page; that also deletes what it gave. The API gives no watch history, which is why the
Takeout import above exists.

## The advertising ID

The Android advertising ID is only a resettable number that ad networks use to recognise a phone: it holds no information about you
or the ads you see, and Google offers no API for your ad profile. It is not used here. Your interests are learned from what you
actually use (app time, YouTube, the diary). Google's own list of your ad interests is at myadcenter.google.com if you want to read
it and tell the assistant with `/diary` or "remember that I like ...".

## What the assistant learns from the diary (observations)

Each night the diary also records what you said you ate, felt, did, liked and disliked as structured observations (a meal at a
time, a mood from 1 to 5, a like...). The kinds are open: if you keep mentioning something new (skincare, reading), it gets its own
kind with no code change, and shows up as a recurring habit. The profile then reports average mood and how it differs after short
sleep, what you like and dislike, and what you eat. Observations are encrypted like the rest of the memory.

## Chat retention

Raw chat text older than `CHAT_KEEP_DAYS` (default 7, `0` keeps everything) is erased every night, after the day was written
into the diary (days that have no diary entry yet are summarised first, up to 5 a night; days still waiting keep their text).
Payments and their confirmation messages, the diary, learned items, notes and the private journal are never touched.
`POST /memory/prune` runs it now.

## How the assistant uses all of this

- **Your writing style** (`style` in the profile): language, message length, casualness and emoji are measured from your chat and the
  assistant writes back the same way. It keeps the last good measurement after old chat is erased.
- **Hobbies and taste** (`hobbies`, `ai.content`): app time by kind, your busiest apps and what you watch, listen to and like on
  YouTube. The assistant mentions them the way a friend would and never recites numbers.
- **Care check-ins:** a few times a day the server looks at last night's sleep, whether any meal was recorded, being up past
  midnight, caffeine and stress. If something stands out it sends one short message in your style, for each reason at most once a day
  and never more than two messages a day, never at night. It does not diagnose. Turn it off with `CARE_CHECKINS=false` in `.env`.
- **Conversations it starts** (`COMPANION_CHATS`, default on): in the morning, at midday and in the evening it may write to you first, like a
  friend, about something it really knows (a plan you mentioned, a hobby, yesterday) and in your style. Your answer continues the
  chat with full context. It never interrupts while you are chatting, waits two hours after any other proactive message, sends at most
  `COMPANION_MAX_PER_DAY` (3) a day, and stops after two unanswered messages until you write again. If it has nothing real to say it
  stays silent. `COMPANION_CHATS=false` turns it off.
- **The briefing after the alarm is one MP3:** the short briefing is spoken in a single call to the voice engine (so it costs one request,
  not one per topic) and cached; the full briefing, started by hand, still plays topic by topic.
- **Spoken briefings** use the language and voice you picked in settings; the style measurement does not change settings by itself.
- **Web research** (`RESEARCH_MODE=bundled`): ask it to look something up and it starts a background research with
  [GPT Researcher](https://github.com/assafelovic/gpt-researcher) (Apache-2.0, built from a pinned release by `deploy.sh`, about 4 GB).
  A few minutes later the findings arrive in the chat, in your style, with the source links; the full report stays for a week for
  follow-up questions. Only the topic leaves (numbers and personal data are refused), the models are this app's own gateway, search is
  DuckDuckGo unless `TAVILY_API_KEY` is set, and `RESEARCH_PER_DAY` (5) limits the use. Embeddings use `GEMINI_API_KEY` (a small call).
