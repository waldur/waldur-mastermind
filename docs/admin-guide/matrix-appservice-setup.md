# Matrix Chat Integration

## Overview

Waldur integrates with the [Matrix](https://matrix.org/) open communication protocol as an Application Service (appservice). This enables:

- **Project chat rooms** — each project can have a dedicated Matrix room
- **Automatic member sync** — project members are invited to rooms based on their roles
- **Bot commands** — operational queries (resource status, orders, members) from within the chat
- **History export** — manual, scheduled, or on-deletion export of chat messages and media
- **Web chat sessions** — Waldur's chat drawer gets short-lived Matrix tokens that live only in the browser's memory
- **External clients** — optionally, users can sign in to Element or another Matrix client
- **Room lifecycle** — disable, reactivate, and reprovision rooms

The integration works with any Matrix homeserver that supports the Application Service API (Synapse, Dendrite, Conduit/Tuwunel, etc.).

## Prerequisites

- A running Matrix homeserver with Application Service support
- Waldur reachable from the homeserver over HTTP/HTTPS
- A staff account in Waldur — the Setup appservice wizard, connectivity diagnostics, and the **Settings** tab are staff-only

The Setup appservice wizard collects everything else it needs. If the homeserver URL, homeserver domain, or user registration secret (`MATRIX_HOMESERVER_URL`, `MATRIX_HOMESERVER_DOMAIN`, `MATRIX_USER_REGISTRATION_SECRET`) are still empty in Constance, the wizard shows a prerequisites step that prompts for them and persists them. You can also set them in Constance beforehand to skip that step.

## Appservice Setup

### Via the UI

The page lives at **Administration → Configuration → Matrix chat** and is split into a **Rooms** tab and a staff-only **Settings** tab. Support users see only the Rooms tab; the Setup appservice and Check connectivity actions are staff-only.

1. Navigate to **Administration → Configuration → Matrix chat**
2. Click **Setup appservice** to open the wizard
3. **Prerequisites step** (shown only when these are missing): enter the homeserver URL, homeserver domain, and user registration secret
4. **Main step**: optionally adjust the Waldur URL (defaults to the current browser origin; this is the base URL the homeserver uses to reach Waldur) and the bot localpart (default `waldur-bot`)
5. On the result step, copy the generated registration YAML
6. Save the YAML to a file on your homeserver (e.g., `/etc/matrix/waldur-appservice.yaml`)
7. Register the file in your homeserver configuration (e.g., `app_service_config_files` in Synapse, or the equivalent directive for your homeserver)
8. Restart the homeserver

> **Warning:** If AS and HS tokens are already configured, the wizard warns that running setup again will generate new tokens and overwrite the existing ones. You will need to update your homeserver configuration with the new registration YAML.

### Via the API

#### POST /api/admin/matrix-appservice/setup/

Generates fresh appservice tokens (rotating any existing ones), enables the appservice, and returns registration YAML.

**Authentication:** Staff only (`is_staff = True`). Non-staff users receive `403 Forbidden`.

**Request body (all fields optional):**

| Field | Type | Description |
| --- | --- | --- |
| `url` | string | Waldur base URL reachable by the homeserver (e.g., `https://waldur.example.com`) |
| `sender_localpart` | string | Bot user localpart (default: `waldur-bot`) |
| `homeserver_url` | string | Homeserver URL. Persisted only when `MATRIX_HOMESERVER_URL` is still empty |
| `homeserver_domain` | string | Homeserver domain. Persisted only when `MATRIX_HOMESERVER_DOMAIN` is still empty |
| `user_registration_secret` | string | Shared registration secret (write-only). Persisted only when `MATRIX_USER_REGISTRATION_SECRET` is still empty |

The last three fields back the wizard's prerequisites step — they are only written when the corresponding Constance value is empty, so an existing configuration is never overwritten by them.

**Example request:**

```bash
curl -X POST https://waldur.example.com/api/admin/matrix-appservice/setup/ \
  -H "Authorization: Token YOUR_STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://waldur.example.com",
    "sender_localpart": "waldur-bot"
  }'
```

**Example response (200):**

```json
{
  "registration_yaml": "as_token: abc123...\nhs_token: def456...\nid: waldur\n...",
  "as_token": "abc123...",
  "hs_token": "def456...",
  "sender_localpart": "waldur-bot",
  "webhook_url": "https://waldur.example.com/_matrix/app/v1/transactions/{txnId}"
}
```

#### GET /api/admin/matrix-appservice/status/

Returns the current appservice configuration state.

**Example response (200):**

```json
{
  "enabled": true,
  "as_token_configured": true,
  "hs_token_configured": true,
  "sender_localpart": "waldur-bot",
  "bot_user_id": "@waldur-bot:matrix.example.com",
  "webhook_path": "/_matrix/app/v1/transactions/{txnId}",
  "homeserver_url": "https://matrix.example.com",
  "homeserver_domain": "matrix.example.com",
  "transaction_count": 42
}
```

#### GET /api/admin/matrix/diagnostics/

Runs live connectivity checks against the configured Matrix homeserver. Staff only.

Checks performed (the `checks` array in the response):

1. Homeserver URL configured (`homeserver_configured`)
2. Homeserver domain configured (`homeserver_domain_configured`)
3. Homeserver reachable (`homeserver_reachable`, via `/_matrix/client/versions`)
4. AS token configured (`as_token_configured`)
5. HS token configured (`hs_token_configured`)
6. Registration secret configured (`registration_secret_configured`)
7. Bot authentication (`bot_whoami`, via `/account/whoami`)
8. Bot can operate (`bot_functional`, list joined rooms)
9. Room statistics (`room_stats`: active, creating, errored counts)
10. User profile statistics (`user_stats`: provisioned count)

**Example response (200):**

```json
{
  "ok": true,
  "checks": [
    {"name": "homeserver_domain_configured", "label": "Homeserver domain configured", "ok": true, "detail": "matrix.example.com"},
    {"name": "homeserver_reachable", "label": "Homeserver reachable", "ok": true, "detail": "OK — versions: v1.11, v1.12"},
    {"name": "bot_whoami", "label": "Bot authentication (whoami)", "ok": true, "detail": "OK — authenticated as @waldur-bot:matrix.example.com"}
  ]
}
```

#### POST /api/admin/matrix/reprovision/

Resets all active rooms to `creating` state and re-queues them for provisioning on the homeserver. Also resets all user provisioning status. Use this when migrating to a new homeserver. Staff only.

Do not run it against the homeserver the rooms already live on. Old rooms are not
deleted, so each one stays behind with its history while Waldur replaces it with
an empty room.

**Example response (202):**

```json
{
  "rooms_reprovisioned": 5,
  "users_reset": 42
}
```

#### From the command line

The same operation is available as a management command, for deployments where
opening a shell is easier than authenticating to the API as staff:

```bash
waldur reprovision_matrix_rooms --dry-run   # report the counts, change nothing
waldur reprovision_matrix_rooms             # prompts before writing
waldur reprovision_matrix_rooms -y          # no prompt, for scripts
```

It prompts by default because it discards every stored room id and room alias and
resets every user's provisioning state, and only a working homeserver can issue
replacements. With no
terminal attached, as in a Kubernetes Job or a cron run, it refuses and tells you
to pass `-y`. It refuses to run when Matrix chat is disabled or the homeserver is
unconfigured: the room-creation tasks it queues would have nothing to talk to,
leaving every room stuck in `creating`. `--dry-run` works either way, so the
counts can be checked before the new homeserver is switched on.

Room creation happens in the background, so the command returns before the
rooms exist. Watch the room states to confirm they leave `creating`.

### Creating project rooms

Anyone holding the `MATRIX_ROOM.CREATE` permission on a project or its
organization can create the project's room from its Chat tab. Organization
owners hold it by default. Staff can create any room from the Chat tab;
support can create one from the Matrix admin rooms page or through
`POST /api/matrix/rooms/`.
Disabling and deleting a room stay with staff.

To let project managers create rooms too, grant them the permission in
`permissions-override.yaml`:

```yaml
- role: PROJECT.MANAGER
  add_permissions:
    - MATRIX_ROOM.CREATE
```

The grant also lets them manage the room and download its history exports, see
[Room actions](#room-actions).

If you replaced `CUSTOMER.OWNER` wholesale in `custom-roles.yaml`, add
`MATRIX_ROOM.CREATE` to that list yourself, or owners lose room creation.

To give every project a room without anyone asking for one:

- **New projects:** turn on `MATRIX_AUTO_CREATE_PROJECT_ROOMS` (off by default).
  It applies to every project created from then on, including projects
  created by loading a demo preset.
- **Existing projects:** run the backfill.

```bash
waldur provision_matrix_rooms --dry-run                 # list the projects, create nothing
waldur provision_matrix_rooms --customer <uuid>         # one organization only
waldur provision_matrix_rooms --limit 50                # at most 50 rooms this run
```

The backfill skips projects that already have a room, including an archived
one, so it is safe to re-run. Each room provisions a Matrix account for, and
invites, every member of its project and of its organization. `--limit` only caps how many rooms one
run queues; on a large deployment, wait for a batch to leave `creating` before
running the next.

### Room aliases

The registration claims an alias namespace of `#waldur-<project>:<your domain>`,
which is what the appservice needs in order to give each project room a readable
address. Without it the homeserver refuses every alias request with `M_EXCLUSIVE`,
room creation falls back to an alias-less room, and the "Open in Matrix client"
link never appears because it is only rendered when an alias exists.

If you registered the appservice before this namespace existed, re-run setup and
register the new YAML on your homeserver. Only rooms created after that get an
alias, since it is requested at creation time.

To give existing rooms an alias, run `waldur reprovision_matrix_rooms`. This is
the exception to the warning above, and it is destructive: every active project
room is replaced by an empty one, and the old room stays behind with its
history. Check the counts with `--dry-run` first.

### Token rotation

Every call to the setup endpoint generates new AS and HS tokens, overwriting any
existing ones. Re-running setup therefore invalidates the previous registration
YAML: after each call you must update your homeserver configuration with the new
YAML and restart the homeserver.

Prerequisite fields (`homeserver_url`, `homeserver_domain`,
`user_registration_secret`) are not overwritten — they are only persisted when the
corresponding Constance value is still empty.

## Chat Rooms

### Creating a room

Staff and support users create a chat room for a project. Each project can have at most one room.

**POST /api/matrix/rooms/**

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `project` | UUID | Yes | Project UUID |

The room name is automatically set to the project name. Room creation is asynchronous — a Celery task creates the room on the homeserver and sets the state to `ACTIVE`. If creation fails, the state is set to `ERROR` with a message.

The room alias is automatically generated as `#waldur-{project_uuid_prefix}:{homeserver_domain}`.

### Room states

| State | Description |
| --- | --- |
| `creating` | Room creation task is in progress |
| `active` | Room is available for use |
| `disabling` | Room is being disabled (kicking members, exporting history) |
| `archived` | Room has been disabled and archived |
| `error` | Room creation or operation failed — see `error_message` for details |

State transitions:

- `creating` → `active` (on successful creation)
- any state → `error` (on failure)
- `active` / `error` → `disabling` (on disable action or project deletion)
- `disabling` → `archived` (after members kicked and history exported)
- `archived` → `active` (on reactivate action)
- `error` / `creating` / `disabling` → `creating` (on retry action)
- `active` → `creating` (on appservice reprovision)

### Listing rooms

**GET /api/matrix/rooms/**

Returns rooms accessible to the authenticated user based on their project and customer roles. Staff and support users see all rooms.

**Query parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `project_uuid` | UUID | Filter rooms by project |
| `state` | string | Filter by room state |
| `member` | boolean | When true, return only rooms the requesting user is a member of |

### Room actions

Permissions vary per action. Whoever may create a room keeps its members in sync and exports its
history; organization owners can by default, and support can for every room. That is as far as
either goes: retrying and re-enabling a room are staff's alone.

| Action | Required permission |
| --- | --- |
| `sync_members`, `export_history` | `MATRIX_ROOM.CREATE` on the project or its organization, or staff or support |
| `retry`, `reactivate` | Staff (`is_staff`) |
| `disable`, `DELETE` | Staff (`is_staff`) |
| `join`, `leave` | Staff or support (`is_staff_or_support`) |
| `open` | A current member of the room (`invited` or `joined`) |

**POST /api/matrix/rooms/{uuid}/sync_members/**

Triggers a member sync — invites all current project members to the room and sets power levels based on roles. Returns `202 Accepted`.

**POST /api/matrix/rooms/{uuid}/export_history/**

Triggers a manual history export. Returns `202 Accepted` with the export object.

**POST /api/matrix/rooms/{uuid}/retry/**

Re-queues provisioning for a room that is stuck. Accepts rooms in `error`, `creating`, or `disabling` state. For `disabling`, the disable task is re-queued with `delete_history=False`. Returns `202 Accepted`, or `409 Conflict` if the room is in another state.

**POST /api/matrix/rooms/{uuid}/disable/**

Disables an active room. The disable process kicks all members, optionally exports history, and archives the room.

| Field | Type | Default | Description |
| --- | --- | --- | --- |
| `delete_history` | boolean | `false` | Delete all history exports for this room |

Returns `202 Accepted`.

**POST /api/matrix/rooms/{uuid}/reactivate/**

Re-enables an archived room. Sets the room back to `active` state and triggers a member sync. Returns `202 Accepted`.

**POST /api/matrix/rooms/{uuid}/join/** and **POST /api/matrix/rooms/{uuid}/leave/**

Lets a staff or support user join or leave a room they are not a project member of (e.g. to moderate). On join, the user is added with a moderator power level (50). Returns `202 Accepted`.

**POST /api/matrix/rooms/{uuid}/open/**

Returns `{"room_id": ...}` to a member whose membership is `invited` or `joined`, and accepts a pending invite on the way; if accepting fails, the room ID is still returned and the invite stays pending. Everyone else who can see the room — staff, support, a customer owner who is not a project member — gets `403`: managing a room does not grant reading its conversation. Callers who cannot see the room get `404`, a room that is not active gets `409`, and with Matrix chat disabled the action answers `400`. The chat drawer calls this when it opens a room.

**DELETE /api/matrix/rooms/{uuid}/**

Deletes a room record. Only rooms in `error`, `creating`, or `archived` state can be deleted. Returns `204 No Content` or `409 Conflict`.

**GET /api/matrix/rooms/{uuid}/members/**

Lists room members with their user UUID, full name, Matrix user ID, power level, and membership state. Paginated.

## Member Sync

When a room is created or a manual sync is triggered:

1. All active project members (direct and via customer) are enumerated
2. Each user is provisioned on the homeserver if needed (via `MatrixUserProfile`)
3. Display names are set to the user's full name
4. Users are invited to the room, and the invite is accepted on their behalf
5. Power levels are set based on roles:
   - Project admin or customer owner: power level 50
   - Regular member: power level 0
   - The bot account: power level 100

Member records are stored in `MatrixRoomMember` with membership states: `invited`, `joined`, `left`, `banned`.

Joins and leaves act as the user through the appservice token with `?user_id=`. They never log in as the user, so they create no Matrix device or access token.

### Automatic member management

The integration automatically responds to role changes:

- **Role granted** — user is invited to the room and a notification is posted
- **Role revoked** — if the user has no remaining roles in the project or its customer, they are kicked from the room; a notification is posted regardless
- **Project deleted** — the room is disabled (members kicked, history exported, room archived)
- **Order state changed** — notifications are posted when orders are approved, completed, rejected, canceled, or errored
- **User deactivated or deleted** — every `WALDUR_WEB_` device of the user is signed out by a background task, which revokes those devices' access and refresh tokens. An open chat drawer then asks Waldur for a new session, gets none, and the user is returned to the login page. Devices are signed out even while chat is switched off (`MATRIX_ENABLED`): switching chat off does not end drawers that are already open, because they renew their tokens with the homeserver directly. A user reactivated before the task runs keeps their new sessions. Other Matrix devices, such as Element, are left alone; in `password` mode the derived password keeps working, so deactivate the account on the homeserver to cut external access

## History Exports

### Export triggers

| Type | Trigger |
| --- | --- |
| `manual` | Someone who manages the room clicks "Export history" on it |
| `periodic` | Celery beat task runs for all active rooms (when `MATRIX_HISTORY_EXPORT_ENABLED` is on) |
| `on_deletion` | Automatic export before archiving a room |

### Export process

1. Messages are fetched from the homeserver via pagination
2. If `MATRIX_EXPORT_MEDIA` is enabled, media files are downloaded and packaged into a ZIP archive
3. Output: JSON file with messages and metadata, optionally a media ZIP

### Export states

| State | Description |
| --- | --- |
| `pending` | Export has been scheduled |
| `exporting` | Messages are being fetched |
| `completed` | Export finished successfully |
| `failed` | Export failed — see `error_message` |

### Who can download exports

An export is the room's whole history, media included, so it goes to whoever manages the room and
no further. Listing exports and downloading their files is open to the same people who can trigger
an export:

- those holding `MATRIX_ROOM.CREATE` on the project or its organization, by default organization
  owners;
- staff and support.

Everyone else gets `404`, room members included: they read the room itself. Access follows the
role, so it ends when the role does, and it stays with the same people after the room is archived.

A personal access token reaches only the exports within its bindings. It also has to carry
`MATRIX_ROOM.CREATE`, or for staff and support the staff or support scope, as it does to trigger
an export.

### Retention

A daily task deletes exports older than `MATRIX_HISTORY_EXPORT_RETENTION_DAYS` (default 90), files
included. The newest completed export of each room is kept whatever its age, so the final export of
an archived room survives. An export still pending or running after that long has stalled, and is
deleted with the rest.

Exports keep messages that were later redacted in the room; retention is what removes them from older
exports. Set it to `0` or less to keep exports forever. Deleting an export, by retention, `disable`
with `delete_history`, or with its room, also deletes its files.

When upgrading, the first nightly run deletes every export older than 90 days except each room's
newest completed one. To keep them, set `MATRIX_HISTORY_EXPORT_RETENTION_DAYS` before upgrading.

There is no action to delete a single export. Besides retention, exports are deleted only when
staff disable a room with `delete_history` or delete the room.

### Listing exports

**GET /api/matrix/exports/**

Returns the exports the user can download (see above).

**Query parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `room_uuid` | UUID | Filter exports by room |
| `state` | string | Filter by export state |
| `export_type` | string | Filter by type: `manual`, `periodic`, `on_deletion` |

## Web Chat Sessions

**POST /api/matrix/session/**

Starts a Matrix session for Waldur's chat drawer. Waldur signs the user in through the appservice on a new device, `WALDUR_WEB_` followed by 12 hexadecimal characters and named "<site name> web chat", asking for a refresh token. It returns the tokens without storing them:

| Field | Description |
| --- | --- |
| `homeserver_url` | Public homeserver URL (`MATRIX_HOMESERVER_PUBLIC_URL`, falling back to `MATRIX_HOMESERVER_URL`) |
| `matrix_user_id` | The user's Matrix ID |
| `device_id` | The session's device |
| `access_token` | Expires after the homeserver's `access_token_ttl` |
| `refresh_token` | Renews the access token through Matrix `/refresh`; idle-expires after `refresh_token_ttl`. `null` if the homeserver issues no refresh tokens, and then the access token does not expire |
| `expires_in_ms` | Lifetime of `access_token`; `null` without refresh tokens |

| Status | When |
| --- | --- |
| `200` | Session started |
| `403` | The account was deactivated while the session was being started; the new device is signed out again |
| `404` | Matrix chat is disabled |
| `429` | The per-user `matrix_session` rate limit (default 120/hour) is exhausted |
| `503` | The homeserver failed or Matrix is misconfigured; the details are logged, not returned |

The drawer keeps both tokens in memory only. It renews the access token through Matrix `/refresh`, and asks Waldur for a new session when a refresh is rejected or when the homeserver signs its device out, for example from Element's session list or through the device cleanup described below. Waldur is therefore consulted when the drawer connects, when a refresh is rejected and when the device is signed out, not while refreshes succeed: switching chat off or signing out of Waldur in another tab does not cut an open drawer, which keeps working until it next needs a new session. Whether the user may still chat is decided then: a deactivated, deleted or signed-out user gets no new session and is returned to the login page. The drawer shows that the chat session has ended when chat was switched off, or when the new session is signed out again within a minute. A rate-limited connect asks the user to try again later.

Every session has its own device. Tuwunel keeps one refresh token per device, so two browser tabs sharing a device would invalidate each other's refresh token. A session starts on every page load for a room member, because the drawer connects in the background for unread counts, so each load is one device and one call against the rate limit. After each new session Waldur signs out the user's web devices not seen for 24 hours, a window fixed in Waldur that matches the default `refresh_token_ttl`. Beyond the 10 most recently seen devices it signs out the rest, except devices seen in the last ten minutes: those belong to open tabs. It never signs out the session that triggered the cleanup.

The homeserver sets the lifetimes. The Helm chart and docker-compose configure Tuwunel with `access_token_ttl = 300` (5 minutes) and `refresh_token_ttl = 86400` (24 hours, idle); another homeserver needs its own equivalent settings. Logins without a refresh token, such as Element with a password, keep non-expiring tokens and are unaffected.

## External Clients

`MATRIX_EXTERNAL_LOGIN_METHOD` decides whether users can open their rooms in Element or another Matrix client:

| Method | What the user gets |
| --- | --- |
| `none` (default) | Waldur offers no external sign-in: "Open in external Matrix client" and "Connect to Matrix…" are hidden. This hides the password; it does not disable password login on the homeserver |
| `password` | The room, the homeserver, their Matrix user ID and a password Waldur derives for them. Needs `MATRIX_USER_REGISTRATION_SECRET` |
| `oidc` | The room, the homeserver and an instruction to sign in with single sign-on, which must be configured on the homeserver |

Switching away from `password` does not revoke passwords users have already seen or sign out their external clients; reset those passwords on the homeserver to cut that access.

**GET /api/matrix/credentials/**

Returns what the external client dialog shows. If the user has not been provisioned on the homeserver yet, provisioning happens on demand.

| Method | Response fields |
| --- | --- |
| `none` | `method`, `matrix_user_id`, `homeserver_url` |
| `password` | `method`, `matrix_user_id`, `homeserver_url`, `password` |
| `oidc` | `method`, `matrix_user_id`, `homeserver_url` |

The endpoint never returns an access token. Waldur's chat drawer gets its tokens from `POST /api/matrix/session/`. It answers `400` with the reason when Matrix is misconfigured for the chosen method, and `503` when provisioning the user on the homeserver fails; the details are logged, not returned.

## Webhook

**PUT /_matrix/app/v1/transactions/{txnId}**

The homeserver sends room events to this endpoint. Authentication is via the `hs_token` in the `Authorization: Bearer` header.

The endpoint is idempotent — duplicate transaction IDs are ignored. Events are dispatched to a Celery task for asynchronous processing.

### Bot commands

The bot responds to commands posted in project chat rooms:

| Command | Description |
| --- | --- |
| `!help` | Show available commands |
| `!status` | Resource status summary for the linked project |
| `!orders` | Last 5 orders for the project |
| `!members` | List room members with their project roles |

## Configuration Reference

These Constance settings control the integration:

| Setting | Default | Description |
| --- | --- | --- |
| `MATRIX_ENABLED` | `False` | Enable Matrix chat integration |
| `MATRIX_HOMESERVER_URL` | `""` | Homeserver URL (e.g., `https://matrix.example.com`) |
| `MATRIX_HOMESERVER_DOMAIN` | `""` | Homeserver domain for user IDs (e.g., `matrix.example.com`) |
| `MATRIX_APPSERVICE_AS_TOKEN` | `""` | Token Waldur uses to authenticate with the homeserver |
| `MATRIX_APPSERVICE_HS_TOKEN` | `""` | Token the homeserver uses to authenticate with Waldur |
| `MATRIX_APPSERVICE_SENDER_LOCALPART` | `waldur-bot` | Bot user localpart |
| `MATRIX_HISTORY_EXPORT_ENABLED` | `False` | Enable periodic and on-deletion exports |
| `MATRIX_EXPORT_MEDIA` | `False` | Download media files during export |
| `MATRIX_HISTORY_EXPORT_RETENTION_DAYS` | `90` | Days to keep history exports, files included; each room's newest completed export is kept; `0` or less keeps them forever |
| `MATRIX_USER_REGISTRATION_SECRET` | `""` | Shared secret for registering users on the homeserver |
| `MATRIX_USER_ID_FORMAT` | `username` | Format for generating Matrix user IDs: `username`, `uuid`, or `email_local` |
| `MATRIX_EXTERNAL_LOGIN_METHOD` | `none` | How users sign in to an external Matrix client: `none`, `password`, or `oidc`. See [External clients](#external-clients) |

## Feature Flag

The Matrix chat UI is gated on the project feature flag `project.show_matrix_chat` ("Enable Matrix chat integration for projects"). When disabled, all Matrix-related UI elements are hidden — the admin route, the project **Communication** and **Chat** tabs, the dashboard "Team chat" button, and background auto-connect. Note the flag only controls UI visibility; the API endpoints are guarded by their own permission checks.

## Data Model

| Model | Description |
| --- | --- |
| `MatrixUserProfile` | Links a Waldur user to their Matrix user ID and tracks provisioning state. It stores no Matrix token. |
| `MatrixRoom` | A Matrix room linked to a project via generic FK. One room per project. Manages state via FSM transitions. |
| `MatrixRoomMember` | Tracks room membership, power levels, and membership state per user. |
| `MatrixHistoryExport` | A chat history export with state, message/media counts, and file references. |
| `MatrixAppserviceTransaction` | Idempotency record for processed webhook transactions. |
