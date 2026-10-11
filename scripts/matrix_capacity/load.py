#!/usr/bin/env python
"""Hold N Waldur chat drawers open against a Matrix homeserver and measure delivery.

Each simulated user does what homeport's background auto-connect does with
matrix-js-sdk 40 (startClient({initialSyncLimit: 30}), MemoryStore, no filter
option, presence left on):

  * POST /user/{id}/filter with the SDK's default filter, once per page load;
  * an initial /sync with that filter inline plus timeline.limit=30, timeout=0;
  * /sync with timeout=0 while the last response carried to-device events;
  * then the long-poll: since=<next_batch>, the stored filter, timeout=30000,
    org.matrix.msc4222.use_state_after=true, no set_presence;
  * a refresh at /_matrix/client/v3/refresh when the access token is due,
    before the next request, as the SDK's TokenRefresher does.

Messages are a Poisson process over all users (--msg-interval seconds per user
on average). Each carries the send time; every other member's sync records the
arrival. A fraction of users (--active) behave as if their drawer were open on
the room and post a read receipt for each message they receive.

The sessions come from provision.py. Refresh tokens rotate, so the file is
rewritten with the latest tokens after each run and stays usable for the next.

    uv run --with httpx python load.py --sessions sessions.jsonl --users 1000 \\
        --base-url http://127.0.0.1:6167 --out runs/n01000

See README.md next to this file.
"""

import argparse
import asyncio
import datetime
import json
import multiprocessing as mp
import os
import random
import resource
import signal
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.parse
import uuid

import httpx

DEFAULT_FILTER = {"room": {"timeline": {"unread_thread_notifications": True}}}
POLL_TIMEOUT_MS = 30_000
# matrix-js-sdk gives up on a /sync after pollTimeout + BUFFER_PERIOD_MS (80 s).
SYNC_LOCAL_TIMEOUT_S = 110
REQUEST_TIMEOUT_S = 15
REFRESH_IF_EXPIRES_WITHIN_S = 0.5
# On M_UNKNOWN_TOKEN the SDK refreshes and retries only when the token was due
# within a minute; otherwise the session is gone.
EXPIRED_IN_FLIGHT_S = 60
# An encrypted text event is ~0.5 KB of ciphertext; the plain body is padded to
# that size so payloads match.
PAD = "x" * 380


class User:
    def __init__(self, row):
        self.row = row
        self.user_id = row["user_id"]
        self.room_id = row["room_id"]
        self.access_token = row["access_token"]
        self.refresh_token = row["refresh_token"]
        # Expired unless refreshed recently; the first request refreshes.
        self.expiry = row.get("expiry", 0)
        self.lock = asyncio.Lock()
        self.refreshing = None
        self.ready = False
        self.active = False


class Stats:
    def __init__(self):
        self.lat = []  # (sent_at, latency_s)
        self.send_lat = []
        self.counts = {}
        self.sync_status = {}
        self.initial_sync_s = []
        self.loop_lag = []

    def inc(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n


async def _refresh(client, user, stats):
    try:
        r = await client.post(
            "/_matrix/client/v3/refresh",
            json={"refresh_token": user.refresh_token},
            timeout=REQUEST_TIMEOUT_S,
        )
    except httpx.HTTPError:
        stats.inc("refresh_error")
        raise
    if r.status_code != 200:
        stats.inc(f"refresh_{r.status_code}")
        if r.status_code in (401, 403):
            # The drawer would ask Waldur for a new session now.
            user.row["broken"] = True
        raise RuntimeError(f"refresh {r.status_code} {r.text[:200]}")
    body = r.json()
    user.access_token = body["access_token"]
    user.refresh_token = body.get("refresh_token", user.refresh_token)
    user.expiry = time.time() + body.get("expires_in_ms", 300_000) / 1000
    stats.inc("refresh_ok")


async def ensure_token(client, user, stats):
    async with user.lock:
        if user.expiry - time.time() > REFRESH_IF_EXPIRES_WITHIN_S:
            return
        # Refresh tokens rotate: a refresh cut off by the end of a run would
        # leave this user with tokens the homeserver no longer accepts.
        user.refreshing = asyncio.ensure_future(_refresh(client, user, stats))
        await asyncio.shield(user.refreshing)


async def call(client, user, stats, method, path, **kw):
    for attempt in (0, 1):
        await ensure_token(client, user, stats)
        token = user.access_token
        headers = {"Authorization": f"Bearer {token}"}
        r = await client.request(method, path, headers=headers, **kw)
        if r.status_code != 401 or attempt:
            return r
        if user.access_token == token:
            if user.expiry - time.time() > EXPIRED_IN_FLIGHT_S:
                return r
            user.expiry = min(user.expiry, time.time())
        # Expired while in flight, or refreshed meanwhile by another request:
        # the SDK retries this transparently, so it is not a failure.
        stats.inc("expired_in_flight")
    return r


def handle_sync(body, user, stats, t_recv, window, receipts):
    rooms = body.get("rooms", {}).get("join", {})
    newest = None
    for room_id, room in rooms.items():
        for ev in room.get("timeline", {}).get("events", []):
            if ev.get("type") != "m.room.message":
                continue
            content = ev.get("content", {})
            sent = content.get("cap_ts")
            if sent is None or ev.get("sender") == user.user_id:
                continue
            sent /= 1000
            newest = (room_id, ev["event_id"])
            if window[0] <= sent <= window[1]:
                stats.lat.append((sent, t_recv - sent))
    if user.active and newest and user.ready:
        receipts.append(newest)
    stats.inc("to_device", len(body.get("to_device", {}).get("events", [])))


async def run_user(client, user, stats, start_at, window, stop):
    await asyncio.sleep(max(0, start_at - time.time()))
    if stop.is_set():
        return
    t_load = time.time()
    try:
        # Page load: what the SDK fetches before and with its first sync.
        for path in (
            "/_matrix/client/versions",
            "/_matrix/client/v3/capabilities",
            "/_matrix/client/v3/pushrules/",
        ):
            await call(client, user, stats, "GET", path, timeout=REQUEST_TIMEOUT_S)
        uid = urllib.parse.quote(user.user_id)
        r = await call(
            client,
            user,
            stats,
            "POST",
            f"/_matrix/client/v3/user/{uid}/filter",
            json=DEFAULT_FILTER,
            timeout=REQUEST_TIMEOUT_S,
        )
        r.raise_for_status()
        filter_id = r.json()["filter_id"]
        initial = json.loads(json.dumps(DEFAULT_FILTER))
        initial["room"]["timeline"]["limit"] = 30
        params = {
            "filter": json.dumps(initial, separators=(",", ":")),
            "timeout": 0,
            "org.matrix.msc4222.use_state_after": "true",
            "_cacheBuster": int(time.time() * 1000),
        }
    except Exception as e:
        stats.inc("page_load_error")
        stats.inc(f"page_load_error:{type(e).__name__}")
        return
    since = None
    catching_up = True
    while not stop.is_set():
        if since is not None:
            params = {
                "filter": filter_id,
                "timeout": 0 if catching_up else POLL_TIMEOUT_MS,
                "since": since,
                "org.matrix.msc4222.use_state_after": "true",
            }
        try:
            r = await call(
                client,
                user,
                stats,
                "GET",
                "/_matrix/client/v3/sync",
                params=params,
                timeout=SYNC_LOCAL_TIMEOUT_S,
            )
        except httpx.TimeoutException:
            stats.inc("sync_timeout")
            await asyncio.sleep(1)
            continue
        except Exception as e:
            if stop.is_set():
                return
            stats.inc("sync_error")
            stats.inc(f"sync_error:{type(e).__name__}")
            await asyncio.sleep(2)
            continue
        t_recv = time.time()
        stats.sync_status[r.status_code] = stats.sync_status.get(r.status_code, 0) + 1
        if r.status_code != 200:
            await asyncio.sleep(2)
            continue
        body = r.json()
        if since is None:
            stats.initial_sync_s.append(t_recv - t_load)
            user.ready = True
            stats.inc("ready")
        since = body["next_batch"]
        catching_up = bool(body.get("to_device", {}).get("events"))
        receipts = []
        handle_sync(body, user, stats, t_recv, window, receipts)
        for room_id, event_id in receipts:
            asyncio.ensure_future(send_receipt(client, user, stats, room_id, event_id))


async def send_receipt(client, user, stats, room_id, event_id):
    rid = urllib.parse.quote(room_id)
    eid = urllib.parse.quote(event_id)
    try:
        r = await call(
            client,
            user,
            stats,
            "POST",
            f"/_matrix/client/v3/rooms/{rid}/receipt/m.read/{eid}",
            json={},
            timeout=REQUEST_TIMEOUT_S,
        )
        stats.inc(f"receipt_{r.status_code}")
    except Exception:
        stats.inc("receipt_error")


async def send_message(client, user, stats):
    txn = uuid.uuid4().hex
    sent = time.time()
    # Canonical JSON has no floats, so the send time goes in as milliseconds.
    content = {
        "msgtype": "m.text",
        "body": f"capacity {txn} {PAD}",
        "cap_ts": int(sent * 1000),
    }
    try:
        rid = urllib.parse.quote(user.room_id)
        r = await call(
            client,
            user,
            stats,
            "PUT",
            f"/_matrix/client/v3/rooms/{rid}/send/m.room.message/{txn}",
            json=content,
            timeout=REQUEST_TIMEOUT_S,
        )
        stats.inc(f"send_{r.status_code}")
        if r.status_code != 200 and stats.counts[f"send_{r.status_code}"] <= 3:
            print(f"send {r.status_code}: {r.text[:300]}", flush=True)
        if r.status_code == 200:
            stats.send_lat.append(time.time() - sent)
    except Exception as e:
        stats.inc("send_error")
        stats.inc(f"send_error:{type(e).__name__}")


async def sender(client, users, stats, rate, window, stop):
    # Senders only start once the ramp is over, like the latency window.
    await asyncio.sleep(max(0, window[0] - time.time()))
    while not stop.is_set() and time.time() < window[1]:
        await asyncio.sleep(random.expovariate(rate))
        ready = [u for u in users if u.ready]
        if ready:
            asyncio.ensure_future(send_message(client, random.choice(ready), stats))


async def lag_monitor(stats, stop):
    while not stop.is_set():
        t = time.perf_counter()
        await asyncio.sleep(0.1)
        stats.loop_lag.append(time.perf_counter() - t - 0.1)


async def worker_main(rows, args, t_start, window, out_path):
    users = [User(r) for r in rows]
    for u in users:
        u.active = random.random() < args.active
    stats = Stats()
    stop = asyncio.Event()
    limits = httpx.Limits(max_connections=None, max_keepalive_connections=None)
    async with httpx.AsyncClient(
        base_url=args.base_url, limits=limits, timeout=REQUEST_TIMEOUT_S
    ) as client:
        tasks = [
            asyncio.ensure_future(
                run_user(
                    client,
                    u,
                    stats,
                    t_start + random.uniform(0, args.ramp),
                    window,
                    stop,
                )
            )
            for u in users
        ]
        rate = len(users) / args.msg_interval
        tasks.append(
            asyncio.ensure_future(sender(client, users, stats, rate, window, stop))
        )
        tasks.append(asyncio.ensure_future(lag_monitor(stats, stop)))
        # Leave time after the window for the last messages to arrive.
        await asyncio.sleep(max(0, window[1] + args.drain - time.time()))
        stop.set()
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(
            *(u.refreshing for u in users if u.refreshing), return_exceptions=True
        )
    tokens = [
        {
            **u.row,
            "access_token": u.access_token,
            "refresh_token": u.refresh_token,
            "expiry": u.expiry,
        }
        for u in users
    ]
    with open(out_path, "w") as f:
        json.dump(
            {
                "lat": stats.lat,
                "send_lat": stats.send_lat,
                "counts": stats.counts,
                "sync_status": stats.sync_status,
                "initial_sync_s": stats.initial_sync_s,
                "loop_lag": stats.loop_lag,
                "tokens": tokens,
            },
            f,
        )


def worker(rows, args, t_start, window, out_path):
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    asyncio.run(worker_main(rows, args, t_start, window, out_path))


def stats_sampler(prefix, path, stop_at):
    with open(path, "w") as f:
        while time.time() < stop_at:
            t = time.time()
            out = subprocess.run(
                ["docker", "stats", "--no-stream", "--format", "{{json .}}"],
                capture_output=True,
                text=True,
            ).stdout
            for line in out.splitlines():
                row = json.loads(line)
                if row["Name"].startswith(prefix):
                    f.write(json.dumps({"t": t, **row}) + "\n")
            f.flush()
            time.sleep(max(0, 5 - (time.time() - t)))


def pct(values, p):
    if not values:
        return None
    values = sorted(values)
    k = min(len(values) - 1, int(round(p / 100 * (len(values) - 1))))
    return values[k]


def mem_mib(s):
    s = s.split("/")[0].strip()
    for unit, f in (
        ("GiB", 1024),
        ("MiB", 1),
        ("KiB", 1 / 1024),
        ("B", 1 / 1024 / 1024),
    ):
        if s.endswith(unit):
            return float(s[: -len(unit)]) * f
    return None


def iso(t):
    return datetime.datetime.fromtimestamp(t, datetime.UTC).isoformat(
        timespec="seconds"
    )


def summarize(args, parts, stats_path, window):
    lat = [x for p in parts for _, x in p["lat"]]
    send = [x for p in parts for x in p["send_lat"]]
    counts, sync_status = {}, {}
    for p in parts:
        for k, v in p["counts"].items():
            counts[k] = counts.get(k, 0) + v
        for k, v in p["sync_status"].items():
            sync_status[k] = sync_status.get(k, 0) + v
    init = [x for p in parts for x in p["initial_sync_s"]]
    lag = [x for p in parts for x in p["loop_lag"]]
    docker = {}
    if stats_path and os.path.exists(stats_path):
        with open(stats_path) as f:
            for line in f:
                try:
                    row = json.loads(line)
                except ValueError:  # the sampler may be mid-line
                    continue
                if not window[0] <= row["t"] <= window[1]:
                    continue
                d = docker.setdefault(row["Name"], {"cpu": [], "mem": []})
                d["cpu"].append(float(row["CPUPerc"].rstrip("%")))
                d["mem"].append(mem_mib(row["MemUsage"]))
    syncs = sum(sync_status.values())
    bad = sum(v for k, v in sync_status.items() if str(k) != "200")
    sends_ok = counts.get("send_200", 0)
    summary = {
        "users": args.users,
        "ready": counts.get("ready", 0),
        "window_start": iso(window[0]),
        "window_end": iso(window[1]),
        "window_s": window[1] - window[0],
        "msg_rate_per_s": round(args.users / args.msg_interval, 3),
        "sends_ok": sends_ok,
        "sends_failed": sum(
            v for k, v in counts.items() if k.startswith("send_") and k != "send_200"
        ),
        "deliveries": len(lat),
        "p50_s": pct(lat, 50),
        "p95_s": pct(lat, 95),
        "p99_s": pct(lat, 99),
        "max_s": max(lat) if lat else None,
        "send_p50_s": pct(send, 50),
        "send_p95_s": pct(send, 95),
        "initial_sync_p50_s": pct(init, 50),
        "initial_sync_p95_s": pct(init, 95),
        "syncs": syncs,
        "sync_non200": bad,
        "sync_timeouts": counts.get("sync_timeout", 0),
        "sync_errors": counts.get("sync_error", 0),
        "expired_in_flight": counts.get("expired_in_flight", 0),
        "refresh_ok": counts.get("refresh_ok", 0),
        "refresh_failed": sum(
            v
            for k, v in counts.items()
            if k.startswith("refresh_") and k != "refresh_ok"
        ),
        "page_load_errors": counts.get("page_load_error", 0),
        "loop_lag_p99_s": pct(lag, 99),
        "loop_lag_max_s": max(lag) if lag else None,
        "counts": counts,
        "sync_status": sync_status,
        "docker": {
            name: {
                "cpu_avg_pct": round(statistics.mean(d["cpu"]), 1),
                "cpu_max_pct": round(max(d["cpu"]), 1),
                "mem_max_mib": round(max(m for m in d["mem"] if m is not None), 1),
            }
            for name, d in docker.items()
            if d["cpu"]
        },
    }
    p95 = summary["p95_s"]
    summary["pass"] = bool(
        summary["ready"] == args.users
        and p95 is not None
        and p95 < args.p95_limit
        and summary["sync_timeouts"] == 0
        and summary["refresh_failed"] == 0
        and (bad + summary["sync_errors"]) <= 0.001 * max(syncs, 1)
        and summary["sends_failed"] == 0
    )
    return summary


def write_private(path, lines):
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.writelines(lines)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser(
        description="Hold N chat drawers open against a Matrix homeserver and "
        "measure message delivery."
    )
    ap.add_argument("--sessions", required=True, help="from provision.py create")
    ap.add_argument("--users", type=int, required=True)
    ap.add_argument(
        "--skip",
        type=int,
        default=0,
        help="use sessions from this line on, e.g. to split the users between "
        "two load machines",
    )
    ap.add_argument(
        "--base-url",
        required=True,
        help="the homeserver's client API, e.g. http://127.0.0.1:6167",
    )
    ap.add_argument("--procs", type=int, default=0, help="0: one per 250 users")
    ap.add_argument(
        "--ramp", type=float, default=30, help="seconds over which pages load"
    )
    ap.add_argument(
        "--settle", type=float, default=20, help="pause between ramp and window"
    )
    ap.add_argument(
        "--duration", type=float, default=180, help="measured window, after the ramp"
    )
    ap.add_argument("--drain", type=float, default=10)
    ap.add_argument(
        "--msg-interval",
        type=float,
        default=300,
        help="mean seconds between a user's messages",
    )
    ap.add_argument(
        "--active",
        type=float,
        default=0.1,
        help="fraction of users with the room open, posting read receipts",
    )
    ap.add_argument("--p95-limit", type=float, default=2.0)
    ap.add_argument(
        "--docker-stats-prefix",
        help="sample `docker stats` of containers whose names start with this; "
        "only when the homeserver runs on this machine's Docker",
    )
    ap.add_argument("--out", required=True, help="directory for this run's output")
    args = ap.parse_args()

    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    resource.setrlimit(resource.RLIMIT_NOFILE, (min(65536, hard), hard))

    with open(args.sessions) as f:
        all_rows = [json.loads(line) for line in f]
    rows = all_rows[args.skip : args.skip + args.users]
    if len(rows) < args.users:
        sys.exit(f"only {len(rows)} sessions in {args.sessions}")
    # Such a user syncs but never sends, so the step would pass under less
    # traffic than its user count says.
    no_room = [r["username"] for r in rows if not r.get("room_id")]
    if no_room:
        sys.exit(
            f"{len(no_room)} sessions have no room, e.g. {', '.join(no_room[:3])}. "
            "Create them again with provision.py create and a new --out file."
        )
    os.makedirs(args.out, exist_ok=True)
    procs = args.procs or max(1, (args.users + 249) // 250)
    t_start = time.time() + 2
    window = (
        t_start + args.ramp + args.settle,
        t_start + args.ramp + args.settle + args.duration,
    )
    print(
        f"{args.users} users, {procs} procs, ramp {args.ramp}s, window {args.duration}s, "
        f"{args.users / args.msg_interval:.2f} msg/s",
        flush=True,
    )

    stats_path = None
    sampler = None
    if args.docker_stats_prefix:
        stats_path = os.path.join(args.out, "docker-stats.jsonl")
        sampler = mp.Process(
            target=stats_sampler,
            args=(args.docker_stats_prefix, stats_path, window[1] + args.drain),
        )
        sampler.start()
    chunks = [rows[i::procs] for i in range(procs)]
    with tempfile.TemporaryDirectory() as tmp:
        # The workers' results carry live tokens; only the sessions file keeps
        # them, so the run directory can be shared.
        paths = [os.path.join(tmp, f"part-{i}.json") for i in range(procs)]
        workers = [
            mp.Process(target=worker, args=(c, args, t_start, window, p))
            for c, p in zip(chunks, paths)
        ]
        for w in workers:
            w.start()
        for w in workers:
            w.join()
        if sampler:
            sampler.join()
        parts = []
        for p in paths:
            with open(p) as f:
                parts.append(json.load(f))

    # Refresh tokens rotate: write back the latest ones for the next run.
    latest = {t["username"]: t for p in parts for t in p.pop("tokens")}
    write_private(
        args.sessions,
        [json.dumps(latest.get(r["username"], r)) + "\n" for r in all_rows],
    )
    for i, p in enumerate(parts):
        with open(os.path.join(args.out, f"part-{i}.json"), "w") as f:
            json.dump(p, f)

    summary = summarize(args, parts, stats_path, window)
    with open(os.path.join(args.out, "summary.json"), "w") as f:
        json.dump(summary, f, indent=2)
    print(json.dumps({k: v for k, v in summary.items() if k != "counts"}, indent=2))


if __name__ == "__main__":
    main()
