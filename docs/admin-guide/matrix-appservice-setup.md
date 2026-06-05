# Matrix Chat Integration

## Overview

Waldur integrates with the [Matrix](https://matrix.org/) open communication protocol as an Application Service (appservice). This enables:

- **Project chat rooms** — each project can have a dedicated Matrix room
- **Automatic member sync** — project members are invited to rooms based on their roles
- **Bot commands** — operational queries (resource status, orders, members) from within the chat
- **History export** — manual, scheduled, or on-deletion export of chat messages and media
- **User credentials** — authenticated users can retrieve their Matrix login details
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

**Example response (202):**

```json
{
  "rooms_reprovisioned": 5,
  "users_reset": 42
}
```

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

Permissions vary per action (demo policy: owners manage day-to-day membership and exports; room lifecycle is staff-only):

| Action | Required permission |
| --- | --- |
| `sync_members`, `export_history`, `retry`, `reactivate` | Customer owner (`is_owner`) |
| `disable`, `DELETE`, `join`, `leave` | Staff or support (`is_staff_or_support`) |

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

**DELETE /api/matrix/rooms/{uuid}/**

Deletes a room record. Only rooms in `error`, `creating`, or `archived` state can be deleted. Returns `204 No Content` or `409 Conflict`.

**GET /api/matrix/rooms/{uuid}/members/**

Lists room members with their user UUID, full name, Matrix user ID, power level, and membership state. Paginated.

## Member Sync

When a room is created or a manual sync is triggered:

1. All active project members (direct and via customer) are enumerated
2. Each user is provisioned on the homeserver if needed (via `MatrixUserProfile`)
3. Display names are set to the user's full name
4. Users are invited to the room
5. Power levels are set based on roles:
   - Project admin or customer owner: power level 50
   - Regular member: power level 0
   - The bot account: power level 100

Member records are stored in `MatrixRoomMember` with membership states: `invited`, `joined`, `left`, `banned`.

### Automatic member management

The integration automatically responds to role changes:

- **Role granted** — user is invited to the room and a notification is posted
- **Role revoked** — if the user has no remaining roles in the project or its customer, they are kicked from the room; a notification is posted regardless
- **Project deleted** — the room is disabled (members kicked, history exported, room archived)
- **Order state changed** — notifications are posted when orders are approved, completed, rejected, canceled, or errored

## History Exports

### Export triggers

| Type | Trigger |
| --- | --- |
| `manual` | Owner clicks "Export history" on a room |
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

### Listing exports

**GET /api/matrix/exports/**

Returns exports accessible to the user based on room access. Staff and support see all exports.

**Query parameters:**

| Parameter | Type | Description |
| --- | --- | --- |
| `room_uuid` | UUID | Filter exports by room |
| `state` | string | Filter by export state |
| `export_type` | string | Filter by type: `manual`, `periodic`, `on_deletion` |

## User Credentials

**GET /api/matrix/credentials/**

Returns Matrix login credentials for the authenticated user. If the user has not been provisioned on the homeserver yet, provisioning happens on-demand.

The response varies based on the configured `MATRIX_LOGIN_METHOD`:

| Method | Response fields |
| --- | --- |
| `password` | `matrix_user_id`, `homeserver_url`, `password` |
| `token` | `matrix_user_id`, `homeserver_url`, `login_token` |
| `oidc` | `matrix_user_id`, `homeserver_url`, `oidc_provider_url` |

**Optional query parameter:**

| Parameter | Type | Description |
| --- | --- | --- |
| `room_uuid` | UUID | Auto-invite and join the user into this room. The user must have access to the room's project. Returns additional `room_id` and `access_token` fields for embedded chat. |

When `room_uuid` is provided:

- The user is invited to the Matrix room and automatically joined
- An `access_token` is returned for direct homeserver communication (embedded chat)
- If the user does not have access to the room's project, the room parameters are silently ignored

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
| `MATRIX_USER_REGISTRATION_SECRET` | `""` | Shared secret for registering users on the homeserver |
| `MATRIX_USER_ID_FORMAT` | `username` | Format for generating Matrix user IDs: `username`, `uuid`, or `email_local` |
| `MATRIX_LOGIN_METHOD` | `token` | User login method: `password`, `token`, or `oidc` |
| `MATRIX_OIDC_PROVIDER_URL` | `""` | OIDC provider URL (fallback when multiple active IDPs exist) |

## Feature Flag

The Matrix chat UI is gated on the project feature flag `project.show_matrix_chat` ("Enable Matrix chat integration for projects"). When disabled, all Matrix-related UI elements are hidden — the admin route, the project **Communication** and **Chat** tabs, the dashboard "Team chat" button, and background auto-connect. Note the flag only controls UI visibility; the API endpoints are guarded by their own permission checks.

## Data Model

| Model | Description |
| --- | --- |
| `MatrixUserProfile` | Links a Waldur user to their Matrix user ID. Tracks provisioning state and access token. |
| `MatrixRoom` | A Matrix room linked to a project via generic FK. One room per project. Manages state via FSM transitions. |
| `MatrixRoomMember` | Tracks room membership, power levels, and membership state per user. |
| `MatrixHistoryExport` | A chat history export with state, message/media counts, and file references. |
| `MatrixAppserviceTransaction` | Idempotency record for processed webhook transactions. |
