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

Rooms, member sync, bot commands, exports and the chat drawer need a homeserver with the Application Service API, and short-lived drawer sessions also need refresh tokens. Locking the accounts of deactivated and deleted users, generated passwords and the refusal of homeserver-admin accounts also need the Synapse admin API (`/_synapse/admin`), with the bot as a homeserver admin; Synapse and Tuwunel have it. The Helm chart and docker-compose ship Tuwunel.

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
3. Homeserver reachable (`homeserver_reachable`, via `/_matrix/client/versions`; also names the homeserver software and version, from `/_matrix/federation/v1/version` or, with federation off, Tuwunel's `/_tuwunel/server_version`)
4. Public homeserver URL configured (`public_homeserver_configured`: the URL browsers use, `MATRIX_HOMESERVER_PUBLIC_URL` or else `MATRIX_HOMESERVER_URL`)
5. Public homeserver reachable (`public_homeserver_reachable`, via `/_matrix/client/versions` from Waldur; only probed when the public URL differs from the internal one)
6. AS token configured (`as_token_configured`)
7. HS token configured (`hs_token_configured`)
8. Registration secret configured (`registration_secret_configured`)
9. Bot authentication (`bot_whoami`, via `/account/whoami`; also how many rooms the bot is in)
10. Appservice can act for users (`appservice_user_namespace`: `/account/whoami` as the staff user's Matrix ID. It fails when the homeserver's appservice registration does not cover users, which breaks chat sessions and room joins; register the appservice again with Waldur's registration. When it fails, it also counts room members recorded as invited, not joined)
11. Chat drawer tokens expire (`web_token_lifetime`: signs the staff user in on a test device and reads the token's lifetime; fails when tokens never expire or live over an hour. Skipped until the staff user has opened the chat once)
12. Bot is a homeserver admin (`bot_homeserver_admin`; see [Making the bot a homeserver admin](#making-the-bot-a-homeserver-admin))
13. LiveKit configured (`livekit_configured`: a LiveKit focus in the homeserver's `/.well-known/matrix/client`; only calls need it)
14. Room statistics (`room_stats`: active, creating, errored counts)
15. User profile statistics (`user_stats`: provisioned count, plus the active users whose roles put them in an active project room but who have no Matrix profile, named up to ten. Provisioning refused or failed for those; the worker log says why, and [Existing Matrix accounts](#existing-matrix-accounts) says how to link one. Fails while any room member is unlinked)

**Example response (200):**

```json
{
  "ok": true,
  "checks": [
    {"name": "homeserver_domain_configured", "label": "Homeserver domain configured", "ok": true, "detail": "matrix.example.com"},
    {"name": "homeserver_reachable", "label": "Homeserver reachable", "ok": true, "detail": "OK — Tuwunel 1.9.0, Matrix up to v1.19"},
    {"name": "bot_whoami", "label": "Bot authentication (whoami)", "ok": true, "detail": "OK — authenticated as @waldur-bot:matrix.example.com, in 3 room(s)"}
  ]
}
```

#### POST /api/admin/matrix/reprovision/

Resets all active rooms to `creating` state and re-queues them for provisioning on the homeserver. Also resets all user provisioning status. Use this when migrating to a new homeserver. Staff only.

Do not run it against the homeserver the rooms already live on. Old rooms are not
deleted, so each one stays behind with its history while Waldur replaces it with
an empty room.

Users keep their Matrix IDs: each one is provisioned again on the new homeserver
under the ID their profile already holds. The new homeserver therefore needs the
same `server_name` as the old one. Under another domain Waldur keeps acting for
the old IDs, and chat fails for every user. To move, point
`MATRIX_HOMESERVER_URL` at the new homeserver, register the appservice there,
make the bot a homeserver admin there (see
[Existing Matrix accounts](#existing-matrix-accounts)), then reprovision.

Reprovisioning takes over an account the new homeserver already has under a
user's ID, since the profile says the ID is theirs. With the bot an admin, it
refuses the homeserver's admins; it cannot tell a self-registered account, so
keep registration closed on the new homeserver until reprovisioning is done.

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
[Room actions](#room-actions). Whoever may create a project's room is also an
admin in it (power level 50), so it makes project managers room admins as well.

If you replaced `CUSTOMER.OWNER` wholesale in `custom-roles.yaml`, add
`MATRIX_ROOM.CREATE` to that list yourself, or owners lose room creation.

The same permission on an organization also puts its holders in every room of
the organization's projects (see [Member Sync](#member-sync)). Taking it away
from organization owners therefore takes them out of those rooms as well, and
giving it to another organization role brings that role's holders in. Both
happen by themselves: three minutes after the role's permissions change, Waldur
syncs every active room the role has holders in, which also moves their power
level. The wait covers deployment, which loads the role files one after another
and so briefly takes away a permission that a later file gives back. Such a
role, for example the `PROJECT.MANAGER` override above, therefore has its rooms
synced once on every deployment, without changing anything in them. The room
list follows the new permissions at once. A grant on a single project only
changes the power level: project roles already put their holders in that
project's room.

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

Rooms are created unfederated (`m.federate: false`): only accounts on this homeserver can join them, whatever its federation settings. The packaged homeservers keep federation on for LiveKit's OpenID check, which does not open these rooms.

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

1. Everyone with an active project role is enumerated, plus everyone whose customer role may create the customer's chat rooms (`MATRIX_ROOM.CREATE`, held by customer owners by default). Other customer roles, such as organization support and reader, are left out
2. Each user is provisioned on the homeserver if needed (via `MatrixUserProfile`). A user whose Matrix ID already belongs to an account Waldur did not create is skipped; see [Existing Matrix accounts](#existing-matrix-accounts)
3. Display names are set to the user's full name
4. Users are invited to the room, and the invite is accepted on their behalf
5. Power levels are set based on roles:
   - Project admin, or anyone who may create the project's room (`MATRIX_ROOM.CREATE` on the project or its organization, held by organization owners by default): power level 50, shown as Admin in the chat
   - Regular member: power level 0
   - The bot account: power level 100

   A level the user's roles no longer give is lowered again. Staff and support who joined with the Join action keep power level 50 whatever their roles.

Member records are stored in `MatrixRoomMember` with membership states: `invited`, `joined`, `left`, `banned`.

The same rule decides who else sees a room. A user sees a project's room in the room list, and reaches its members, media and history exports or answers to bot commands, only if a role would put them in it in step 1; each action may ask for more on top. Organization support and readers therefore do not see the rooms of the organization's projects. Staff and support users list every room.

Joins and leaves act as the user through the appservice token with `?user_id=`. They never log in as the user, so they create no Matrix device or access token.

### Existing Matrix accounts

Waldur only provisions Matrix accounts it creates. The appservice can act as any
account in its namespace, so taking over an existing one would sign the user in
as its owner. When a user without a Matrix profile derives an ID that already
has an account on the homeserver, such as a self-registered account or one made
by hand, provisioning refuses it: their chat answers "Chat is unavailable right
now", member sync skips them, and the API or worker log says the ID "already belongs to
an account this Waldur did not create". Diagnostics lists the room members left
without a profile in its "User profiles" row.

If the account belongs to the user, link it:

```bash
waldur link_matrix_account <username> @<localpart>:<homeserver domain>
```

After a database reset, or a restore of an older dump, against the same
homeserver, every user provisioned since has an account but no profile. Link
them in one go:

```bash
waldur link_matrix_account --all
```

`--all` links each user without a profile to the existing account with their
derived ID. It skips users with no account, homeserver admins, IDs another user
already holds, and IDs that several users without a profile derive, since
nothing tells whose that account is. It names each skipped user; link the owner
by hand. Run it only when every such account belongs to this Waldur's users.
Back up the homeserver together with Waldur's database, so a restore brings
both back to the same point.

Different users can derive the same ID: under `email_local` (`alice@a.org` and
`alice@b.org`), and when usernames differ only in case or in characters Matrix
does not allow, which become `_` (`a@b` and `a_b`). The first one provisioned
gets the account. The second gets no chat, and the log names the user who holds
it; linking cannot help, since an ID is linked to one user only. A user deleted
and recreated under the same username finds their old account, which outlives
the deletion, and needs `link_matrix_account`. Once the bot is a homeserver
admin, the deletion has also locked that account and replaced its password, so
after linking either deactivate and reactivate the user, or unlock the account
on the homeserver and run a member sync of their rooms (see
[Automatic member management](#automatic-member-management)).

No Waldur user may hold a homeserver admin's account, since its sessions could
run admin commands. The homeserver admins are the accounts you administer it
with, plus the bot once you make it one. Waldur can only tell who they are once
the bot is a homeserver admin itself: on Tuwunel, send
`!admin users make-user-admin @waldur-bot:<homeserver domain>` in `#admins`.
That also makes the appservice token an admin credential. From then on Waldur
refuses web chat sessions, reprovisioning and links for any user whose account
is an admin, sets no password on such an account and does not lock it. Until
then nothing is refused, and `link_matrix_account` warns that it could not
check.

### Automatic member management

The integration automatically responds to role changes:

- **Role granted** — a project role invites the user to the project's room and posts a notification; a customer role that passes the member sync rule above invites the user to every active room of the customer's projects, without a notification
- **Project role revoked** — a notification is posted, and the user is kicked from the room unless a remaining role still puts them in it (see [Member Sync](#member-sync))
- **Customer role revoked** — the user is kicked, without a notification, from each active room of the customer's projects that they are in and no remaining role puts them in
- **Staff or support status lost** — staff and support keep a room they joined with the Join action only while they are active staff or support; afterwards they are kicked from it, unless a role puts them in it
- **Role permissions changed** — when a role gains or loses `MATRIX_ROOM.CREATE`, through the role API or the role files loaded at deployment (`permissions.yaml`, `custom-roles.yaml`, `permissions-override.yaml`), every active room of the projects and organizations where the role has holders is synced three minutes later (see [Member Sync](#member-sync))
- **Project deleted** — the room is disabled (members kicked, history exported, room archived)
- **Order state changed** — notifications are posted when orders are approved, completed, rejected, canceled, or errored
- **User deactivated or deleted** — a background task signs out every Matrix device of the user, Element included, removes them from all their rooms, and then locks their Matrix account. A locked account refuses its tokens and every new sign-in, by password or single sign-on, so the user cannot return through an external client while the IdP still accepts them. An open chat drawer then asks Waldur for a new session, gets none, and the user is returned to the login page. This runs even while chat is switched off (`MATRIX_ENABLED`), because open drawers renew their tokens with the homeserver directly. A user reactivated before the task runs keeps access. Reactivation unlocks the account and then invites the user back to every room a role puts them in. A deleted user's account stays locked, and its password is replaced with one nobody knows. Locking needs the bot to be a homeserver admin (see [Making the bot a homeserver admin](#making-the-bot-a-homeserver-admin)). Until it is, the account stays usable: the user can still sign in to an external client but finds no rooms there, and with `oidc` has to be disabled at the IdP as well. Making the bot an admin later does not lock the accounts of users who were deactivated earlier

Kicks that fail are retried for several minutes, and each retry checks again whether the user still has access. A member whose kick never succeeds stays recorded as a member. A sign-out that fails is retried the same way while the account could not be locked. Once it is locked, the devices left signed in are refused like any other, but work again if the user is reactivated.

Only a reactivation unlocks an account, and it unlocks it whoever locked it. If its unlock fails for longer than the retries last, or the admin API is not usable then, the account stays locked: the user's chat answers "Your chat account is locked; ask your administrator." until staff deactivates and reactivates the user, or an admin unlocks the account on the homeserver (`PUT /_synapse/admin/v2/users/<Matrix ID>` with `{"locked": false}`) and then runs a member sync of the user's rooms, which only a reactivation starts by itself. A new user who gets the Matrix ID of a deleted one is refused before that, like anyone whose ID has an account Waldur did not create for them (see [Existing Matrix accounts](#existing-matrix-accounts)). Linked deliberately, it is still the deleted user's account, with their direct messages and other rooms, and it stays locked until staff or an admin does one of those two.

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
| `recovery_key` | The user's encryption recovery key (see [Encryption keys](#encryption-keys)); `null` until encryption is set up. Only ever returned to its owner |

| Status | When |
| --- | --- |
| `200` | Session started |
| `403` | The account was deactivated while the session was being started; the new device is signed out again |
| `404` | Matrix chat is disabled |
| `429` | The per-user `matrix_session` rate limit (default 120/hour) is exhausted |
| `503` | "Your chat account is locked; ask your administrator." when the user's Matrix account is locked (see [Automatic member management](#automatic-member-management)), otherwise the homeserver failed or Matrix is misconfigured; the details are logged, not returned |

The drawer keeps both tokens in memory only. It renews the access token through Matrix `/refresh`, and asks Waldur for a new session when a refresh is rejected or when the homeserver signs its device out, for example from Element's session list or through the device cleanup described below. Waldur is therefore consulted when the drawer connects, when a refresh is rejected and when the device is signed out, not while refreshes succeed: switching chat off or signing out of Waldur in another tab does not cut an open drawer, which keeps working until it next needs a new session. Whether the user may still chat is decided then: a deactivated, deleted or signed-out user gets no new session and is returned to the login page. The drawer shows that the chat session has ended when chat was switched off, or when the new session is signed out again within a minute. A rate-limited connect asks the user to try again later.

Every session has its own device. Tuwunel keeps one refresh token per device, so two browser tabs sharing a device would invalidate each other's refresh token. A session starts on every page load for a room member, because the drawer connects in the background for unread counts, so each load is one device and one call against the rate limit. After each new session Waldur signs out the user's web devices not seen for 24 hours, a window fixed in Waldur that matches the default `refresh_token_ttl`. Beyond the 10 most recently seen devices it signs out the rest, except devices seen in the last ten minutes: those belong to open tabs. It never signs out the session that triggered the cleanup.

The homeserver sets the lifetimes. The Helm chart and docker-compose configure Tuwunel with `access_token_ttl = 300` (5 minutes) and `refresh_token_ttl = 86400` (24 hours, idle); another homeserver needs its own equivalent settings. Logins without a refresh token, such as Element with a password, keep non-expiring tokens and are unaffected.

## Encryption keys

Chat is end-to-end encrypted. Each user's encryption identity (cross-signing keys,
key backup and the device that receives messages while no drawer is open) is
unlocked by a **recovery key** that Waldur holds for them: the homeserver only ever
stores encrypted keys, and the recovery key that unlocks them is held by your
Waldur deployment. It is stored encrypted under `FIELD_ENCRYPTION_KEY` (see
`docs/field-encryption.md`) and returned only in the user's own web chat session.

**Calls are encrypted in transit only.** Audio, video and screen sharing go
through LiveKit over DTLS-SRTP, so they are encrypted between each browser and
the LiveKit server, but the server handles them in clear: whoever operates LiveKit
can see and hear a call. The room's end-to-end encryption covers its messages,
not its calls. Run LiveKit on infrastructure you trust as much as Waldur itself.

The drawer sets encryption up on a user's first session. Only one browser may do
so at a time, and Waldur must hold the recovery key before any key is uploaded:

**POST /api/matrix/crypto/lease/** with `{"kind": "bootstrap"}` grants a lease
for ten minutes. **POST /api/matrix/crypto/escrow/** with `{"lease": ...,
"recovery_key": ...}` stores the key, from the lease holder only; the lease is
kept while the drawer uploads the keys that go with it. **POST
/api/matrix/crypto/lease/release/** with `{"lease": ...}` ends it when the setup
is done or has failed.

These endpoints, and the recovery key in the session response, accept only the
ways the web UI authenticates: the user's Waldur API token or a session.
Personal access tokens, OIDC access tokens and staff impersonating a user are
refused (`403`) and get no key. The user's API token itself is not limited to
the web UI (it can be copied into scripts), so treat it as giving access to the
user's chat encryption too. Escrowing a key and starting a reset are recorded in
the user's event log.

| Status | When |
| --- | --- |
| `200` / `204` | Lease granted / key stored |
| `400` | The recovery key is not a valid Matrix recovery key |
| `409` | `state` says why: `set_up` (already done), `locked` (the homeserver has an identity Waldur holds no key for), `in_progress` (another window holds the lease; see `Retry-After`), `no_lease` (the lease expired or another window took over), `not_locked` (a reset was asked for but isn't needed) |
| `429` | The per-user `matrix_crypto` rate limit (default 30/hour) is exhausted |
| `503` | The homeserver could not be asked, or the bot is not a homeserver admin |

### Locked identities

Tuwunel accepts a user's first cross-signing keys without a password but refuses
to replace them unless the user answers a password prompt, which Waldur's users
can't. An identity is **locked** when the homeserver has cross-signing keys for
the user and the key Waldur holds is missing or no longer opens their secret
storage. That happens when:

- the Waldur database is restored to a point before the user's first setup while
  the homeserver keeps newer data, or Waldur's profiles are relinked with
  `link_matrix_account`;
- `FIELD_ENCRYPTION_KEY` is lost, or `SECRET_KEY` is rotated while it is unset;
- the user set encryption up in another client with a key Waldur never saw.

A lease with `{"kind": "reset"}` recovers it. Waldur checks that the identity is
really locked, sets a temporary Matrix password through the admin API (the bot
must be a homeserver admin), and returns it once so the drawer can answer the
password prompt. The password is replaced with a discarded random one as soon as
the new recovery key is escrowed, or when the lease runs out, and a sweep every
ten minutes replaces any left behind. A reset replaces
the user's identity and deletes their old key backups, and in password mode it
also replaces any Matrix password the user generated for an external client.

Back up the homeserver and the Waldur database **together**, and keep
`FIELD_ENCRYPTION_KEY` safe: without it, no escrowed recovery key can be read.

## External Clients

`MATRIX_EXTERNAL_LOGIN_METHOD` decides whether users can open their rooms in Element or another Matrix client:

| Method | What the user gets |
| --- | --- |
| `none` (default) | Waldur offers no external sign-in: "Open in external Matrix client" and "Connect to Matrix…" are hidden. Users cannot generate a password, but this does not disable password login on the homeserver: a password generated earlier keeps working there |
| `password` | The room, the homeserver, their Matrix user ID and a password they generate in Waldur; see [Generated passwords](#generated-passwords). For testing and sites without an identity provider; needs the bot to be a homeserver admin |
| `oidc` | The room, the homeserver and an instruction to sign in with single sign-on, which must be configured on the homeserver |

Switching away from `password` does not revoke passwords users have already seen or sign out their external clients; to refuse password logins, set `login_with_password = false` on the homeserver.

**GET /api/matrix/credentials/**

Returns what the external client dialog shows. If the user has not been provisioned on the homeserver yet, provisioning happens on demand.

| Method | Response fields |
| --- | --- |
| `none` | `method`, `matrix_user_id`, `homeserver_url` |
| `password` | `method`, `matrix_user_id`, `homeserver_url` |
| `oidc` | `method`, `matrix_user_id`, `homeserver_url` |

The endpoint never returns a password or an access token. Waldur's chat drawer gets its tokens from `POST /api/matrix/session/`. It answers `400` with the reason when Matrix is misconfigured for the chosen method, and `503` when provisioning the user on the homeserver fails; the details are logged, not returned.

### Generated passwords

Waldur registers every account with a random password that it does not keep, so nobody knows it; Tuwunel treats an account without a password as deactivated and refuses single sign-on into it. In `password` mode a user replaces it with one they can see:

**POST /api/matrix/credentials/password/**

It sets a random password on the user's Matrix account through the homeserver's admin API and returns it with `matrix_user_id` and `homeserver_url`. The password is not stored, so it is shown only in this response (sent with `Cache-Control: no-store`). Generating again replaces it; clients already signed in stay signed in. Each one is recorded in the event log (`matrix_password_generated`), without the password. Deactivating the user locks the account, which refuses the password too; after a reactivation it works again. Deleting the user also replaces the password with a random one and signs out every device through the admin API. A password generated while the account is locked is refused like any other until the account is unlocked.

| Status | When |
| --- | --- |
| `200` | Password set |
| `403` | The user was deactivated meanwhile; the account is locked |
| `404` | Matrix chat is disabled |
| `409` | The login method is not `password` |
| `429` | The per-user `matrix_password` rate limit (default 30/hour) is exhausted |
| `503` | "Matrix passwords are not available yet; ask your administrator." when the bot is not a homeserver admin, the admin API is not reachable or the account is a homeserver admin's, otherwise "Chat is unavailable right now. Please try again later."; the details are logged |

### Making the bot a homeserver admin

Locking the Matrix account of a deactivated or deleted user, generating passwords, and refusing Waldur users whose Matrix account is a homeserver admin (see [Existing Matrix accounts](#existing-matrix-accounts)) go through the homeserver's admin API (`/_synapse/admin`), which answers only homeserver admins. Every login method needs the first of these. Until the bot is an admin, or while the admin API is not reachable, a deactivated user's account stays unlocked, a deleted user's password is not replaced, generating a password answers `503`, and admin accounts are not refused.

- **Tuwunel:** as an admin, send `!admin users make-user-admin @waldur-bot:<homeserver domain>` in `#admins`.
- **Synapse:** set the bot's admin flag, for example with `PUT /_synapse/admin/v2/users/@waldur-bot:<homeserver domain>` and `{"admin": true}`, using an admin's access token.

Use your bot's localpart if it is not `waldur-bot`. The appservice token (`MATRIX_APPSERVICE_AS_TOKEN`) already acts as every local user of the homeserver; as an admin's token it carries server-wide powers on top, such as setting any account's password, so protect it accordingly. Waldur calls the admin API at `MATRIX_HOMESERVER_URL`; if a proxy blocks `/_synapse/admin` there, point it at an internal address. The diagnostics check "Bot is a homeserver admin" (`bot_homeserver_admin`) shows whether the bot is an admin.

## Calls

Calls run on LiveKit. Waldur issues the LiveKit tokens that Matrix clients (Waldur's chat drawer and Element Call) need to join a call, in place of lk-jwt-service. Point the homeserver's `.well-known/matrix/client` LiveKit focus at it: `livekit_service_url` is `https://<waldur-api>/api/matrix/livekit`.

| Endpoint | Request |
| --- | --- |
| `POST /api/matrix/livekit/get_token` | `{room_id, slot_id, openid_token, member: {id, claimed_user_id, claimed_device_id}}` |
| `POST /api/matrix/livekit/sfu/get` | `{room, openid_token, device_id}` (the older form; Element Call falls back to it) |

Both answer `{url, jwt}`. The Matrix OpenID token authenticates the caller, verified at the homeserver (`/_matrix/federation/v1/openid/userinfo` at `MATRIX_HOMESERVER_URL`, so its federation endpoints must be reachable there). Any origin may call these two paths, without credentials.

- **Who gets a token:** a user of this homeserver who is joined to the room at that moment, for one of their own devices. For a room Waldur manages, the room must also be active and the user a Waldur user who still has access to it (a role that puts them in the room, or staff who joined it), so a user whose role was revoked gets no token even before they are removed from the room on the homeserver. Rooms Waldur does not manage, such as direct messages and rooms created in Element, have no Waldur roles: there membership on the homeserver alone decides. Users of other homeservers are refused.
- **What it allows:** joining that room's call, for 3 minutes; LiveKit renews the token of a connected participant.
- **Removal:** when a user loses access to a Waldur room (role revoked, staff leave, deactivation, deletion, room disabled), Waldur also disconnects them from its call. A removed user can reconnect until the token LiveKit last gave them expires, up to about 10 minutes. Leaving or being removed from a room Waldur does not manage disconnects no one; the user cannot get a new token for it.
- **Client address:** the per-address limit keys on the last `X-Forwarded-For` entry, the address the proxy in front of Waldur saw (a port, as Azure Application Gateway adds, is dropped). Behind a further load balancer or CDN that does not pass the real client address on (real-IP or PROXY protocol), every client appears as that balancer and all share one bucket: calls then fail with `429` under load rather than going unlimited.
- **OpenID tokens:** any Matrix OpenID token of a user lets its holder get call tokens for the rooms that user has joined, as with lk-jwt-service. A widget or integration the user hands an OpenID token to can therefore join their calls while the OpenID token is valid.
- **Limits:** `matrix_livekit_token` per client address (default 600/hour) and `matrix_livekit_token_user` per Matrix user (default 120/hour); `MATRIX_LIVEKIT_KEY`, `MATRIX_LIVEKIT_SECRET` and `MATRIX_LIVEKIT_PUBLIC_URL` (the signalling URL browsers connect to) must be set, or the endpoints answer `503`.

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
| `MATRIX_HOMESERVER_URL` | `""` | Homeserver URL (e.g., `https://matrix.example.com`) that Waldur calls, including the admin API under `/_synapse/admin` |
| `MATRIX_HOMESERVER_DOMAIN` | `""` | Homeserver domain for user IDs (e.g., `matrix.example.com`) |
| `MATRIX_APPSERVICE_AS_TOKEN` | `""` | Token Waldur uses to authenticate with the homeserver |
| `MATRIX_APPSERVICE_HS_TOKEN` | `""` | Token the homeserver uses to authenticate with Waldur |
| `MATRIX_APPSERVICE_SENDER_LOCALPART` | `waldur-bot` | Bot user localpart |
| `MATRIX_HISTORY_EXPORT_ENABLED` | `False` | Enable periodic and on-deletion exports |
| `MATRIX_EXPORT_MEDIA` | `False` | Download media files during export |
| `MATRIX_HISTORY_EXPORT_RETENTION_DAYS` | `90` | Days to keep history exports, files included; each room's newest completed export is kept; `0` or less keeps them forever |
| `MATRIX_USER_REGISTRATION_SECRET` | `""` | Shared secret for registering users on the homeserver |
| `MATRIX_USER_ID_FORMAT` | `username` | Format for generating Matrix user IDs: `username`, `uuid`, or `email_local`. Applies only to users provisioned afterwards; existing users keep their Matrix ID. See [Existing Matrix accounts](#existing-matrix-accounts) for IDs two users share |
| `MATRIX_EXTERNAL_LOGIN_METHOD` | `none` | How users sign in to an external Matrix client: `none`, `password`, or `oidc`. See [External clients](#external-clients) |

## Feature Flag

The Matrix chat UI is gated on the project feature flag `project.show_matrix_chat` ("Enable Matrix chat integration for projects"). When disabled, all Matrix-related UI elements are hidden — the admin route, the project **Communication** and **Chat** tabs, the dashboard "Team chat" button, and background auto-connect. Note the flag only controls UI visibility; the API endpoints are guarded by their own permission checks.

## Data Model

| Model | Description |
| --- | --- |
| `MatrixUserProfile` | Links a Waldur user to their Matrix user ID and tracks provisioning state. It stores no Matrix token. |
| `MatrixRoom` | A Matrix room linked to a project via generic FK. One room per project. Manages state via FSM transitions. |
| `MatrixRoomMember` | Tracks room membership, power levels, and membership state per user. `manually_joined` marks staff and support who joined with the Join action. |
| `MatrixHistoryExport` | A chat history export with state, message/media counts, and file references. |
| `MatrixAppserviceTransaction` | Idempotency record for processed webhook transactions. |

## Troubleshooting

The messages below appear in the API and worker logs; the user only sees "Chat is unavailable right now", unless the row says otherwise.

| Log message | Cause | Fix |
| --- | --- | --- |
| `<id> already belongs to an account this Waldur did not create` | The user's derived Matrix ID had an account before Waldur provisioned them | If it is theirs, `waldur link_matrix_account <username> <id>`; after a database reset or restore, `waldur link_matrix_account --all`. Otherwise create another account for the user on the homeserver and link that; deactivating the existing account does not free its ID. See [Existing Matrix accounts](#existing-matrix-accounts) |
| `<id> is already linked to <user>; <username> cannot be linked to it too` | Two users derive the same Matrix ID | The second user gets no chat until what the ID is derived from (username, or email under `email_local`) changes. See [Existing Matrix accounts](#existing-matrix-accounts) |
| `<user> was linked to <id> meanwhile; try again` | `link_matrix_account` linked the user while their chat was being provisioned | None; the next attempt uses the linked account |
| `<id> is a homeserver admin; Waldur does not act as it for a user`, or `does not set the password of <id>`; `Not locking <id>, a homeserver admin`; `Matrix password of deleted <id> not replaced and its account not locked: it is a homeserver admin's` | The user's account is a homeserver admin's. They get no chat session, and generating a password answers "Matrix passwords are not available yet; ask your administrator." Deactivating or deleting the user still signs the account's devices out and removes it from the rooms Waldur manages, but does not lock it or replace its password | Remove the admin flag from the account on the homeserver, or keep the user off chat |
| `The Matrix account of <user> is locked` | A deactivation or deletion locked the account and no reactivation unlocked it: the unlock failed for longer than its retries, or the account is a deleted user's, linked to this one. The user sees "Your chat account is locked; ask your administrator." | Deactivate and reactivate the user, or unlock the account on the homeserver and run a member sync of their rooms. See [Automatic member management](#automatic-member-management) |
