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

PUT and PATCH are disabled -- tokens are immutable after creation.

### Create Token

**POST** `/api/personal-access-tokens/`

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

### Rotate Token

**POST** `/api/personal-access-tokens/{uuid}/rotate/`

Atomically revokes the old token and creates a new one with the same name, scopes, and expiration. The operation uses `select_for_update()` to prevent races. Returns the same response format as create.

### Revoke Token

**DELETE** `/api/personal-access-tokens/{uuid}/`

Sets `is_active=False` on the token (soft delete). Returns 204.

## Scope Enforcement

PAT scopes follow an intersection model: the effective permission at request time is the intersection of the PAT's scopes and the user's current roles.

```text
effective_permissions = PAT_scopes AND user_current_roles
```

Key implementation details in `waldur_core.permissions.utils`:

- `_pat_scope_check()` runs in both `has_permission()` and `has_any_permission()` **before** the `is_staff` bypass, so staff PATs are properly scoped
- If a user loses a role after creating a PAT, the PAT's matching permissions stop working immediately
- Scope validation at creation ensures users can only request permissions they currently have

## Security

### PAT-via-PAT Blocked

The `create`, `destroy`, and `rotate` actions reject requests authenticated with a PAT. This prevents token escalation -- PATs can only be managed through session or token authentication.

### Uniform Error Responses

All authentication failures return the same `"Invalid token."` message regardless of the failure reason (inactive, expired, nonexistent). This prevents information leakage about token state.

### User Deactivation Cascade

The `revoke_user_pats_on_deactivation` signal handler in `waldur_core.core.handlers` revokes all active PATs when a user's `is_active` changes from `True` to `False`.

### Global Kill Switch

The `PAT_ENABLED` Constance setting (default: `True`) controls whether PAT authentication is active. When `False`, `PATAuthentication.authenticate()` returns `None` for all `w_`-prefixed tokens, effectively disabling all PATs system-wide.

## Configuration

### Constance Settings

| Setting | Default | Description |
|---------|---------|-------------|
| `PAT_ENABLED` | `True` | Global enable/disable for PAT authentication |
| `PAT_MAX_LIFETIME_DAYS` | `365` | Maximum token lifetime in days |
| `PAT_MAX_TOKENS_PER_USER` | `20` | Maximum active tokens per user |

These settings are in the "Personal Access Tokens" fieldset in the Constance admin panel.

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

### Create a PAT

```bash
curl -X POST https://waldur.example.com/api/personal-access-tokens/ \
  -H "Authorization: Token YOUR_SESSION_TOKEN" \
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
  -H "Authorization: Token YOUR_SESSION_TOKEN"
```

### Revoke a PAT

```bash
curl -X DELETE https://waldur.example.com/api/personal-access-tokens/{uuid}/ \
  -H "Authorization: Token YOUR_SESSION_TOKEN"
```

## Network ACLs

A token can be restricted to a set of source networks. `allowed_networks` takes a list of CIDR strings; bare addresses are widened to `/32` or `/128`. An empty list (the default) means the token is unrestricted. The number of entries is capped by the `PAT_MAX_ACL_ENTRIES` Constance setting.

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

The action takes the same row lock as `rotate`, so a concurrent rotation cannot copy a stale ACL onto the new token. Setting the ACL of an inactive token returns 400.

A request from outside the ACL is rejected with the same generic `Invalid token.` error as any other authentication failure -- the response never confirms that the token itself is valid. The rejection is recorded as a `pat_access_denied_from_ip` audit event with the source address (debounced per token and source IP).

Enforcement **fails closed**: if the source address cannot be determined, a token with a non-empty ACL is rejected.

A known token presented after it should no longer work -- revoked, or with a deactivated owner, or an owner no longer permitted personal access tokens -- is rejected with the same generic error and recorded as a `pat_authentication_rejected` audit event carrying a `reason` (`revoked`, `user_inactive`, `permission_revoked`), debounced per token, reason and source IP. An expired token is rejected without an individual audit event (expiry is an expected lifecycle end, already surfaced by the `pat_expired` event the cleanup task emits, not a replay signal). An unknown token is never audited, so a forged bearer token cannot flood the event log.

Audit volume is additionally capped per token: `PAT_MAX_AUDIT_EVENTS_PER_HOUR` (default 50) limits how many events one token may generate per hour, counted separately for source-address changes and for rejections. Beyond the ceiling the events and the `last_used_at` / `last_used_ip` / `use_count` write are suppressed for the rest of the window and a warning is logged; the requests themselves are unaffected. The per-address debounce alone cannot bound this -- a caller with an IPv6 /64 has effectively unlimited source addresses.

### Deployment requirement

Waldur resolves the client address from the **first** `X-Forwarded-For` entry, falling back to `REMOTE_ADDR`. This is the same resolver used across the platform (audit logging, `AccessSubnet`, marketplace filters) and matches the ingress `whitelistSourceRange`.

The network ACL is a **security boundary, and it is only as strong as the trust boundary that populates `X-Forwarded-For`.** Because the resolver takes the first entry, that entry must be written by a proxy you control, not by the caller:

- **Safe (required):** the edge proxy / ingress **overwrites** `X-Forwarded-For` with the real client address on each request. The default `nginx` ingress does exactly this out of the box (`use-forwarded-headers: false`), so the first entry is always the address that actually connected to the load balancer.
- **Unsafe — defeats the ACL:** any proxy in front that is configured to **trust and append** to an incoming `X-Forwarded-For` (for nginx ingress, `use-forwarded-headers: true`) without restricting that trust to known upstream proxies. In that mode a caller can send `X-Forwarded-For: <an-allowed-ip>` and the spoofed value becomes the first entry, bypassing the ACL entirely (`curl -H "X-Forwarded-For: 203.0.113.5" ...`).

Do **not** enable forwarded-header trust/append mode unless the proxy accepting it is itself reachable only through a trusted, IP-restricted upstream that overwrites the header. The same caveat applies to `AccessSubnet` and marketplace IP filters, which share this resolver. Enforcement fails closed on an unresolvable address (a token with a non-empty ACL is rejected), but it cannot distinguish a spoofed first entry from a genuine one — that distinction is the proxy's job.

### Audit attribution

Every audit event carries `auth_method` in its context -- `session`, `pat`, `token`, or `oidc`. PAT-authenticated requests additionally carry `pat_uuid` and `pat_name`, so an action can be traced to the specific token. Filter with `/api/events/?auth_method=pat` or `/api/events/?pat_uuid=<uuid>`.

## See Also

- [Service Accounts](service-accounts.md) -- programmatic access at organizational/project scope
- [Identity Bridge](identity-bridge.md) -- federated identity management
