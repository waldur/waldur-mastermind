# Resource API Keys

How a site-agent resource's API keys are generated, applied, revealed and
rotated — and, where the backend supports it, requested, assigned, limited,
paused, resumed and deleted one by one. The motivating case is inference resources (Envoy AI Gateway keys),
but nothing here is Envoy-specific: the croit-s3 plugin uses the same lifecycle
for S3 access/secret pairs, which is what surfaced the mutable-`client_id` case
below. The agent-side mechanics build on the
[event pub/sub architecture](design/pubsub-architecture.md) and the
[agent queue guide](guides/agent-pubsub.md).

## Overview

- A resource owns **multiple** API keys, each independently revealed and
  rotated from the portal.
- **Zero-downtime rotation**: rotating one key never disturbs the others, so
  consumers on a different key keep working.
- **The stored key is always a working key.** Waldur never hands a member a key
  the gateway would reject.
- **Safe visibility**: members read the keys on demand, but they are never
  plaintext where a database dump or a broad API response could leak them.
- **Per-key governance**, where the backend supports it: a key can be requested,
  assigned to a person, given per-component limits, restricted to some models, paused,
  resumed and deleted — each without touching the resource or its other keys.

## Design rationale

**The site agent generates the key, not Waldur.** Generation (and any
backend-specific format, e.g. the `sk-` prefix) lives in the site-agent plugin.
Waldur is a management layer, not the source of provider secrets. Crucially, the
agent **applies the key to the gateway first, then reports it to Waldur** — so
Waldur only ever stores a key that is already live. Minting in Waldur instead
produces phantom keys: generate → store → the apply fails → Waldur holds a key
the gateway never accepted. Requesting a key keeps this: the request creates a
row and a command, never a value.

**Waldur stores the key, encrypted, for reveal.** For multiple members to read a
key later, something durable and member-reachable must hold it. The agent has no
inbound server (it dials out only), so a live per-reveal round-trip to the agent
would be slow and fail whenever the agent is offline. Waldur holding an
**encrypted** copy keeps reveal a fast, reliable read; the agent remains the
source.

**Every change is a command the agent acknowledges.** Pause, resume, delete, a
settings update and a request all follow rotation's shape: Waldur moves the key
into a transitional state, records the command as `pending_action`, publishes
it, and settles the key only when the agent reports back. A key that has not been
acknowledged refuses a second command, so two commands can never race at the
agent.

**Keys are shared by default, personal when assigned.** Two keys are provisioned
by default — two independent keys are what make rotation zero-downtime, and they
model the operator reality of primary/standby credentials. On a team-shared
inference offering that is not enough: usage cannot be attributed to a person, a
single key cannot be limited, and a leaked key can only be rotated. So a key may
carry an **assignee**, and then only that person (plus staff and support) can
reveal it.

**A resource under per-key management starts with no keys.** Where the offering
manages keys one by one (`enable_api_key_provisioning`, below) and the agent's
backend can mint a key on request, the agent provisions none. A key can be
requested at any time there, so a replacement is created, clients move to it, and
the old key is deleted. The default pair would only be two shared keys with no
assignee and no limits, which any member of the resource's project or
organization can reveal. Its users request their own keys instead. A restored
resource follows the same rule. Its key rows were deleted when it terminated, so
it comes back with none. Existing resources keep the keys they have when the option is turned
on or off. Wherever either condition does not hold, the pair stays, as on a
backend without per-key commands such as ceph-s3. The agent decides this, so
Waldur's API and models are unchanged.

**The assignee can rotate their own key.** Whoever is trusted with a key's value
is trusted to replace it, so a person who suspects their key has leaked does not
have to find a manager while it stays live. Nothing else follows from being the
assignee: pausing, resuming, deleting and editing the key stay with whoever
manages the resource's users. A separate permission for this was considered and
dropped — it would be one more thing for a project to grant, to the one person
who already holds the secret.

**One key per person.** A person is the assignee of at most one key on a
resource; a request that would hand them a second is refused with 400, naming
the key they hold. A backend ties a person's own use of the
resource to the key assigned to them — LiteLLM's chat follows it and bills it —
and two keys would leave that two answers. A key holds its assignee until it is
`Deleted`: one still `Deleting` may yet fail and stay in service. Keys on other
resources do not count, so someone can hold one key on each of several
resources. The check runs with the resource row locked, so two concurrent
requests cannot both pass; there is no database constraint, so keys assigned
before the rule keep their assignees.

**A key's assignee is fixed when the key is requested.** It stays until the key
is deleted, and no edit sets, changes or clears it. A key changes hands by
requesting a key for the new holder and deleting the old one, and someone who
left the project is cut off by deleting their key. The key's limits and its usage
so far this month belong to the key, not to whoever holds it, so a key that
changed hands would charge one person's spending to another's allowance and
report it as theirs. Unassigning would also hand a still-working secret to
everyone who can reveal the resource's unassigned keys. An unassigned key stays
shared: to give someone a personal key, request one for them. Without key
management a key can be neither requested nor deleted, so an assignee set while
it was on stays, and a manager can still rotate the key. An edit could change
the assignee in 8.1.3, so one that still names `user` is refused with 400 rather
than silently dropped.

**Limits are enforced by Waldur, not the gateway.** The Envoy backend's
`set_resource_limits` is a no-op by design: limits are enforced by Waldur (report
→ pause → block). A per-key limit works the same way — reported per-key usage that
reaches the limit issues a `pause` for that key. Nothing new is pushed to the
gateway to enforce a limit, and a limit never affects billing.

**Limits are monthly.** A key's usage is counted per month, like a resource's
usage per billing period, and its limits apply to each month afresh. A key Waldur
paused for its limit therefore comes back on its own once a new month begins, and
so it does when the limit is raised; a key a person paused stays paused.

**Deletion is soft.** A deleted key keeps its row and the usage it reported, so
the resource's per-component totals for the month do not move when a key goes
away. Hard deletion would lose usage that belongs in the resource's total.

**Governance is a backend capability.** Not every key-carrying backend can honour
a per-key command: ceph-s3's `pause_resource` is a no-op and its `client_id`
rotates with the secret. The offering declares support with
`plugin_options.enable_api_key_provisioning`; without it the governance fields
read as null and the governance actions are refused, while reveal and rotation
work as before.

The option is a claim the operator makes about the agent's backend, and nothing
checks it: the agent does not yet report what its backend supports. Turned on
for a backend without per-key commands (ceph-s3, for one), every request, pause,
resume, update or delete errs at the agent and leaves the key `Erred`. Turn it
on only for a backend whose agent sets `supports_resource_api_key_lifecycle`.

## Model

`ResourceApiKey` (`marketplace/models.py`) — many per resource:

| Field | Purpose |
| --- | --- |
| `resource` | `ForeignKey` — a resource owns a collection of keys |
| `client_id` | the backend's **public** identifier, e.g. `<resource_backend_id>-<n>` (one gateway Secret entry) or an S3 access key; unique per resource, and may move on rotation. Blank on a requested key until the agent creates it |
| `key_ciphertext` | the agent-generated value, Fernet-encrypted ([field encryption](#encryption-at-rest)); dropped when the key is deleted |
| `issued_at` | when the agent last stored a value: the key's creation or its latest rotation. Unlike `modified`, a pause, resume or edit leaves it alone, so it is the age of the value in use |
| `state` | FSM, aligned to resource states (below) |
| `pending_action` | the command in flight (`ResourceApiKeyActions`: `create`, `rotate`, `pause`, `resume`, `delete`, `update`); blank once settled, kept on `Erred` to show which command failed |
| `user` | optional assignee, set when the key is requested and never changed; only they can reveal the key, and they can rotate it. A person is the assignee of at most one key per resource that is not `Deleted` |
| `limits` | optional per-component limits, keyed by component type; zero means no limit |
| `allowed_models` | the models the key may call, never null: `["*"]` for every model, `[]` for none, or a list of names ([below](#model-lists)) |
| `current_usages` | per-component usage in `usage_period`, as last reported by the agent |
| `usage_period` | the month `current_usages` covers (its first day); usage of an earlier month counts against no limit |
| `paused_by_limit` | Waldur paused the key because its usage reached a limit; such a key is resumed automatically once under its limits again |
| `models_outdated` | the resource's model selection changed while the key could not take an `update`; Waldur sends one once the key settles ([below](#resource-selection)) |
| `error_message` | agent-reported failure detail when `Erred` |
| `modified` | `TimeStampedModel`; the time the key last changed state or settings — the agent's sweep reads it as "in this state since". Usage reports do not touch it |

The key value is deliberately **not** on `Resource`: it never touches the broad
`ResourceSerializer`, gets its own permission-gated endpoints, and stays out of
admin and reversion history. `ResourceSerializer` carries only `has_api_keys`, a
boolean (an `Exists` annotation on the consumer and provider resource viewsets,
with a per-instance fallback) so the portal can offer key management without
knowing the backend —
`offering_type` cannot tell these backends apart, they are all site-agent offerings.

**`client_id` is the backend's public identifier, and it is not always stable.**
Envoy uses a slot, `<resource_backend_id>-<n>`, that survives rotation: only the
value behind it changes. The croit-s3 plugin puts the S3 **access key** there,
which a rotation replaces along with the secret. `set_key` therefore accepts
an optional `client_id`; a backend with a stable identifier omits it. A value
already held by another key of the same resource — a deleted one included — is
rejected: usage is attributed by `client_id`, so a deleted key's identifier is
never handed out again.

### Model lists {#model-lists}

A key's `allowed_models` has three forms, and so has its resource's selection
([below](#resource-selection)):

| Value | Meaning on a key | Meaning on a resource |
| --- | --- | --- |
| `["*"]` | every model its resource allows | every model of the offering, including ones it adds later |
| `[]` | no model: the key cannot call anything | no model: none of its keys can call anything |
| `["a", "b"]` | those models | those models |

"Every model" has a value of its own so that an empty list can mean what it
looks like. With null or an empty list standing for every model, the selection
that looked most restrictive was the least restrictive one, and a key limited to
a model the provider later retired could be widened to all of them by saving it
with nothing ticked.

- `*` means every model only as the sole entry; next to another entry it is
  refused. It is not a pattern: `provider/*` is just a model name.
- A key's names must be among those its resource allows: the resource's own
  names, or the offering's `resource_options.options.models.choices` when the
  resource is on every model (any name, when the offering lists none). A name
  the offering no longer lists is allowed by no resource.
- A request that leaves `allowed_models` out creates a key with `[]`; an edit that
  leaves it out does not change it. Null is refused.
- A key the agent reports on its own (`report_created`) was never restricted at
  the backend, so it is stored as `["*"]`. Keys that existed before the list
  became non-null were migrated from null to `["*"]`.
- On an offering without `enable_api_key_provisioning` the field reads as null,
  like every governance field.

A backend that cannot restrict a key's models has to refuse every list but
`["*"]`, the empty one included, so that Waldur never shows "No models" for a
key that can call all of them. The Envoy AI Gateway plugin does, so on Envoy a
resource has to stay on every model: any narrower effective list errs the key.
Removing a model from the offering's choices drops it from every effective
list that names it, but `["*"]` names nothing: a key whose effective list is
`["*"]`, and every key on Envoy, can call a retired model for as long as the
backend serves it. The provider has to remove it from the backend too.

#### The resource's selection {#resource-selection}

A resource option named `models` of type `select_string_multi` is the resource's
model selection, on any offering: it takes `["*"]`, a new resource starts on it,
and an empty one means none. Under `enable_api_key_provisioning` it narrows
every key of the resource; without it, it narrows nothing, but an offering that
turns key management on later finds its resources already on every model. When
selections start or stop narrowing — key management is turned on, or the option
becomes or stops being a multiple choice — or the provider adds or removes a
model in the option's choices, every key whose effective list changes with it
is sent an `update`. Turning key management off sends nothing:
its keys take no governed command. A single-choice `models` option has no room
for `*` and narrows nothing.

- **Effective list.** Waldur stores a key's list as chosen and sends the agent
  the effective one: the resource's selection, then the key's, less any model
  the offering no longer lists. Every model stays `["*"]`.

  | Resource | Key | Sent |
  | --- | --- | --- |
  | `["*"]` | `["*"]` | `["*"]` |
  | `["a", "b"]` | `["*"]` | `["a", "b"]` |
  | `["a", "b"]` | `["b", "c"]` | `["b"]` |
  | `[]` | anything | `[]` |
  | `["*"]` | `["a", "c"]`, with `c` no longer listed | `["a"]` |

  The key listing carries both: `allowed_models` is the key's own list,
  `effective_models` the one a command carries. A sweep that replays a command
  from the listing sends `effective_models`. A key keeps names its resource has
  since stopped allowing, or the offering stopped listing, and an edit may send
  them back unchanged; it cannot add new ones. A model the provider lists again
  is sent again to the keys that kept it.
- **Values.** Resource-option validation (`update_options`,
  `update_options_direct`, and the provider's `new_options` at approval)
  accepts a lone `*` although it is not among the option's choices, and refuses
  it next to a name.
- **New resources** are on `["*"]` unless the order chose a selection. Resources
  that had no selection, or an empty one, were migrated to `["*"]`, since an
  empty selection used to leave their keys every model. An offering that lists
  an "All models" entry among its own choices should drop it; resources holding
  it have to be switched to every model by hand.
- **A key the agent reports** on its own (`report_created`) is stored as
  `["*"]`; when its resource allows fewer models, Waldur sends it an `update`
  with them at once.
- **A change** to the selection, by any path that saves the resource's options,
  sends an `update` for every key whose effective list it changes. Requesting
  and reporting a key lock the resource row, so a key created while the
  selection changes is either seen by the change or created with the new one.
  A paused key's `resume` carries the new list instead, and a deleted one needs
  none. A key that cannot take a command — one is in flight, or its last one
  failed — is marked `models_outdated`; once it settles `OK`, Waldur sends the
  `update`, unless a command carrying its settings (`create`, `update`,
  `resume`) went first. The hourly `send_outdated_api_key_models` task sends
  any settled key still marked.
- **Rollout.** A site agent that predates `effective_models` replays a stuck
  command with the key's own `allowed_models`, which is wider than what Waldur
  sent when the resource is narrowed. Upgrade agents before narrowing resources.
  Keys the upgrade finds applied wider than their resource now allows — until
  then a selection narrowed only the portal's dialogs, and a model the offering
  stopped listing was still sent — are marked
  `models_outdated` by a migration and sent their effective list by the hourly
  task. On Envoy those keys then err, within the hour: put such resources on
  every model before upgrading, by clearing their selection, which the
  migration then turns into `["*"]`.
  The migration rewrites resources, not pending orders: an option order placed
  before it with an empty `models` meant every model, and now means none. Check
  pending option orders of such offerings when upgrading.

### States

The FSM reuses the **resource state vocabulary** so the portal renders it with
the standard `StateIndicator` (`@/core/StateIndicator`) — no bespoke badge:

| State | Meaning | Indicator |
| --- | --- | --- |
| `Creating` | agent is generating + applying the initial key | spinner |
| `OK` | applied and live at the gateway | green |
| `Updating` | a rotate, pause, resume or update is in flight; `pending_action` says which | spinner |
| `Paused` | the gateway refuses the key; it keeps its value | — |
| `Deleting` | the agent is revoking the key | spinner |
| `Deleted` | revoked; the row stays for its usage, hidden from default listings | — |
| `Erred` | the agent could not apply the change | red |

```mermaid
stateDiagram-v2
    [*] --> Creating: provision / request
    Creating --> OK: report_created / set_key
    OK --> Updating: rotate / update / pause
    Paused --> Updating: resume
    Updating --> OK: set_key (rotate) / set_ok (resume, update)
    Updating --> Paused: set_paused
    OK --> Deleting: delete
    Paused --> Deleting: delete
    Deleting --> Deleted: set_deleted
    Creating --> Erred: set_erred
    Updating --> Erred: set_erred
    Deleting --> Erred: set_erred
    Erred --> Updating: retry rotate / update / pause / resume
    Erred --> Creating: retry create
    Erred --> Deleting: delete
    Creating --> Deleted: delete (no client_id yet)
    Erred --> Deleted: delete (no client_id yet)
    Erred --> OK: set_key / set_ok
    Erred --> Paused: set_paused
    Erred --> Deleted: set_deleted
    OK --> [*]: resource terminated (rows deleted)
```

Every acknowledgement names the commands it settles, and is refused otherwise:
`set_key` settles `create` and `rotate`, `set_ok` settles `resume` and `update`,
`set_paused` settles `pause`, `set_deleted` settles `delete`. So a late or
duplicated report can neither resurrect a paused or deleted key nor settle a
command it does not answer — a rotation report cannot un-pause a key.
`set_erred` is accepted from any transitional state but never from `OK`, `Paused`
or `Deleted`: a stale failure report must not flip a key that has since settled.

Erred is always recoverable, but only by the command that failed or by a
delete. `pending_action` stays set on an Erred key, and `POST /{uuid}/retry/`
sends that command again, with the limits and models the key has now; posting the
same command directly (a `pause` after a failed pause, a `PATCH` of limits or
models after a failed update) is a retry too. Any other command is refused with
`409`: a rotation standing in for a failed pause would leave the key `OK` and
live, and one standing in for a failed update would show limits and models the
backend never received. A requested key that erred before it had a `client_id`
never reached the backend, so it can only be requested again (`retry`) or
deleted. A key that erred before commands were recorded retries a rotation.

A requested key without a `client_id` — still `Creating`, or `Erred` on its
`create` — holds nothing at the backend, so deleting it publishes no command:
it is `Deleted` at once, even while its `create` is still pending. That is the
way out of a request whose command was lost. Should the agent still create the
key afterwards, its `set_key` is refused with `409`, and the agent withdraws the
key it minted from the backend, so nothing is left live that Waldur does not
track.

Once governance is switched off, a failed governed command (`pause`, `update`,
`delete`, `create`) cannot be retried; an ungoverned one (`rotate`, `resume`)
may then take its place, so the key is not stranded.

## Flows

```mermaid
sequenceDiagram
    participant M as Member portal
    participant W as Waldur
    participant Q as RabbitMQ
    participant A as Site agent
    participant B as Backend Secret

    Note over M,B: Create (2 keys)
    W->>Q: provision event
    Q->>A: event
    A->>A: generate key-1, key-2
    A->>B: write 2 Secret entries
    A->>W: POST report_created (per key)  -- state OK
    Note over M,B: Rotate one key
    M->>W: POST keys/{id}/rotate  -- Updating (rotate)
    W->>Q: command (key id, slim)
    Q->>A: command
    A->>A: generate new value
    A->>B: overwrite that client_id only
    A->>W: POST set_key (new value)  -- state OK
    Note over M,B: Request a key
    M->>W: POST keys/ {resource, user, limits}  -- Creating (create)
    W->>Q: command (key id, limits, models)
    A->>B: write a new Secret entry
    A->>W: POST set_key (value, client_id)  -- state OK
    Note over M,B: Pause a key
    M->>W: POST keys/{id}/pause  -- Updating (pause)
    W->>Q: command
    A->>B: block that client_id
    A->>W: POST set_paused  -- state Paused
    Note over M,B: Reveal
    M->>W: GET keys/{id}/reveal (audited)
    W-->>M: {api_key}  (decrypted)
```

Rotation never touches the other keys, so a consumer authenticating with a
sibling key keeps working throughout — that is what makes it zero-downtime, and
why two keys are provisioned where keys are not managed one by one.

Commands (observable type `resource_api_key_rotation` — the name predates the
other commands and agents subscribe to it by name) carry a **slim** payload:
resource/key identifiers, `client_id`, and `action`, never key material.
`create`, `rotate`, `update` and `resume` also carry the key's `limits` and its
effective `allowed_models` ([the resource's selection](#resource-selection)), so
settings edited while a key was paused are applied when it resumes, and a
rotated key keeps the settings Waldur has for it. Agents
from before `rotate` carried them ignore them and keep the backend's. The key only ever
travels agent → Waldur, in the `report_created` / `set_key` request body (TLS),
and is encrypted on receipt. When a resource is
terminated, its remaining key rows — deleted ones included — are deleted by the
termination callback.

**Limits.** The agent reports each key's usage so far in a month with
`report_usage`. When a key is `OK`, has no command in flight and its usage this
month reaches a non-zero limit, Waldur issues a `pause` for that key alone, marks
it `paused_by_limit` and audit-logs it as automatic. A key over its limit while
a command is in flight is checked again when the agent acknowledges that
command: if it settles `OK`, it is paused at once rather than serving until the
next report. The resource and its other keys keep serving. An `Erred` key is not paused automatically — its failed
command needs a person to retry it first.

Waldur resumes a key it paused for its limit, again audit-logged as automatic, as
soon as its usage is under every limit again:

- a usage report for a new month, since limits start afresh each month;
- a limit raised above the usage, or removed — the `resume` carries the new limits;
- the hourly `resume_api_keys_under_limit` task, for a month that begins before
  the agent reports anything for it.

A resume by hand clears the mark, but does not override the limit: if the key is
still over it, it is paused again once the agent acknowledges the resume. A key a person paused is never resumed automatically.

## API

All endpoints live under `/api/marketplace-resource-api-keys/`
(`ResourceApiKeyViewSet`); keys are addressed by their own UUID and filtered per
resource with `?resource_uuid=`, and further by `state`, `pending_action`,
`has_pending_action` (`false` for keys with no command in flight), `user_uuid`,
`offering_uuid` and `modified_before`. A listing leaves out `Deleted` keys
unless `?state=Deleted` asks for them.

Consumer side. The actions marked *governance* need the offering's
`enable_api_key_provisioning`; without it they return 400.

- `GET /` and `GET /{uuid}/` — list/retrieve status (`client_id`, state,
  `pending_action`, `modified`, error message, assignee, limits, models as chosen
  and effective, usage — no secret). Visible to resource project/customer
  members and the provider organization.
- `GET /{uuid}/reveal/` — return the decrypted value (in the body, never the
  URL, with `Cache-Control: no-store`). Restricted to **consumer-side** access —
  a member of the resource's project or its customer, plus staff/support; a
  provider-org member (who can reach the write actions) is explicitly excluded,
  and so are minimal-visibility viewers (as with `backend_metadata`). A key with
  an assignee is revealed **only to the assignee** (plus staff/support). Only an
  `OK` key is revealed — a transitional or paused key's stored value may not
  match the gateway. **Audited** — each reveal emits the
  `marketplace_resource_api_key_revealed` event identifying the key.
- `POST /{uuid}/rotate/` — the `rotate` command. Available on every backend,
  and to the key's assignee as well as to whoever holds `RESOURCE.MANAGE_USERS`.
- `POST /{uuid}/resume/` — the `resume` command. Available on every backend
  too, so a key paused before the provider switched governance off can come
  back.
- `POST /{uuid}/retry/` — re-sends the command that left the key `Erred`
  (`pending_action`); governed unless that command is `rotate` or `resume`. The
  key's assignee can retry a failed `rotate`; a retry of any other command is
  refused to them with `403`.
- `POST /` *(governance)* — request a key: `{resource, user?, limits?,
  allowed_models?}`. Creates a `Creating` row and the `create` command; Waldur
  generates nothing. The assignee must be a member of the resource's project (an
  organization role alone is not enough) and must not already hold a key on the
  resource (see **One key per person** above), limits must name offering components,
  and models must be among those the resource allows. `["*"]`, which stands for
  every model the resource allows, is always accepted, and a request without
  `allowed_models` creates a key with `[]`, no model
  ([model lists](#model-lists)).
- `PATCH /{uuid}/` *(governance)* — change `limits`, `allowed_models`. The
  change is the `update` command — or, on a paused key, is stored, audited as
  `marketplace_resource_api_key_updated` and sent with its `resume`. An edit
  that names `user`, `null` included, is refused with 400 on `user` on every
  offering, with or without key management: the key is left as it was, and
  nothing is published or audited (see **A key's assignee is fixed when the key
  is requested** above).
- `POST /{uuid}/pause/` *(governance)* — the `pause` command.
- `DELETE /{uuid}/` *(governance)* — the `delete` command; `202`, since the key
  is gone only once the agent acknowledges. A requested key with no `client_id`
  yet is `Deleted` at once, with no command (see [States](#states)).
- `GET /usage_totals/?resource_uuid=` — the resource's reported key usage this
  month, summed per component, deleted keys included.

Every command is gated on `RESOURCE.MANAGE_USERS` (project or customer scope).
`rotate`, and a `retry` of a failed rotation, also admit the key's **assignee**
without that permission, under the conditions `reveal` holds them to: still a
member of the resource's project or its customer, and not a minimal-visibility
viewer. An assignee who no longer reaches the key at all — they left the
project and hold no role on its customer — gets `404`, since the key is not
listed for them; one who still lists it some other way (through the provider
organization, or as global support) gets `403`. Whether the caller is the
assignee, and which command a retry repeats, are checked with the key locked. An
unassigned key gives ordinary members nothing: it is rotated by a manager.

Three things stay closed to the assignee:

- Their rotation never stands in for another command that failed. Once
  governance is off a manager may rotate a key whose `pause` failed, but for
  the assignee that would undo a pause meant for them, so it is refused with
  `403`.
- They cannot rotate an `OK` key whose usage this month has reached one of its
  limits (`409`). A usage report pauses only a settled key, so a rotation in
  flight would hold the limit pause off for the very person the limit is set
  for. A manager can still rotate it, and the key is paused once the agent
  acknowledges the rotation.
  This holds only where limits are enforced: not once governance is off, and
  not for a retry of a failed rotation, since an `Erred` key is not paused for
  its limit and the retry is what settles it.
- A [personal access token](personal-access-tokens.md) cannot act as the
  assignee: its scopes delegate role permissions, and being a key's assignee is
  not one, so a token limited to reading would otherwise be able to replace the
  key.

A rotation in flight also makes the key refuse a manager's `pause` or delete
until the agent acknowledges it. Each rotation is audited, and deleting the key
ends its assignee's rotations.

Every command is refused while the resource is terminating/terminated or while
the key already has an unacknowledged command, and emits an audit event naming
whoever issued it — a manager, the assignee, or Waldur itself for a limit pause
or resume:
`marketplace_resource_api_key_requested`, `_rotated`, `_paused`, `_resumed`,
`_updated` or `_deleted`. Those events read as requested ("Pause of API key … has
been requested by …"), since the agent has not carried the command out yet; an
agent's failure report emits `marketplace_resource_api_key_failed` naming the
command and the error.

Provider side (used by the site agent), gated on `RESOURCE.MANAGE_API_KEY` held
on any of the provider-side scopes the permission is granted at — the offering
itself (`OFFERING.MANAGER`, which is what a site agent runs as), the offering's
customer (`CUSTOMER.OWNER`), or that customer's `ServiceProvider`
(`CUSTOMER.MANAGER`). The list of keys is likewise visible to the provider
organization, but reading a value is not: `reveal` stays consumer-side only.

- `POST /report_created/` — the agent pushes a key it created on its own at
  provisioning; Waldur encrypts, stores, and the row lands `OK`. Idempotent per
  `(resource, client_id)`, but refused for a key that has since been paused or
  deleted or has another command in flight, and once the resource itself is
  terminating/terminated. **Not** for a requested key: that row has no
  `client_id` to match on, so `report_created` would add a second row and leave
  the requested one `Creating`. A requested key is reported with `set_key` on
  its own UUID.
- `POST /{uuid}/set_key/` — the agent pushes a created or rotated value; Waldur
  encrypts, stores, transitions to `OK`. Takes an optional `client_id` for
  backends whose public identifier rotates with the secret; a requested key must
  send the one it was given.
- `POST /{uuid}/set_ok/` — acknowledges `resume` or `update`.
- `POST /{uuid}/set_paused/` — acknowledges `pause`.
- `POST /{uuid}/set_deleted/` — acknowledges `delete`; the value is dropped, the
  row and usage kept.

- `POST /{uuid}/set_erred/` — the agent reports that a command failed → `Erred`,
  audited as `marketplace_resource_api_key_failed`.
- `POST /{uuid}/report_usage/` *(governance)* — `{usages: {component_type:
  value}, billing_period?}`: the key's usage so far in that month (any day of
  it; the current month when omitted). A report for the month the key holds is
  merged into `current_usages`; one for a later month replaces it. Only the
  latest month is kept, so a report for an earlier one is refused with `409`,
  and one for a future month with `400`. May trigger a limit pause, or the resume
  of a key paused for its limit. Accepted for a deleted key, so usage up to the
  deletion still counts.

The acknowledgements (`set_key`, `set_ok`, `set_paused`, `set_deleted`,
`set_erred`) are not gated on `enable_api_key_provisioning`: a command already in
flight still settles if the provider switches governance off.

**Reconciliation sweep contract.** A command is published once; if the agent
misses it, the key waits in its transitional state. An agent that re-drives keys
stuck in `Updating` (listing `state=Updating&modified_before=…`) must replay the
command named by `pending_action` with the key's `effective_models`, not assume
a rotation: `Updating` also covers `pause`, `resume` and `update`, and a
rotation replayed over a lost pause would install a new secret that `set_key`
then refuses. Keys stuck in `Creating` and
`Deleting` are found the same way; a replayed `create` that lands after the
original one is refused by `set_key` and withdrawn by the agent. A sweep written
before this change must be updated before an offering turns on
`enable_api_key_provisioning`.

## Encryption at rest {#encryption-at-rest}

This is one of several encrypted columns; see [Field encryption](field-encryption.md)
for the general feature (what is encrypted, key configuration, rotation, backups).

Keys are encrypted with Fernet via `waldur_core.core.encryption`
(`encrypt_value` / `decrypt_value` / `is_encrypted`), keyed by
`settings.FIELD_ENCRYPTION_KEY`. `FIELD_ENCRYPTION_KEY` is a separate setting
from `SECRET_KEY`: leaking Django settings must not, by itself, unlock encrypted
DB fields.

**Rotating the encryption key.** `FIELD_ENCRYPTION_KEY_FALLBACKS` (a
comma-separated list) holds previous keys so the app can be re-keyed without
downtime, using `MultiFernet`: encryption always uses the primary
`FIELD_ENCRYPTION_KEY`, but decryption is attempted against the primary *and*
every fallback. To rotate:

1. Generate a new Fernet key, set it as `FIELD_ENCRYPTION_KEY`, and move the
   previous key into `FIELD_ENCRYPTION_KEY_FALLBACKS`. Existing rows still
   decrypt (via the fallback); new writes use the new primary.
2. Run `waldur reencrypt_fields`, which rewrites every stored token under the new
   primary. Waiting for rows to be re-saved on their own does not work here: a key
   is only rewritten when it happens to be rotated, so there is no point at which
   you could tell the old key had become unnecessary.
3. Drop the old key from `FIELD_ENCRYPTION_KEY_FALLBACKS`.

`waldur reencrypt_fields --dry-run` reports the same counts without writing, and in
particular how many rows **no** configured key can decrypt. That is worth checking
on its own: such rows are invisible until someone calls `reveal` and gets a 409, and
the only fix is to restore the key that wrote them.

When no dedicated key is configured, the key is derived from `SECRET_KEY` (with
a startup warning). That derived key always remains an **implicit last-resort
decrypt fallback**, so a deployment that starts without a dedicated key can
introduce `FIELD_ENCRYPTION_KEY` later without losing the rows written before
the switch — no manual fallback entry or re-encrypt migration needed. (This
costs nothing at rest: an attacker holding `SECRET_KEY` and a dump can decrypt
those pre-switch rows regardless of the server's fallback list.)

| Threat | Protection |
| --- | --- |
| Database dump / backup theft | Values are opaque Fernet tokens |
| Casual `SELECT` / SQL-injection read | Ciphertext, not plaintext |
| Over-broad API serialization | Keys live off `ResourceSerializer`, behind gated endpoints |
| Application-server compromise | **Not** protected — the process can decrypt |

Defense-in-depth against at-rest exposure, not a secrets manager.

## Usage and billing

Billing stays **per resource**: a resource's usage is what the agent reports for
the resource, and rotating, adding, pausing or deleting a key does not change it.
A key limit never affects an invoice.

Per-key usage (`current_usages`, reported with `report_usage`) is a breakdown of
that total for one month — it is what limits are enforced against and what lets
usage be attributed to an assignee. Summed over all of a resource's keys, deleted
ones included, it should add up to the resource's usage in that month
(`usage_totals`), which is why deletion is soft. Only a key's latest month is
kept: the resource's own usage history stays in its component usages.

How each backend gets there is plugin-specific and documented with the plugin.
The Envoy AI Gateway tags requests with a per-key `x-client-id`; per-key numbers
depend on the usage shipper emitting them per `client_id`. croit-s3 meters the S3
user directly, so keys never enter usage at all — and it does not declare
`enable_api_key_provisioning`.
