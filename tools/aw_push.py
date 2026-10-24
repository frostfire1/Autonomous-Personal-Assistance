#!/usr/bin/env python3
"""Send this computer's ActivityWatch usage to your assistant (app names and seconds only: no window titles, no URLs).

Needs ActivityWatch (https://activitywatch.net) running here with its window watcher (and the AFK watcher, so idle time is not
counted). Python 3.9+, no packages. Run it by hand, or schedule it (Task Scheduler, cron, a systemd timer) every 30 minutes:

    python aw_push.py --server http://192.168.2.191:8000 --key YOUR_INGEST_API_KEY

Options (or the environment variables APA_URL, APA_KEY, APA_HMAC): --hmac SECRET when the server uses INGEST_HMAC_SECRET,
--device NAME (default: this computer's name), --kind desktop|android|other, --days N (default 2: yesterday and today),
--aw URL (default http://localhost:5600).
"""

import argparse
import hashlib
import hmac
import json
import os
import socket
import sys
import time
import urllib.request
from datetime import datetime, timedelta


def call(url, *, method="GET", body=None, headers=None):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **(headers or {})})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read() or b"null")


def query(aw, window, afk, start, end):
    lines = [f'events = flood(query_bucket("{window}"));']
    if afk:
        lines += [f'afk = flood(query_bucket("{afk}"));',
                  'events = filter_period_intersect(events, filter_keyvals(afk, "status", ["not-afk"]));']
    period = f"{start.isoformat()}/{end.isoformat()}"
    return lines, period


def day_usage(aw, window, afk, day_start, day_end):
    lines, period = query(aw, window, afk, day_start, day_end)
    merged = call(f"{aw}/api/0/query/", method="POST", body={"timeperiods": [period], "query": lines + ["RETURN = merge_events_by_keys(events, [\"app\"]);"]})[0]
    totals = {e["data"].get("app", "").strip(): e["duration"] for e in merged if e["data"].get("app")}
    first = call(f"{aw}/api/0/query/", method="POST", body={"timeperiods": [period], "query": lines + ["RETURN = limit_events(sort_by_timestamp(events), 1);"]})[0]
    last = call(f"{aw}/api/0/query/", method="POST", body={"timeperiods": [period], "query": lines + ["RETURN = limit_events(sort_by_timestamp(events), 100000);"]})[0]
    apps = {a: {"app": a, "seconds": int(s)} for a, s in totals.items() if s >= 1}
    if first and first[0]["data"].get("app") in apps:
        apps[first[0]["data"]["app"]]["first_at"] = first[0]["timestamp"]
    if last and last[-1]["data"].get("app") in apps:
        e = last[-1]
        end_at = datetime.fromisoformat(e["timestamp"].replace("Z", "+00:00")) + timedelta(seconds=e["duration"])
        apps[e["data"]["app"]]["last_at"] = end_at.isoformat()
    return list(apps.values())


def push(server, key, secret, payload):
    path = "/ingest/usage"
    body = json.dumps(payload).encode()
    headers = {"X-API-Key": key, "Content-Type": "application/json"}
    if secret:
        ts = str(int(time.time()))
        headers["X-Timestamp"] = ts
        headers["X-Signature"] = hmac.new(secret.encode(), f"{ts}.POST.{path}.".encode() + body, hashlib.sha256).hexdigest()
    req = urllib.request.Request(server.rstrip("/") + path, data=body, method="POST", headers=headers)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--server", default=os.environ.get("APA_URL"))
    p.add_argument("--key", default=os.environ.get("APA_KEY"))
    p.add_argument("--hmac", default=os.environ.get("APA_HMAC", ""))
    p.add_argument("--aw", default="http://localhost:5600")
    p.add_argument("--device", default=socket.gethostname())
    p.add_argument("--kind", default="desktop")
    p.add_argument("--days", type=int, default=2)
    a = p.parse_args()
    if not (a.server and a.key):
        sys.exit("Give --server and --key (or APA_URL and APA_KEY).")
    buckets = call(f"{a.aw}/api/0/buckets/")
    windows = [b for b in buckets if b.startswith(("aw-watcher-window_", "aw-watcher-android"))]
    afks = {buckets[b].get("hostname"): b for b in buckets if b.startswith("aw-watcher-afk_")}
    if not windows:
        sys.exit("No window watcher bucket in ActivityWatch: start aw-watcher-window first.")
    now = datetime.now().astimezone()
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for window in windows:
        afk = afks.get(buckets[window].get("hostname"))
        for back in range(a.days - 1, -1, -1):
            start = midnight - timedelta(days=back)
            end = min(start + timedelta(days=1), now)
            apps = day_usage(a.aw, window, afk, start, end)
            if apps:
                r = push(a.server, a.key, a.hmac, {"device": a.device, "kind": a.kind, "day": start.date().isoformat(), "apps": apps})
                print(f"{start.date()} {window}: {len(apps)} apps sent ({r})")


if __name__ == "__main__":
    main()
