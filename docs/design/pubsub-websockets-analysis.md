# Pub/sub and WebSockets: state-of-the-system analysis

An assessment of how real-time messaging works in waldur-mastermind today,
how browser-facing WebSocket delivery fits into it, and where the gaps are.
The architecture itself is documented in
[the pub/sub architecture doc](pubsub-architecture.md); this document analyzes
it, with emphasis on the WebSocket angle.

## Summary

- Waldur has **no in-process WebSocket stack**: no Django Channels, no ASGI,
  no SSE event stream, no MQTT, no socket.io, and no Redis. Serving is
  WSGI/gunicorn only (`src/waldur_core/server/wsgi.py`,
  `docker/rootfs/etc/waldur/gunicorn.conf.py`).
- All real-time delivery is **RabbitMQ over STOMP**. Django is a publisher
  only: signals build payloads, Celery workers push them to per-consumer
  queues via `stomp-py` (`src/waldur_core/logging/utils.py`,
  `publish_stomp_messages`).
- Consumers connect to RabbitMQ directly, not to Django. Site agents and
  IdM/IGA syncs use plain STOMP; the browser UI can connect through
  RabbitMQ's `rabbitmq_web_stomp` plugin (STOMP over WebSocket), gated by
  the experimental `marketplace.realtime_updates` feature flag
  (`src/waldur_core/core/features.py`).
- The design is deliberate: Waldur's authorization is dynamic, so fan-out is
  computed application-side (one queue per consumer, re-authorized at publish
  time) instead of relying on broker-native routing keys.

## What exists

### The publish pipeline

Flow: Django signal → payload builder → `prepare_messages()` /
`dispatch_user_event()` → `publish_messages.delay()` (Celery) →
`publish_stomp_messages()` → STOMP `SEND` to `/amq/queue/{queue}`.

Key components, all in `src/waldur_core/logging/`:

- `utils.py` — the single STOMP publisher: one `stomp.Connection12` per
  message, plus a token-bucket rate limiter (500 msg/s, burst 1000),
  hash-based dedup with a 4h cache TTL, per-resource monotonic sequence
  numbers, and throttled failure logging.
- `circuit_breaker.py` — `stomp_circuit_breaker` (5 failures to open, 60s
  recovery, 2 successes to close). When OPEN, batches are dropped rather
  than retried.
- `backend.py` — RabbitMQ management-API client (vhosts, users,
  permissions, queues) and the queue arguments: 1h message TTL, 10k max
  length, overflow rejected to a DLX (`waldur.dlq.messages`).
- `event_dispatch.py` — the marketplace-free dispatcher: scope-key
  resolution, consumer matching, batched re-authorization, envelope
  stamping (`object_type`, `schema_version`, `event_type`).
- `tasks.py` — the `publish_messages` Celery task with exponential backoff
  (max 5 retries, capped at 300s, jittered).

Marketplace events enter through
`src/waldur_mastermind/marketplace/utils.py` (`prepare_messages()`, payload
enrichers) and the signal handlers in `marketplace/handlers.py`; the site
agent plugin (`src/waldur_mastermind/marketplace_site_agent/`) adds queue
registration, identity-sync messages, and beat-scheduled queue cleanup.

### The consumer model

- **Unified path (current):** one durable queue per `EventConsumer`,
  named `consumer_{uuid.hex}`, in a per-user vhost (`{user_uuid.hex}`).
  Consumers demultiplex on the envelope's `object_type`.
- **Legacy path (deprecated, WAL-10111):** per-offering/object-type queues
  named from `subscription/{sub}/offering/{off}/{object_type}` topics.
  Migration runbook lives in
  [the agent pub/sub guide](../guides/agent-pubsub.md).

Object types (`src/waldur_core/logging/enums.py`): `order`, `user_role`,
`resource`, `offering_user`, `importable_resources`, `service_account`,
`course_account`, `resource_periodic_limits`, `offering_resources_sync`,
`resource_api_key_rotation`, `resource_end_date_change_request`,
`user_profile`, `user_ssh_key`, `user_lifecycle`.

### Authentication and authorization

- Broker credentials are per consumer: a random RabbitMQ user with
  permissions only on the owner's vhost; the passcode is the caller's
  Personal Access Token (only its hash is stored) or DRF token.
- Authorization is enforced twice: at registration
  (`holds_any_role_on_scope_or_ancestor()`) and again at every publish
  (`users_with_role_on_any_scope_key()`, one batched query per event), so
  revoking a role stops delivery immediately.
- Global consumers (no scope bindings) receive an all-user firehose and are
  staff/support only, checked at both registration and delivery.
  `object_types` is a convenience filter, not a security boundary.
- Registration races are closed with a Postgres advisory lock and
  `select_for_update`.

### The WebSocket path that does exist

Browser delivery is **broker-terminated, not Django-terminated**:

1. RabbitMQ enables `rabbitmq_web_stomp` (see
   `.devcontainer/rabbitmq-enabled-plugins`).
2. The ingress proxies `/rmqws-stomp` on the API host to the broker's
   web-STOMP listener (the proxy is a Helm/ingress concern, not in this
   repo).
3. Homeport, when `marketplace.realtime_updates` is enabled, registers an
   `EventConsumer` bound to the user's own scope and subscribes over
   STOMP-over-WebSocket with the per-consumer credentials.

This reuses the entire authz, dedup, TTL, and cleanup machinery unchanged —
the browser is just another consumer.

### Adjacent streaming (not the event bus)

- AI chat answers stream as HTTP chunked NDJSON
  (`StreamingHttpResponse` in `src/waldur_mastermind/chat/views.py`), not
  SSE and not WebSockets.
- Matrix chat / LiveKit (`src/waldur_mastermind/matrix_chat/`) is a
  separate real-time chat and AV integration.
- Web/e-mail hooks (`src/waldur_core/logging/models.py`) push events as
  HTTP POSTs with SSRF re-validation at request time.

## Assessment

### What the current design gets right

- **No new server tier.** Delegating WebSocket termination to RabbitMQ
  avoids running ASGI servers, channel layers, and Redis alongside the
  WSGI/Celery deployment — a real operational saving for a platform many
  operators self-host.
- **Uniform security model.** Browser, agent, and IdM consumers all go
  through the same registration, per-vhost isolation, and publish-time
  re-authorization. There is no second, weaker path for UI updates.
- **Failure containment.** Circuit breaker, rate limiting, bounded queues
  with DLX overflow, dedup IDs, and Celery retry/backoff mean a slow or
  dead broker degrades event delivery without taking down request serving.
- **App-side fan-out matches dynamic authz.** Broker routing keys are
  static; Waldur roles are not. Computing recipients per event (one batched
  role query) is the correct trade-off here.

### Gaps and risks

- **Connection-per-message publishing.** `publish_stomp_messages()` opens
  and closes a STOMP connection for every message. At the rate limiter's
  ceiling this is a lot of TCP/STOMP handshakes; a pooled or long-lived
  connection per worker process would cut latency and broker churn. The
  rate limiter and breaker currently mask this cost.
- **At-most-once when the breaker is open.** Once the circuit breaker
  opens, batches are dropped, and the 1h queue TTL bounds delivery anyway.
  Consumers must treat the bus as lossy and reconcile via the REST API
  (agents already do; a UI consumer must too). This is documented but easy
  to overlook when building new consumers.
- **Inconsistent envelopes.** Core-dispatched events carry `event_type`;
  marketplace events carry `offering_uuid` but no `event_type` (a known
  gap noted in the architecture doc). Unifying the envelope would simplify
  consumer demultiplexing.
- **The browser path depends on out-of-repo wiring.** The `/rmqws-stomp`
  proxy and the `rabbitmq_web_stomp` plugin are deployment concerns; a
  misconfigured ingress fails silently from Waldur's point of view. There
  is no health endpoint that verifies end-to-end web-STOMP reachability.
- **Credential coupling to tokens.** The RabbitMQ passcode is the user's
  API token; token rotation invalidates broker credentials, and a live
  WebSocket session holds a credential equivalent to full API access for
  that user. A scoped, broker-only credential would narrow the blast
  radius.
- **Hard-coded tuning.** Rate-limiter and circuit-breaker constants are
  code, not settings; large deployments cannot tune them without a patch.
- **Legacy path still live.** The deprecated per-offering queue scheme
  still runs in parallel in `prepare_messages()`; every event pays for
  both matching passes until the migration completes.

### If in-process WebSockets were ever wanted

Adopting Django Channels/ASGI would mean a channel layer (Redis), a second
server tier, and re-implementing the per-consumer authorization that the
broker path already provides — for little gain, since RabbitMQ web-STOMP
already reaches the browser. The stronger candidates for future work are
incremental: pool STOMP connections, unify the envelope, add an end-to-end
web-STOMP health check, issue broker-scoped credentials, and finish the
legacy-path retirement.

## Replacing RabbitMQ: options survey

RabbitMQ currently does three jobs, and any replacement plan has to cover
all three or consciously split them:

1. **Celery broker** — AMQP, durable quorum queues
   (`server/celery_settings.py`).
2. **Event bus for external consumers** — site agents and IdM/IGA syncs
   over STOMP, one durable queue per consumer, per-user vhost isolation,
   credentials provisioned via the management API.
3. **Browser push** — STOMP over WebSocket via `rabbitmq_web_stomp`
   behind `/rmqws-stomp`.

The STOMP consumer contract is public (waldur-site-agent and external
IdM integrations implement it), so jobs 2 and 3 carry a cross-repo
migration cost regardless of the technology chosen.

### Candidate technologies

**NATS + JetStream.** Single Go binary; subjects with wildcard routing;
JetStream durable consumers replace per-consumer queues; native WebSocket
listener for browsers (no proxy plugin); accounts give vhost-like
multi-tenant isolation; the *auth callout* extension delegates
authentication and per-subject authorization to an external service —
i.e. Django could keep exactly the dynamic-authorization model it has
today, evaluated at connect time. Caveats: Celery/kombu has **no NATS
transport** (an open feature request), so adopting NATS for events still
requires a second answer for Celery; the Python client is asyncio-first;
agents must be rewritten from STOMP to NATS.

**Redis / Valkey.** The only candidate that covers the Celery-broker job
out of the box (Redis is Celery's second first-class broker). Streams +
consumer groups can model per-consumer durable delivery, and ACLs can
scope users to key/channel patterns, but there is no vhost concept, no
management API for tenant provisioning, and delivery guarantees are
weaker than quorum queues (visibility-timeout redelivery, data loss on
abrupt termination unless carefully tuned). No native browser transport —
a WebSocket tier must be added on top. Licensing note: Redis moved to
RSAL/SSPL in 2024 and added AGPLv3 in 2025; the BSD-licensed **Valkey**
fork is now the default in major distros and the low-drama choice for a
self-hosted open-source platform.

**PostgreSQL as the queue.** Waldur already runs Postgres, and the
publish path is already capped at 500 msg/s by the app-side rate limiter
— well inside the 1k–10k dequeues/s a modest Postgres sustains with
`SELECT ... FOR UPDATE SKIP LOCKED` or the **pgmq** extension.
Per-consumer queues become rows scoped by the existing permission
system, so vhosts, broker users, and the management-API provisioning
code disappear entirely. LISTEN/NOTIFY is only a wake-up signal (it is
lossy, serializes commits, and breaks behind transaction-mode
PgBouncer); durable state lives in tables. The consequence: external
agents switch from STOMP push to authenticated HTTPS polling/long-poll
against a REST events endpoint — a bigger contract change, but one that
removes broker credentials from agents altogether. For the task queue,
**Procrastinate** is the production-ready Postgres-native Celery
replacement with Django integration (periodic tasks, retries, locks),
turning the whole stack into Django + Postgres + nothing else.

**Centrifugo.** Not a broker — a self-hosted WebSocket/SSE fan-out
server that pairs with either of the above for job 3. Django publishes
via HTTP POST; per-channel authorization via JWT or a connect-proxy
callback into Django (again preserving dynamic authz). Battle-tested,
language-agnostic, and much cheaper operationally than adopting Django
Channels + ASGI + Redis channel layers in-process.

**MQTT (EMQX / Mosquitto).** Per-topic ACLs and built-in WebSocket
support, but offline durability is session-based rather than queue-based,
tenant provisioning is weaker than vhosts, and it brings no answer for
Celery. No advantage over NATS for this workload.

**Kafka / Redpanda.** Ruled out: static partition-oriented ACLs clash
with dynamic per-user authorization, browsers need a proxy anyway,
operational weight is far above Waldur's throughput needs, and Celery
cannot use it as a broker.

### Two coherent end-states

- **Option 1 — NATS JetStream + Valkey.** NATS takes jobs 2 and 3
  (durable consumers, WebSocket, auth callout for dynamic authz); Valkey
  takes job 1 as the Celery broker. Closest functional match to today's
  architecture: push-based agents, per-consumer durable state, browser
  connects straight to the messaging tier. Cost: two new components
  replace one, Celery loses quorum-queue durability semantics, and both
  waldur-site-agent and the auth-callout service must be built.

- **Option 2 — Postgres-centric.** Events become pgmq/outbox tables
  drained over REST (agents poll/long-poll with their existing API
  tokens); browser push becomes SSE from Django or a small Centrifugo
  sidecar; Celery is either kept on Valkey or replaced by Procrastinate.
  Fewest moving parts of any option — potentially zero brokers — and
  authorization stays purely in Django. Cost: the largest consumer-side
  contract change, and the database absorbs queue write load (mitigated
  by the existing rate cap, TTLs, and cleanup tasks).

A pragmatic sequencing: since the messages are already treated as lossy
(1h TTL, breaker drops), start by moving the event bus, keep Celery on
RabbitMQ until the end, and only then swap the task broker — the two
paths are already fully decoupled in the code
(`publish_messages.delay()` is the only seam between them).

## Removing Celery as well

Dropping Celery is a different order of magnitude from dropping RabbitMQ,
because Waldur does not use Celery as a generic task queue — it uses it
as the **resource-provisioning workflow engine**. An inventory of actual
usage:

- **336 `@shared_task` functions** plus ~80 class-based tasks built on a
  vendored copy of Celery 4.3's removed task metaclass
  (`waldur_core/core/tasks.py:34-88`).
- **The executor machinery is structurally coupled to Celery canvas.**
  203 executor subclasses across 19 `executors.py` files emit `chain`s of
  `.si()` signatures (89 chains, 474 immutable signatures; OpenStack
  provisioning chains run 20–40 steps). Resource FSM state is *defined*
  by which Celery callback fires: success = `link` ran, ERRED =
  `link_error` ran, and `ErrorStateTransitionTask` reads the failed
  sibling's result and traceback out of the result backend
  (`core/tasks.py:316-341`). A chain killed mid-way strands the resource
  — which is why three beat sweeps exist solely to un-stick resources.
- **Retry as control flow.** `PollRuntimeStateTask` (`max_retries=1200`
  at 5s) and the provisioning throttle use `self.retry()` for backend
  polling and admission control — 55 + 58 sites.
- **164 beat entries**, 130 of them contributed through the public
  `WaldurExtension.celery_tasks()` plugin hook (26 implementations),
  each mirrored into a Sentry Cron monitor by `server/sentry_crons.py`.
- **Load-bearing result backend** (Postgres `celery_taskmeta`): error
  propagation and the `reset_updating_resources` sweep both read
  `AsyncResult`; `instance.task_id` is a model column.
- **Worker introspection surfaces**: `control.inspect()` /
  `control.ping()` back two REST endpoints, the health check, and the
  support status command.
- **Locking schemes that assume Celery semantics**: `BackgroundTask`
  smuggles a lock key through Celery message headers and releases it in
  `after_return`; `openportal.run_once_task` is documented as correct
  only because `CELERY_TASK_TIME_LIMIT=1800` hard-kills tasks.

### What could replace it

- **Procrastinate** (Postgres-native, Django integration) covers tasks,
  retries with backoff, cron-style periodic tasks, priorities, locks,
  future scheduling, and cancellation — i.e. everything *except* canvas.
  There is no chain/link_error equivalent; the executor layer cannot be
  ported, only rewritten.
- **Django 6's Tasks framework** standardizes the enqueue interface but
  ships no production worker and no periodic scheduling; the reference
  `django-tasks` DB backend is suitable for light workloads, not for a
  164-job schedule with polling loops. Worth adopting as the *interface*
  eventually, not as the engine.
- **Dramatiq / Huey / RQ** all require Redis (or RabbitMQ) and offer no
  canvas either — they trade one broker for another without removing the
  workflow-engine gap, so they add nothing over Procrastinate here.

### The honest assessment

The tasks, beat schedule, locks, and throttles all port to Procrastinate
with mechanical effort (large but shallow: the
`WaldurExtension.celery_tasks()` hook can keep its shape and feed
Procrastinate's periodic registry; the DB-cache lock becomes a real
Postgres lock; polling loops become scheduled re-enqueues with attempt
counters).

The genuinely hard part is one subsystem: **`core/executors.py` +
`core/tasks.py`**. Removing Celery means replacing implicit
chain/link_error orchestration with an explicit, DB-backed workflow: an
operation table holding the step list, current step, attempt counts, and
error text, driven by ordinary queued tasks that advance it. That is a
real engineering program — but it is also the architecturally honest
version of what exists today: the stuck-resource sweeps exist precisely
because in-flight chain state lives only inside the broker. Moving
workflow state into Postgres makes provisioning crash-recoverable,
inspectable with SQL (which also replaces the `inspect()` REST surfaces
with something better), and testable without a worker.

### Recommended program

1. **Phase 1 — retire RabbitMQ, keep Celery.** Move the event bus per
   the options above; switch Celery's broker to Valkey (a configuration
   change; the attribute-based `PriorityRouter` is broker-agnostic).
   Accepted losses: quorum-queue durability, publisher confirms, and the
   RabbitMQ-specific ops commands (`migrate_rabbitmq_queues`,
   `audit_broker_config`), which retire with it.
2. **Phase 2 — introduce the Postgres workflow engine.** Build the
   operation/step model and port executors plugin-by-plugin (the 203
   subclasses mostly *declare* step lists, so a compatibility shim over
   `get_task_signature()` can carry most of them), while new-style and
   old-style executors coexist.
3. **Phase 3 — port the long tail and delete Celery.** Move the 336
   plain tasks and 164 beat entries to Procrastinate, re-point Sentry
   monitoring (explicit cron check-ins instead of the Celery
   integration), rebuild the health check on queue tables, and drop
   Valkey if nothing else uses it — ending at Django + Postgres only.

Phase 1 is weeks; phases 2–3 are a multi-month program best run
plugin-by-plugin behind the existing executor interface.

## Pointers

- Architecture: [pubsub-architecture.md](pubsub-architecture.md)
- Runtime guide: [agent-pubsub.md](../guides/agent-pubsub.md)
- Order-processing concepts:
  [event-based-order-processing.md](../core-concepts/event-based-order-processing.md)
- Legacy path (deprecated):
  [event-subscription-queues.md](../guides/event-subscription-queues.md)
- Feature flag: `marketplace.realtime_updates` in
  [features.md](../admin/features.md)
