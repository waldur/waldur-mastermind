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

All events are scoped to the token's user and registered under `USER_MANAGEMENT_EVENTS` in `waldur_core.logging.enums`.

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

## See Also

- [Service Accounts](service-accounts.md) -- programmatic access at organizational/project scope
- [Identity Bridge](identity-bridge.md) -- federated identity management
