# Personal Access Tokens

## Overview

Personal Access Tokens (PATs) provide named, scoped, time-limited tokens for programmatic API access. They are distinct from the existing `Token` model, which continues to serve UI/session authentication. PATs use the `w_` prefix to distinguish them from OIDC JWT tokens.

## Token Format

Format: `w_<unix_timestamp>_<random>` (e.g., `w_1735689599_Abc123def...`)

- `w_` prefix identifies the token as a Waldur PAT
- `<unix_timestamp>` is the expiration time, visible by inspecting the token
- `<random>` is 256 bits of entropy via `secrets.token_urlsafe(32)`
- SHA-256 hash stored in database; plaintext shown only once at creation
- First 8 characters stored as `token_prefix` for UI identification

The embedded timestamp allows humans and scripts to check expiry without an API call. The server still validates against the database on every request.

## Authentication

PATs use the `Authorization: Bearer` header:

```text
Authorization: Bearer w_1735689599_<random>
```

The `PATAuthentication` class in `waldur_core.core.authentication` handles PAT requests. It is registered in `DEFAULT_AUTHENTICATION_CLASSES` between `SessionAuthentication` and `OIDCAuthentication`:

```python
"DEFAULT_AUTHENTICATION_CLASSES": (
    "waldur_core.core.authentication.ImpersonationAuthentication",
    "waldur_core.core.authentication.SessionAuthentication",
    "waldur_core.core.authentication.PATAuthentication",
    "waldur_core.core.authentication.OIDCAuthentication",
),
```

The `w_` prefix lets `PATAuthentication` claim the request and fall through to `OIDCAuthentication` for non-PAT Bearer tokens.

## API Endpoints

Base URL: `/api/personal-access-tokens/`

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/personal-access-tokens/` | List user's tokens (no plaintext) |
| POST | `/api/personal-access-tokens/` | Create a new token (plaintext returned once) |
| GET | `/api/personal-access-tokens/{uuid}/` | Retrieve token details (no plaintext) |
| DELETE | `/api/personal-access-tokens/{uuid}/` | Soft-revoke (sets `is_active=False`) |
| POST | `/api/personal-access-tokens/{uuid}/rotate/` | Atomic rotation: creates new token, revokes old |
| POST | `/api/personal-access-tokens/{uuid}/set_network_acl/` | Replace the token's source-network allowlist |
| GET | `/api/personal-access-tokens/available_scopes/` | Lists all delegatable permission enum values |
| GET | `/api/personal-access-tokens/templates/` | Token templates the caller can use, such as the site agent's |
| GET | `/api/personal-access-tokens/template_targets/?template=<key>` | Entities a template's token can be bound to |
| GET | `/api/personal-access-tokens/available_binding_entities/?type=<type>&query=<text>` | Entities the caller can restrict a token to (first 50 matches) |
| POST | `/api/personal-access-tokens/agent_user/` | Staff: create a dedicated user for a template target and its restricted token |

PUT and PATCH are disabled -- tokens are immutable after creation.

### Create Token

**POST** `/api/personal-access-tokens/`

Needs a re-authentication grant for `create_personal_access_token` in the `X-Reauthentication-Grant` header (see [Re-authentication](#re-authentication)).

Request:

```json
{
  "name": "CI/CD Pipeline",
  "scopes": ["LIST_ORDERS", "LIST_RESOURCES"],
  "expires_at": "2025-12-31T23:59:59Z"
}
```

Response (201, with `Cache-Control: no-store` header):

```json
{
  "uuid": "abc123...",
  "name": "CI/CD Pipeline",
  "token": "w_full_plaintext_token_shown_only_once",
  "scopes": ["LIST_ORDERS", "LIST_RESOURCES"],
  "expires_at": "2025-12-31T23:59:59Z",
  "created": "2025-01-15T10:00:00Z"
}
```

### Templates

A template names the scopes a known integration needs, so its token carries
those and nothing more. It is a suggestion only: the token is still created
through `POST /api/personal-access-tokens/` with explicit scopes, and the create
checks apply as usual. Neither endpoint accepts a personal access token.

`site_agent` is the first template. Its required scopes are what the site
agent's own calls are checked against:

| Scope | Agent calls |
|-------|-------------|
| `ORDER.APPROVE` | `approve_by_provider`, `set_state_done`, `set_state_erred` on orders |
| `ORDER.REJECT` | `reject_by_provider` on orders |
| `RESOURCE.SET_BACKEND_ID` | `set_backend_id` on orders and provider resources |
| `RESOURCE.SET_STATE` | `set_as_ok`, `set_as_erred`, `refresh_last_sync`, `set_limits` |
| `RESOURCE.SET_BACKEND_METADATA` | `set_backend_metadata`, `set_endpoints`, `set_membership_sync_statuses` |
| `RESOURCE.SET_USAGE` | `set_usage`, `set_user_usage`, `set_user_usages` |
| `OFFERING.UPDATE_USER` | offering user state transitions |
| `OFFERING.MANAGE_AGENT` | agent identity, service and queue registration; offering `set_backend_metadata`, `check_unique_backend_id` and `site_agent_config`; SLURM policy `report-command-result` |

Optional features add `OFFERING.UPDATE_COMPONENTS` (component sync),
`OFFERING.MANAGE_BACKEND_RESOURCES` (backend resource import),
`SERVICE_PROVIDER.LIST_KEYS` (the SSH keys of the offering's users) and
`RESOURCE.MANAGE_API_KEY` (resource API keys).

`SERVICE_PROVIDER.LIST_KEYS` held on an offering (`OFFERING.MANAGER` has it)
lets `/api/marketplace-service-providers/<uuid>/keys/` list the keys of the
users of that offering's resources; held on the provider's organization it
lists the keys of all the provider's users, as before.

**Where it is bound: one offering.** A site-agent token is bound to the
offering it serves:

- Its user holds the template's scopes on the offering (`OFFERING.MANAGER`) or
  on the offering's organization (`CUSTOMER.OWNER`). The agent's checks accept
  a role above the binding (`binding_on_path`): a binding that names the
  offering counts for the orders, resources, offering users, usage and agent
  registration of that offering, whether the role is held on the offering or
  on its organization. The token reaches nothing outside that offering.
- An organization binding is not offered. A token bound to the organization
  (and its service provider, to see the resources it sells) would reach the
  organization's own purchases and the provider's account directory.
- An offering binding lets the token list and retrieve the offering's service
  provider record, which the agent looks up at startup. It does not reach the
  provider's detail actions, such as its team.
- What a token reads follows the user's roles, not the token's scopes. The
  role must therefore also grant the template's `role_permissions`, which for
  the site agent are `ORDER.LIST` (which orders it sees) and `OFFERING.UPDATE`
  (a resource's team). The token does not carry them.

`template_targets` lists the offerings where the caller's roles hold all of
that, each with the `bindings` to create the token with and the optional
features they can add there. Site-wide grants make no target. `templates`
lists a template only when it has a target, and lists nothing to a user who
may not create tokens.

A staff user's token without `STAFF.ACCESS` acts as the user without staff. So
staff get two kinds of target:

- an offering where their own roles qualify, as anyone else (a scoped token);
- every other offering, marked `staff_access`. There the token works only with
  `STAFF.ACCESS` added to its scopes, and such a token cannot be restricted: its
  `bindings` are empty and it can do everything the staff user can. The
  offering only fills in the agent's configuration.

Staff can instead create a dedicated user for the offering (`agent_user`, with
the same re-authentication as creating a token). It creates a user with no
usable password that only holds `OFFERING.MANAGER` on the offering (its
username, unless one is chosen, is the template, the offering's name and the
start of its UUID, such as `site-agent-slurm-hpc-cluster-f30000`, numbered
`-2`, `-3`… while taken; `template_targets` shows staff that name as
`suggested_username`), checks that
this role grants what the template needs on this site, and returns a token for
that user restricted to the offering, with the chosen optional features. Such
a token passes no owner-only check and reaches no other offering. Everything is
rolled back when any step is refused.

The audit log records the creation in the user's and the creating staff
user's event streams: `user_creation_succeeded`, `dedicated_user_created`
(naming the staff user, the template and the offering), `role_granted` (with
the reason "Dedicated Site agent user", initiated by the staff user) and
`pat_created`.

The dedicated user is marked `is_service_account` (read-only in the API). It has
no usable password and normally authenticates with its token. Single sign-on
still finds it by username like any account and may sign in to it, for a team
that logs in to it on purpose; every such sign-in (OIDC token, OIDC/OAuth login,
identity bridge) is logged at WARNING and recorded as a
`service_account_signed_in` event, since a username collision looks the same.

Which rows a token can read still follows the user's roles, not the token's
scopes, so the template carries no `*.LIST` permissions.

### Restricting a token by hand

`available_binding_entities` lists, for one entity type, the entities the
create endpoint accepts as a binding: those the caller holds an active role on,
directly or on an ancestor (an organization's projects, offerings and
resources, a project's resources, an offering's resources). Staff and holders
of a site-wide grant get any entity. Whether the role grants one of the token's
permissions there is checked when the token is created.

### Rotate Token

**POST** `/api/personal-access-tokens/{uuid}/rotate/`

Atomically revokes the old token and creates a new one with the same name, scopes, and expiration. The operation uses `select_for_update()` to prevent races. Like create, it needs a re-authentication grant for `create_personal_access_token`. Returns the same response format as create.

### Revoke Token

**DELETE** `/api/personal-access-tokens/{uuid}/`

Sets `is_active=False` on the token (soft delete). Returns 204.

## Scope Enforcement

**Restricted tokens and role checks.** Some actions check only that the user
holds a role (`is_owner`, `is_service_manager`, `is_administrator`,
`is_offering_manager`), not a permission, so a token's scopes do not narrow
them; its bindings do. A token passes such a check within its bindings with its
user's roles, whatever scopes it carries. This is what lets an owner's token
bound to a project manage that project's credit, policies or ports.

It also means an organization owner's site-agent token, bound to one offering,
can perform the owner-only actions on that offering: update or delete its
policies, edit its tags, organization groups and user attribute
configuration. An offering manager's token cannot, as those checks want the
owner role.

An owner's token bound to one offering also cannot complete an offering user's
deletion (`set_deleted`, `request_deletion`) or set POSIX attributes: those
act on the provider account and identities shared by the provider's offerings,
so they need a binding that covers the organization. Where either matters, give
the agent its own user with only the `OFFERING.MANAGER` role on the offering.

PAT scopes follow an intersection model: the effective permission at request time is the intersection of the PAT's scopes and the user's current roles.

```text
effective_permissions = PAT_scopes AND user_current_roles
```

Key implementation details in `waldur_core.permissions.utils`:

- `_pat_scope_check()` runs in both `has_permission()` and `has_any_permission()` **before** the `is_staff` bypass, so staff PATs are properly scoped
- If a user loses a role after creating a PAT, the PAT's matching permissions stop working immediately
- Scope validation at creation ensures users can only request permissions they currently have

### Staff and support checks

A staff user's PAT acts as staff only with the `STAFF.ACCESS` scope, and as
support only with `STAFF.ACCESS` or `SUPPORT.ACCESS`. A check that lets staff
or support through must therefore ask the request, not the user:

- `has_staff_access(request)` and `has_support_access(request)` in
  `waldur_core.core.permissions`, or the permission classes built on them
  (`IsStaff`, `IsSupport`, `IsStaffOrSupportUser`, `PATScopeAwareIsAdminUser`)
- the function permissions `structure_permissions.is_staff` and
  `is_staff_or_support`, which call them

A token that lacks the scope its user's role needs also reads as the user without
that role. `PATAuthentication` hands such a request a `TokenScopedUser`, a proxy of
`User` whose `is_staff` and `is_support` read as the token allows:

| Account | Token carries | `is_staff` | `is_support` |
|---|---|---|---|
| staff | neither scope | false | false |
| staff | `SUPPORT.ACCESS` only | false | true |
| support | neither scope | false | false |

Every check on the user's flags follows the token, without the check changing:
`filter_queryset_for_user`, `GenericRoleFilter`, serializer `get_fields`, media
access rules and the staff bypass in `has_permission`. Such a token therefore
sees and does only what the user's roles allow. `/api/users/me/` reports the
flags the token acts with.

The proxy never writes the narrowed flags: `save`, `delete` and
`refresh_from_db` run as a plain `User`, so the database and receivers of `User`
signals see the account's flags; `save` leaves `is_staff` / `is_support` out; and
assigning them raises. `account_is_staff` / `account_is_support` read the account's
flags, which `TokenCarriesUserRole` needs. A task given the user, or its id, loads
it again and sees the account's flags.

Credentials in `GET /api/service-settings/` are the exception: a token sees them
only with `STAFF.ACCESS` or, for an organization owner, with
`OFFERING.UPDATE_INTEGRATION`, the permission guarding the same values on an
offering. Its bindings must cover the organization in both cases, so a bound
token never sees the credentials of settings that belong to no organization. A
staff token without `STAFF.ACCESS` reads as not staff, so it sees these settings
only as far as its user's roles reach. Any other token gets the redacted options a non-owner sees. Use `check_pat_permission` for a check
that decides the role itself and needs only the token's limits on top.

## Security

### PAT-via-PAT Blocked

The `create`, `destroy`, `rotate`, and `set_network_acl` actions reject requests authenticated with a PAT. This prevents token escalation -- PATs can only be managed through session or token authentication.

### Re-authentication

Creating a token, rotating one, and widening a token's network ACL (see [Network ACLs](#network-acls)) each need a re-authentication grant: proof that the user has just confirmed who they are, which holding their session or API token alone is not. Without a valid grant the request gets a 401 whose body has `"code": "reauthentication_required"`, the `action` the grant must be for, and the `methods` the user can confirm with.

Get a grant for that action, then send it in the `X-Reauthentication-Grant` header of the request:

- **Password:** `POST /api/reauthentication/password/` with `{"action": "<action>", "password": "<current password>"}`. The 201 response carries the `grant` and its `expires_at`.
- **Passkey:** `POST /api/reauthentication/passkey/begin/` with `{"action": "<action>"}` returns a WebAuthn challenge; `POST /api/reauthentication/passkey/finish/` with the `action`, the `ceremony` and the signed `credential` returns the grant.

The actions are `create_personal_access_token` (create and rotate) and `widen_personal_access_token_networks`. A grant is single-use, bound to the user and to one action, and expires after `REAUTHENTICATION_GRANT_LIFETIME_SECONDS`. A grant is neither issued nor accepted for a request that is not the user's own sign-in: staff impersonating the user, a personal access token and an OIDC access token get 403.

### Revealing resource API keys

`POST /api/marketplace-resource-api-keys/{uuid}/reveal/` admits a PAT only when
it carries `RESOURCE.REVEAL_API_KEY` and is unbound or bound to the key's
resource, its project or its organization. A binding on the resource's offering
or its service provider does not count: reveal stays consumer-side. Any other
PAT gets `403`, whatever its other scopes. The token's user is then held to the
same rules as without a token: a shared key needs `RESOURCE.REVEAL_API_KEY`
through a role on the project or organization, and an assigned key is revealed
only to its assignee. No token carries the scope unless it was issued with it,
so this is how a CI/CD pipeline gets a credential that can read a resource's
keys and nothing else. The S3 bucket browser actions (`…/s3/…`) refuse every
PAT, whatever its scopes: the reveal scope is for reading a key, not for changing
buckets and objects through Waldur. A PAT still cannot `rotate` or `retry` a key
as its assignee. A bound PAT lists only the keys of resources its bindings reach. See
[Resource API keys](resource-api-keys.md).

### Uniform Error Responses

All authentication failures return the same `"Invalid token."` message regardless of the failure reason (inactive, expired, nonexistent). This prevents information leakage about token state.

### User Deactivation Cascade

The `revoke_user_pats_on_deactivation` signal handler in `waldur_core.core.handlers` revokes all active PATs when a user's `is_active` changes from `True` to `False`.

### Who May Have Tokens

A user may create, rotate and authenticate with tokens when the `PAT_ENABLED` setting is on **or** the user's own `can_use_personal_access_tokens` flag is set:

| `PAT_ENABLED` | User flag | Tokens |
|---------------|-----------|--------|
| on | any | yes |
| off | on | yes |
| off | off | no |

So `PAT_ENABLED` gives tokens to every user, and the flag gives them to selected users on a deployment that keeps them off for everyone else. Staff are not special-cased. `core.utils.can_use_personal_access_tokens(user)` holds the rule; token creation, rotation, REST and MCP authentication, and the template endpoints all call it.

Neither switch deletes tokens. While a user is not allowed tokens, their existing tokens are refused, with the `pats_disabled` audit reason (see [Network ACLs](#network-acls)). They work again once the user is allowed tokens, unless they have expired or been revoked in the meantime. To cut off one user while `PAT_ENABLED` is on, revoke their tokens or deactivate the account; clearing the flag has no effect then.

Before this rule, a user needed both `PAT_ENABLED` and the flag. On upgrade, migration `core.0062` turns a stored `PAT_ENABLED` of `True` off, so exactly the flagged users keep their tokens and nobody gains any. To give tokens to every user, turn `PAT_ENABLED` on again afterwards. A value the deployment applies from a settings file on every deploy is restored by the next deploy, which the migration logs as a warning; change it in that file.

## Configuration

### Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `PAT_ENABLED` | `False` | Give tokens to every user; while off, only users with the per-user flag have them (see [Who May Have Tokens](#who-may-have-tokens)) |
| `PAT_MAX_LIFETIME_DAYS` | `365` | Maximum token lifetime in days |
| `PAT_MAX_TOKENS_PER_USER` | `20` | Maximum active tokens per user |

These settings are in the "Personal Access Tokens" fieldset of the settings page in the Waldur admin (*Configuration → Settings*).

## Model

`PersonalAccessToken` in `waldur_core.core.models`:

| Field | Type | Description |
|-------|------|-------------|
| `user` | ForeignKey(User) | Token owner |
| `name` | CharField | User-assigned name |
| `token_prefix` | CharField | First 8 chars of token (for UI display) |
| `token_hash` | CharField(64) | SHA-256 hash (unique, indexed) |
| `scopes` | JSONField | List of `PermissionEnum` values |
| `expires_at` | DateTimeField | Expiration timestamp |
| `is_active` | BooleanField | Whether the token is active |
| `last_used_at` | DateTimeField | Last usage timestamp |
| `last_used_ip` | GenericIPAddressField | Last client IP |
| `use_count` | PositiveIntegerField | Total usage count |

## Usage Tracking

The `last_used_at`, `last_used_ip`, and `use_count` fields are updated via a batched write with 10-minute cache-based debouncing (cache key: `pat_usage:{pk}`). This avoids a database write on every authenticated request.

## Audit Events

| Event Type | Trigger |
|------------|---------|
| `pat_created` | Token created |
| `pat_revoked` | Token revoked (manual or user deactivation cascade) |
| `pat_rotated` | Token rotated |
| `pat_expired` | Token expired (deactivated by cleanup task) |
| `pat_used_from_new_ip` | Token used from a different IP than `last_used_ip` |
| `pat_access_denied_from_ip` | Request rejected because the source address is outside the token's network ACL |
| `pat_network_acl_updated` | Token's network ACL replaced via `set_network_acl` |

All events are scoped to the token's user and grouped under `EventGroup.AUTH` (via `EVENT_GROUP_MAPPING`) in `waldur_core.logging.enums`.

## Celery Tasks

The `cleanup_expired_personal_access_tokens` task runs every 6 hours. It deactivates all PATs where `expires_at <= now()` and `is_active=True`, emitting a `pat_expired` event for each.

## Examples

### Get a re-authentication grant

Creating and rotating a token each need a fresh grant for `create_personal_access_token`:

```bash
curl -X POST https://waldur.example.com/api/reauthentication/password/ \
  -H "Authorization: Token YOUR_SESSION_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"action": "create_personal_access_token", "password": "YOUR_PASSWORD"}'
```

The `grant` in the response is used in the examples below as `YOUR_GRANT`. It is single-use: get a new one for each change.

### Create a PAT

```bash
curl -X POST https://waldur.example.com/api/personal-access-tokens/ \
  -H "Authorization: Token YOUR_SESSION_TOKEN" \
  -H "X-Reauthentication-Grant: YOUR_GRANT" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "CI/CD Pipeline",
    "scopes": ["LIST_ORDERS", "LIST_RESOURCES"],
    "expires_at": "2025-12-31T23:59:59Z"
  }'
```

### Use the PAT

```bash
curl https://waldur.example.com/api/resources/ \
  -H "Authorization: Bearer w_returned_token_value"
```

### Rotate a PAT

```bash
curl -X POST https://waldur.example.com/api/personal-access-tokens/{uuid}/rotate/ \
  -H "Authorization: Token YOUR_SESSION_TOKEN" \
  -H "X-Reauthentication-Grant: YOUR_GRANT"
```

### Revoke a PAT

```bash
curl -X DELETE https://waldur.example.com/api/personal-access-tokens/{uuid}/ \
  -H "Authorization: Token YOUR_SESSION_TOKEN"
```

## Network ACLs

A token can be restricted to a set of source networks. `allowed_networks` takes a list of CIDR strings; bare addresses are widened to `/32` or `/128`. An empty list (the default) means the token is unrestricted. The number of entries is capped by the `PAT_MAX_ACL_ENTRIES` setting.

Set the ACL at creation time:

```json
{
  "name": "CI/CD Pipeline",
  "scopes": ["LIST_ORDERS"],
  "expires_at": "2025-12-31T23:59:59Z",
  "allowed_networks": ["203.0.113.0/24", "2001:db8::/32"]
}
```

To change the ACL of an existing token, use the `set_network_acl` action:

**POST** `/api/personal-access-tokens/{uuid}/set_network_acl/`

```json
{"allowed_networks": ["203.0.113.0/24"]}
```

A dedicated action is used rather than PATCH because PUT and PATCH stay disabled on the viewset, and the change is recorded as its own `pat_network_acl_updated` audit event.

Narrowing the ACL needs nothing more. A change narrows it when every new network lies inside a current network of the same address family, when it restricts an unrestricted token, or when it leaves the list as it is. An IPv6 network never counts as inside an IPv4 one.

Any other change widens the ACL: adding a network, replacing one with a broader one, or clearing the list, which lets the token work from anywhere. Widening needs a re-authentication grant for `widen_personal_access_token_networks`, sent in the `X-Reauthentication-Grant` header, as creating or rotating a token does. Without one the request gets the 401 `reauthentication_required` challenge and the ACL stays as it was. See [Re-authentication](#re-authentication) for how to get a grant.

Widening is refused (403) to any caller that is not the user's own sign-in: staff impersonating the user, an OIDC access token, or any other delegated credential. A personal access token gets 403 for every change to the ACL, narrowing included, as for the other token management actions.

To widen an ACL, get a grant for `widen_personal_access_token_networks` and send it with the new list:

```bash
curl -X POST https://waldur.example.com/api/personal-access-tokens/{uuid}/set_network_acl/ \
  -H "Authorization: Token YOUR_SESSION_TOKEN" \
  -H "X-Reauthentication-Grant: YOUR_GRANT" \
  -H "Content-Type: application/json" \
  -d '{"allowed_networks": ["203.0.113.0/24", "198.51.100.0/24"]}'
```

The action takes the same row lock as `rotate`, so a concurrent rotation cannot copy a stale ACL onto the new token, and it decides whether a change widens against the locked row. Setting the ACL of an inactive token returns 400.

A request from outside the ACL is rejected with the same generic `Invalid token.` error as any other authentication failure -- the response never confirms that the token itself is valid. The rejection is recorded as a `pat_access_denied_from_ip` audit event with the source address (debounced per token and source IP).

Enforcement **fails closed**: if the source address cannot be determined, a token with a non-empty ACL is rejected.

A known token presented after it should no longer work -- revoked, or with a deactivated owner, or an owner who is not allowed personal access tokens -- is rejected with the same generic error and recorded as a `pat_authentication_rejected` audit event carrying a `reason` (`revoked`, `user_inactive`, `pats_disabled`), debounced per token, reason and source IP. Events recorded before `pats_disabled` existed carry `permission_revoked` for the same check. An expired token is rejected without an individual audit event (expiry is an expected lifecycle end, already surfaced by the `pat_expired` event the cleanup task emits, not a replay signal). An unknown token is never audited, so a forged bearer token cannot flood the event log.

Audit volume is additionally capped per token: `PAT_MAX_AUDIT_EVENTS_PER_HOUR` (default 50) limits how many events one token may generate per hour, counted separately for source-address changes and for rejections. Beyond the ceiling the events and the `last_used_at` / `last_used_ip` / `use_count` write are suppressed for the rest of the window and a warning is logged; the requests themselves are unaffected. The per-address debounce alone cannot bound this -- a caller with an IPv6 /64 has effectively unlimited source addresses.

### Deployment requirement

The network ACL is a security boundary only as strong as the proxies that report the client address. Waldur resolves that address the same way everywhere (access subnets, marketplace concealment, the audit trail, brute-force protection): from the `X-Forwarded-For` entry that its own proxies added, `WALDUR_CORE["TRUSTED_PROXY_COUNT"]` places from the right (default `1`), or from `REMOTE_ADDR` when there is no such header. A header without a usable entry at that place leaves the request with no address, and a token with a non-empty ACL is then rejected, even if the proxy's own address is inside the ACL. Entries before that one are whatever the caller sent and are ignored, so sending `X-Forwarded-For: <an-allowed-ip>` through a proxy that appends to the header does not satisfy the ACL.

Set the count to the number of proxies in front of Waldur that add an entry: `1` for a single ingress, `2` for a load balancer in front of an ingress when both append, `0` when Waldur is reached directly. A count higher than the real number of proxies lets the caller choose the address again. See [the client address behind proxies](admin/brute-force-protection.md#the-client-address-behind-proxies) for each ingress flavour, including the `proxy-real-ip-cidr` setting that ingress-nginx needs with `use-forwarded-headers`.

### Audit attribution

Every audit event carries `auth_method` in its context -- `session`, `pat`, `token`, or `oidc`. PAT-authenticated requests additionally carry `pat_uuid` and `pat_name`, so an action can be traced to the specific token. Filter with `/api/events/?auth_method=pat` or `/api/events/?pat_uuid=<uuid>`.

## See Also

- [Service Accounts](service-accounts.md) -- programmatic access at organizational/project scope
- [Identity Bridge](identity-bridge.md) -- federated identity management
