# Matrix homeserver capacity test

Measures how many concurrent chat users one Matrix homeserver can serve. It
holds N Waldur chat drawers open against the homeserver, sends messages between
them and records how long each message takes to arrive. The figure in the
Capacity section of the Matrix chat docs, for both the Helm chart and Docker
Compose (1000 concurrent users with 4 CPU cores), was measured with it. Use it
to measure your own hardware before going past that figure.

**Run it on a test deployment only, never in production.** `provision.py
create` writes synthetic users, an organization, projects and rooms into
Waldur, and accounts and rooms onto the homeserver. Those accounts and rooms
are still on the homeserver after the clean-up (see
[Cleaning up](#cleaning-up)), and the load slows chat down for everyone on that
homeserver.

| File | What it does |
| --- | --- |
| `provision.py` | Runs in the Waldur API container. `create` makes users in projects of 25, gives each project a room through Waldur's own room tasks (encrypted, the bot in every room, members joined through the appservice), and starts one web chat session per user, as `POST /api/matrix/session/` does. `cleanup` removes what `create` made. |
| `load.py` | The load generator. Simulates one drawer per session, for one user count, and writes `summary.json`. |
| `steps.py` | Runs `load.py` for several user counts in a row. |
| `table.py` | Prints the runs as a markdown table. |

## What it measures

Homeport connects every signed-in user who is a member of an active room in the
background, whether or not the drawer is open, so it can show the unread badge.
Each simulated user does what that client (matrix-js-sdk 40) does:

- loads the page: `versions`, `capabilities` and push rules, uploads the
  SDK's default filter, then an initial `/sync` with a timeline limit of 30;
- long-polls `/sync` with a 30 s timeout, with presence on;
- refreshes its access token when it is due. A request that arrives with a
  token that expired on the way is refreshed and retried, as the SDK does, and
  is not counted as a failure (`expired_in_flight`).

The traffic:

- rooms of 25 members plus the bot (`provision.py --room-size`);
- each user sends a message every 5 minutes on average (`--msg-interval 300`),
  at random times. That is busier than most project chats, and it grows with
  the number of users;
- messages are plain text, padded to the size of an encrypted event;
- one user in ten (`--active 0.1`) has the drawer open on the room and posts a
  read receipt for every message it receives.

Each run loads the users' pages over 30 s (`--ramp`), waits 20 s (`--settle`),
measures for 180 s (`--duration`) and then waits 10 s for the last messages to
arrive (`--drain`). Delivery time runs from the start of the send request to
the message arriving in another member's `/sync`. Only messages sent during
the measured window count.

A run passes when:

- every user finished the initial sync;
- p95 delivery is under 2 s (`--p95-limit`);
- no `/sync` timed out (the SDK gives up after 110 s);
- at most 0.1 % of `/sync` calls failed;
- no token refresh and no send failed.

## Before you start

- A test deployment with Matrix chat on, sized like the one you are planning:
  the same homeserver resources and the same node or host size. Its Celery
  workers must run: the clean-up relies on them.
- Waldur 8.1.3-rc.22 or later, the first release with the web chat sessions
  `provision.py` starts. Without the fix that exempts the homeserver from
  Waldur's appservice throttle, Waldur caps the homeserver's calls at 10000 an
  hour, which a run past about 800 users uses up: the API log then shows 429
  on `PUT /_matrix/app/v1/transactions`, and the diagnostics'
  `appservice_ping` fails.
- `MATRIX_AUTO_CREATE_PROJECT_ROOMS` off for the test. With it on, Celery
  creates a room for every project the script makes, racing the script's own
  room creation, so `create` refuses to run.
- Shell access to the Waldur API container (`kubectl exec` or
  `docker compose exec`).
- Somewhere to run the load from, close to the homeserver on the network, with
  Python 3.11 or later and `httpx` (`pip install httpx`, or prefix the
  commands with `uv run --with httpx`). It keeps one connection open per user;
  `load.py` raises its open-file limit to the hard limit, up to 65536, so check
  that `ulimit -Hn` is above the number of users.
- Nobody else using the homeserver while you measure.

## Reproducing the documented figure

The figure: 1000 concurrent users with the homeserver limited to 4 CPU cores,
p95 delivery 0.41 s.

### 1. Limit the homeserver to 4 CPUs

As in the Capacity section of the Matrix chat docs: with Helm,
`matrixChat.homeserver.resources.limits.cpu: "4"`; with Docker Compose,
`cpus: 4` on the `tuwunel` service in `docker-compose.override.yml`.

### 2. Create the users

Pick a prefix that is not in use on the deployment: everything the script
makes is named after it (`cap-capacity`, `cap-p0000`, `cap-u00000`).
`--homeserver-domain` must be the deployment's `MATRIX_HOMESERVER_DOMAIN`; the
script refuses to run otherwise, and refuses to take over a user, project or
organization of that name that it did not make.

On Kubernetes:

```bash
POD=$(kubectl get pod -l app=waldur-mastermind-api -o name | head -1)
kubectl exec "$POD" -- python /usr/src/waldur/scripts/matrix_capacity/provision.py \
  create --prefix cap --homeserver-domain matrix.test.example.org \
  --users 1000 --out /tmp/sessions.jsonl
(umask 077; kubectl exec "$POD" -- cat /tmp/sessions.jsonl > sessions.jsonl)
kubectl exec "$POD" -- rm /tmp/sessions.jsonl
```

With Docker Compose:

```bash
docker compose exec waldur-mastermind-api \
  python /usr/src/waldur/scripts/matrix_capacity/provision.py \
  create --prefix cap --homeserver-domain matrix.test.example.org \
  --users 1000 --out /tmp/sessions.jsonl
(umask 077; docker compose exec -T waldur-mastermind-api cat /tmp/sessions.jsonl > sessions.jsonl)
docker compose exec waldur-mastermind-api rm /tmp/sessions.jsonl
```

An image built before the script was added doesn't have it at that path: copy
it in from a checkout (`kubectl cp … "${POD#pod/}":/tmp/provision.py`,
`docker compose cp`) and run that copy instead.

If a room can't be created, `create` stops before it writes any session and
names the rooms; run it again to retry them. 1000 users took about two and a
half minutes. Create the largest number of
users you plan to test in one go: every step uses the first N sessions of the
same file.

Adding a user to a room also asks the Matrix bot to write the room's earlier
history into the user's key backup. The bot works through those requests while
`create` runs and after it, 10 users every 5 seconds, with one
`room_keys/version` call per user: the synthetic users have no key backup, so
the bot stops there. For 1000 users that takes about 8 minutes; start the steps
after that, or those calls fall into the measured window. The bot also reads every room member's
`room_keys/version`, 50 users a round, when it starts and every 6 hours after:
a pass that overlaps a step adds one more call per user to it.

`sessions.jsonl` holds working tokens of the synthetic users. Keep it private
and delete it when you are done. Use it within a day of creating it, and of
each run after that: the refresh tokens last a day, and Waldur's daily prune
signs out web sessions that were idle for 24 hours. After that, run `create`
again with a new `--out` file.

### 3. Run the steps

Point the load at the homeserver's client API directly. Not through
`kubectl port-forward`: it carries every connection through one tunnel, and
you would measure the tunnel.

On Kubernetes, run it from a pod in the deployment's namespace, copying the
scripts from a checkout of waldur-mastermind:

```bash
kubectl run cap-load --image=python:3.13-slim --restart=Never -- sleep infinity
kubectl wait --for=condition=Ready pod/cap-load --timeout=120s
kubectl cp scripts/matrix_capacity cap-load:/cap
kubectl cp sessions.jsonl cap-load:/cap/sessions.jsonl
kubectl exec cap-load -- pip install -q httpx
kubectl exec cap-load -- sh -c 'cd /cap && python steps.py \
  --sessions sessions.jsonl --base-url http://matrix-homeserver:6167 \
  --out-dir runs 250 500 1000'
kubectl cp cap-load:/cap/runs runs
(umask 077; kubectl exec cap-load -- cat /cap/sessions.jsonl > sessions.jsonl)
kubectl delete pod cap-load
```

Copy `sessions.jsonl` back before deleting the pod: the run rotated the
refresh tokens, and the copy you started with no longer works.

With Docker Compose, run it in a container on the stack's network
(`<project>` is the Compose project name, by default the name of the
directory), from a directory holding the scripts and `sessions.jsonl`:

```bash
docker run --rm --network <project>_default --ulimit nofile=65536:65536 \
  -v "$PWD:/cap" -w /cap python:3.13-slim sh -c 'pip install -q httpx && \
  python steps.py --sessions sessions.jsonl \
  --base-url http://tuwunel.internal:6167 --out-dir runs 250 500 1000'
```

Going through the ingress or Caddy instead (`https://<matrix domain>`) works
too, and adds them to what you measure.

Each step takes about 4 minutes. Meanwhile, watch the homeserver's CPU and
memory: `kubectl top pod`, `docker stats tuwunel`, or your monitoring. Each
`summary.json` records its measured window in UTC (`window_start`,
`window_end`).

On Docker Compose, `load.py` can also sample `docker stats` into the summary
itself, as the documented run did. That needs the `docker` command, so it runs
on the Docker host, and the host needs the homeserver's port, which Compose
does not publish. Add it in `docker-compose.override.yml`:

```yaml
services:
  tuwunel:
    ports:
      - '127.0.0.1:6167:6167'
```

Then, on the host:

```bash
python steps.py --sessions sessions.jsonl --base-url http://127.0.0.1:6167 \
  --out-dir runs 250 500 1000 -- --docker-stats-prefix tuwunel
python table.py runs --container tuwunel
```

### 4. Read the results

```bash
python table.py runs
```

For the figure, the `n01000` step must pass. The documented run had p95
0.41 s and p99 0.78 s, with the homeserver at 1.9 cores on average and 4.1 at
peak.

## Reading the output

Each step writes `<out-dir>/n<users>/`:

- `summary.json`: the verdict and the numbers.
  - `pass`: the criteria above.
  - `ready`: users whose initial sync finished, and `initial_sync_p95_s`.
  - `p50_s`, `p95_s`, `p99_s`, `max_s`: delivery time, over `deliveries`
    arrivals. `send_p95_s`: how long the send request took.
  - `sync_non200`, `sync_errors`, `sync_timeouts`: failed long-polls.
    `expired_in_flight`: requests retried after a refresh, not failures.
  - `refresh_failed`, `sends_failed`, `page_load_errors`.
  - `loop_lag_p99_s`: the load generator's own lag. Above 0.1 s the generator
    is the bottleneck, not the homeserver: give it more processes (`--procs`,
    by default one per 250 users) or a second machine (`--skip` splits the
    sessions between two).
  - `docker`: CPU (in % of one core) and memory per container, with
    `--docker-stats-prefix`.
- `part-*.json`: the raw delivery samples as `[sent_at, latency_s]` pairs, to
  look at part of the window, such as the burst of page loads at the start.
- `load.log`: `load.py`'s output.

No tokens end up there. Each run rewrites the sessions file it ran against with
the users' latest refresh tokens, since the homeserver rotates them. That file
is the only copy that still works: keep using it for the next run.

## Cleaning up

In the API container, as `create`:

```bash
kubectl exec "$POD" -- python /usr/src/waldur/scripts/matrix_capacity/provision.py \
  cleanup --prefix cap --homeserver-domain matrix.test.example.org

docker compose exec waldur-mastermind-api \
  python /usr/src/waldur/scripts/matrix_capacity/provision.py \
  cleanup --prefix cap --homeserver-domain matrix.test.example.org
```

Without `--yes` it only lists what it would delete. It deletes only what
carries the marker `create` sets: the `<prefix>-capacity` organization, its
`<prefix>-pNNNN` projects and their rooms, and the `<prefix>-uNNNNN` users. If
someone added a project of their own to the organization, the organization and
that project stay.

If a room can't be archived, for instance because the homeserver doesn't
answer, cleanup keeps that room, its project and the organization, deletes the
rest and exits with an error: run it again once the homeserver answers. After
any other interruption, run it again too; it picks up what is left.

Each room is archived first: the members are removed from it, and its history
is not exported. Each deleted user's Matrix account is signed out and locked by
a Celery task. The homeserver keeps the locked accounts and the archived
rooms; Waldur does not delete them. On a Docker Compose stack made only for the
test, `docker compose down -v` removes everything.

Delete `sessions.jsonl` as well.

## Running it in CI

waldur-integration-testing has a manual job, "Matrix capacity smoke (manual)",
that runs these scripts from the mastermind image under test against its
Docker E2E stack with the Matrix profile: it creates the users, waits for the
bot's history fills, runs the steps (50, 100 and 200 users by default, set
with `MATRIX_CAPACITY_USERS` when you play the job) and fails when a step does
not pass. The table, each step's `summary.json` and the homeserver, bot and
API logs are its artifacts; the sessions file never leaves the job.

To measure a merge request, open the integration-testing pipeline that the
merge request's "Trigger integration tests" job started, and play the job
there: it then runs against the image built from the merge request.

The homeserver shares a CI runner with the rest of the stack and with the
load generator, so the job's numbers are a regression signal, to compare
between runs. They are not a capacity figure: measure that as described
above, on hardware sized like the deployment.

## What it does not cover

- End-to-end encryption. Messages are plain text the size of an encrypted
  one. Every page load is a new device, which in a real drawer uploads its
  keys and has the other members share their session keys with it; none of
  that is simulated. Take the result as an upper bound.
- Other traffic: one room per user, no typing notifications, media, calls or
  federation. A test database has little history, so the homeserver's caches
  stay small.
- Homeport itself, and its requests to Waldur's API.
