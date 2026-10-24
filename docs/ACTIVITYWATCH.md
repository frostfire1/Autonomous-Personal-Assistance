# App usage with ActivityWatch

[ActivityWatch](https://activitywatch.net) records which app you use and for how long, on Android and on desktop, and keeps it on
your own devices. The assistant reads its totals so it knows your screen time, your busiest apps and when you are up late, and
uses that in its advice (profile trait `screen_time`, and your "when active" estimate).

**What leaves your devices:** app names and seconds per day. Never window titles, URLs or document names.

## Android

1. Install ActivityWatch for Android and grant it the usage-access permission. Leave it installed.
2. Nothing else: this app reads ActivityWatch on the phone (`127.0.0.1:5600`) and sends today and yesterday with every sync, at
   most every 30 minutes. If ActivityWatch is not installed or not running, it does nothing.

## Desktop (Windows, macOS, Linux)

Run ActivityWatch with its window and AFK watchers (the AFK watcher keeps idle time out), then:

```bash
python tools/aw_push.py --server http://YOUR-SERVER:8000 --key YOUR_INGEST_API_KEY
```

Schedule it every 30 minutes (Task Scheduler, cron or a systemd timer). Add `--hmac SECRET` if the server uses
`INGEST_HMAC_SECRET`, `--device NAME` to label the machine. Any other tool can push the same shape to `POST /ingest/usage`:
`{"device": "...", "kind": "android|desktop|ios|web|other", "day": "YYYY-MM-DD", "apps": [{"app": "...", "seconds": 600}]}`;
a push replaces that device's day.

Read it back: `GET /usage` (the dashboard API) shows per-day totals per device and the most used apps.

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
