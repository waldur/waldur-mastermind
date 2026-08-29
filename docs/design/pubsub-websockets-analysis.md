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

## Pointers

- Architecture: [pubsub-architecture.md](pubsub-architecture.md)
- Runtime guide: [agent-pubsub.md](../guides/agent-pubsub.md)
- Order-processing concepts:
  [event-based-order-processing.md](../core-concepts/event-based-order-processing.md)
- Legacy path (deprecated):
  [event-subscription-queues.md](../guides/event-subscription-queues.md)
- Feature flag: `marketplace.realtime_updates` in
  [features.md](../admin/features.md)
