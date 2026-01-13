# Multi-Client OIDC Authentication

## Overview

Waldur Mastermind supports authentication from multiple client applications to a single backend. This feature allows different client domains to authenticate users via OIDC while ensuring users are redirected back to the correct client application after authentication.

## How It Works

### Authentication Flow

1. **User initiates login**: User clicks "Sign in with OIDC" on a client application (e.g., `https://app1.example.com`)
2. **Return URL stored**: Mastermind stores either:
   - The `return_url` query parameter (if provided), OR
   - The HTTP Referer header (fallback)
3. **OIDC redirect**: User is redirected to the OIDC provider for authentication
4. **OIDC callback**: After authentication, OIDC provider redirects to Mastermind's callback URL
5. **Validation**: Mastermind validates the stored return URL against the allowed redirects list
6. **User redirect**: User is redirected back to the original client application with authentication token

### Security

- **Whitelist validation**: Only pre-configured client domains are allowed
- **Open redirect prevention**: Referrer URLs are validated against the allowed list
- **Base URL matching**: Referrer validation uses only scheme and domain (path is ignored)

## Validation Rules

The `allowed_redirects` field enforces strict security rules to prevent open redirect attacks and ensure proper authentication flow:

### URL Format Requirements

- **Origin-only URLs**: Only scheme + domain + port allowed (no paths, query parameters, or fragments)
  - Valid: `https://homeport.example.com`
  - Invalid: `https://homeport.example.com/path`, `https://homeport.example.com?query=value`
- **No trailing slashes**: URLs must not end with `/`
  - Valid: `https://homeport.example.com`
  - Invalid: `https://homeport.example.com/`
- **Exact matching**: URLs are validated exactly as configured (no pattern matching or wildcards)

### Security Requirements

- **HTTPS-only**: All URLs must use `https://` scheme (exceptions: localhost and 127.0.0.1)
  - Valid: `https://homeport.example.com`, `http://localhost:8080`, `http://127.0.0.1:8000`
  - Invalid: `http://homeport.example.com`, `ftp://example.com`
- **No wildcards**: Domain patterns like `*.example.com` are not supported
- **Complete URLs required**: Each Homeport instance must be explicitly listed

### Validation Examples

**Valid configurations:**

```json
{
  "allowed_redirects": [
    "https://homeport1.example.com",
    "https://homeport2.example.com:8443",
    "http://localhost:8080",
    "http://127.0.0.1:3000"
  ]
}
```

**Invalid configurations:**

```json
{
  "allowed_redirects": [
    "https://homeport.example.com/",     // ❌ Trailing slash
    "https://homeport.example.com/path", // ❌ Path component
    "http://homeport.example.com",       // ❌ HTTP for non-localhost
    "homeport.example.com",              // ❌ Missing scheme
    "*.example.com"                      // ❌ Wildcards not supported
  ]
}
```

## Configuration

### Identity Provider Setup

Configure the `allowed_redirects` field for each Identity Provider via the API:

```json
{
  "provider": "keycloak",
  "label": "Keycloak SSO",
  "client_id": "waldur-client",
  "client_secret": "secret",
  "discovery_url": "https://keycloak.example.com/.well-known/openid-configuration",
  "allowed_redirects": [
    "https://homeport1.example.com",
    "https://homeport2.example.com",
    "https://homeport3.example.com"
  ]
}
```

### OIDC Provider Configuration

**Important**: All Homeport instances use the **same callback URL** on Mastermind:

```
https://api.waldur.example.com/api-auth/{provider}/complete/
```

Register this single callback URL with your OIDC provider. No per-Homeport callback registration is required.

### Homeport Integration

**Note:** This section provides API-level documentation for the backend feature. For complete frontend integration guide with code examples, see the Homeport repository documentation.

Homeport frontends can specify the return URL in two ways:

#### Method 1: Explicit `return_url` Parameter (Recommended)

Pass the return URL as a query parameter when redirecting to the OIDC init endpoint:

```javascript
// In your Homeport frontend
const apiUrl = "https://api.waldur.example.com";
const homeportUrl = window.location.origin; // e.g., "https://homeport1.example.com"
const provider = "keycloak";

// Redirect to OIDC init with explicit return_url
window.location.href = `${apiUrl}/api-auth/${provider}/init/?return_url=${encodeURIComponent(homeportUrl)}`;
```

**Benefits:**
- More explicit and reliable
- Works regardless of browser referrer policy
- Easier to debug and test

#### Method 2: HTTP Referer Header (Automatic Fallback)

If no `return_url` is provided, Mastermind automatically uses the HTTP Referer header:

```javascript
// In your Homeport frontend
const apiUrl = "https://api.waldur.example.com";
const provider = "keycloak";

// Redirect to OIDC init (referrer sent automatically by browser)
window.location.href = `${apiUrl}/api-auth/${provider}/init/`;
```

**Note:** Ensure your Homeport has an appropriate referrer policy:

```html
<!-- In your Homeport's HTML <head> -->
<meta name="referrer" content="origin" />
```

Or in JavaScript:

```html
<a href="https://api.waldur.example.com/api-auth/keycloak/init/"
   referrerpolicy="origin">
  Sign in with Keycloak
</a>
```

#### Priority

When both are available, `return_url` takes priority over the HTTP Referer header.

### API Endpoints

#### Create/Update Identity Provider

**Endpoint**: `POST /api/identity-providers/` or `PATCH /api/identity-providers/{provider}/`

**Request Body**:
```json
{
  "provider": "keycloak",
  "label": "My Keycloak",
  "client_id": "waldur",
  "client_secret": "your-secret",
  "discovery_url": "https://keycloak.example.com/.well-known/openid-configuration",
  "allowed_redirects": [
    "https://homeport1.example.com",
    "https://homeport2.example.com"
  ]
}
```

**Field Validation**:
- `allowed_redirects` must be a list of valid URLs
- Each URL must include scheme (`http://` or `https://`) and domain
- URLs are normalized (trailing slashes are handled automatically)

#### Get Identity Providers

**Endpoint**: `GET /api/identity-providers/`

**Response**:
```json
[
  {
    "provider": "keycloak",
    "label": "Keycloak SSO",
    "is_active": true,
    "allowed_redirects": [
      "https://homeport1.example.com",
      "https://homeport2.example.com"
    ],
    ...
  }
]
```

## Behavior

### With `allowed_redirects` Configured

1. **Matching referrer**: User is redirected to the Homeport that initiated the request
2. **No referrer**: User is redirected to the **first** URL in `allowed_redirects`
3. **Invalid referrer**: Authentication fails with 401 error (referrer not in allowed list)

### Without `allowed_redirects` (Backward Compatibility)

If `allowed_redirects` is empty or not set, the system falls back to the `HOMEPORT_URL` constance setting:

```python
# settings.py
CONSTANCE_CONFIG = {
    'HOMEPORT_URL': ('https://default.example.com/', 'Default Homeport URL'),
}
```

This maintains backward compatibility with existing single-Homeport deployments.

## Migration Guide

### Upgrading from Single Homeport

1. **Run migrations**:
   ```bash
   python manage.py migrate waldur_auth_social
   ```

2. **Update Identity Provider** via API:
   ```bash
   curl -X PATCH https://api.waldur.example.com/api/identity-providers/keycloak/ \
     -H "Authorization: Token YOUR_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "allowed_redirects": ["https://homeport1.example.com"]
     }'
   ```

3. **Add additional Homeports** as needed:
   ```bash
   curl -X PATCH https://api.waldur.example.com/api/identity-providers/keycloak/ \
     -H "Authorization: Token YOUR_TOKEN" \
     -H "Content-Type: application/json" \
     -d '{
       "allowed_redirects": [
         "https://homeport1.example.com",
         "https://homeport2.example.com"
       ]
     }'
   ```

### Adding a New Homeport

1. Configure your new Homeport to use the same Mastermind API URL
2. Add the new Homeport URL to `allowed_redirects`:
   ```json
   {
     "allowed_redirects": [
       "https://existing-homeport.com",
       "https://new-homeport.com"
     ]
   }
   ```
3. No OIDC provider reconfiguration needed (callback URL remains the same)

## Examples

### Example 1: University with Multiple Portals

```json
{
  "provider": "eduteams",
  "allowed_redirects": [
    "https://students.university.edu",
    "https://faculty.university.edu",
    "https://admin.university.edu"
  ]
}
```

### Example 2: Multi-Tenant SaaS

```json
{
  "provider": "keycloak",
  "allowed_redirects": [
    "https://tenant1.waldur.cloud",
    "https://tenant2.waldur.cloud",
    "https://tenant3.waldur.cloud"
  ]
}
```

### Example 3: Development and Production

```json
{
  "provider": "tara",
  "allowed_redirects": [
    "https://portal.example.com",
    "https://staging.example.com",
    "http://localhost:8080"
  ]
}
```

## Troubleshooting

### Error: "Return URL domain is not in the allowed redirects list"

**Cause**: The Homeport URL that initiated the authentication is not in the `allowed_redirects` list, or the URL format is invalid.

**Solution**: Add the Homeport URL to the Identity Provider's `allowed_redirects` with correct format:

```bash
# Check current configuration
curl https://api.waldur.example.com/api/identity-providers/

# Update allowed_redirects with normalized URLs (no trailing slashes)
curl -X PATCH https://api.waldur.example.com/api/identity-providers/{provider}/ \
  -H "Content-Type: application/json" \
  -d '{"allowed_redirects": ["https://your-homeport.com"]}'
```

**Important**: Ensure URLs are normalized:
- Use `https://homeport.com` not `https://homeport.com/`
- Use HTTPS except for localhost/127.0.0.1
- Include only origin (no paths or query parameters)

### Users redirected to wrong Homeport

**Cause**: Referrer header not being sent by the browser.

**Solutions**:
1. Ensure Homeport is setting proper referrer policy
2. Check that the login link properly navigates to the Mastermind OIDC init URL
3. If referrer is unavailable, users will be redirected to the first URL in `allowed_redirects`

### Backward compatibility issues

**Cause**: Existing deployment not configured with `allowed_redirects`.

**Solution**: The system automatically falls back to `HOMEPORT_URL` constance setting when `allowed_redirects` is empty. No action required unless you want to enable multi-Homeport support.

## Technical Details

### Database Schema

```python
class IdentityProvider(models.Model):
    provider = models.CharField(max_length=32, unique=True)
    allowed_redirects = models.JSONField(
        default=list,
        blank=True,
        help_text=(
            "List of allowed Homeport URLs for redirect after OIDC authentication. "
            "URLs must be origin-only (scheme + domain + port), HTTPS-only except "
            "localhost/127.0.0.1, with no trailing slashes or paths."
        )
    )
    # ... other fields
```

### Session Storage

During authentication, the following session keys are used:
- `oidc_state`: CSRF protection token
- `oidc_code_verifier`: PKCE code verifier (if PKCE enabled)
- `oidc_referrer`: HTTP Referer header from the init request

### URL Normalization

Referrer URLs are normalized for comparison:
- Scheme and domain are extracted (e.g., `https://homeport.com/login` → `https://homeport.com`)
- Trailing slashes are handled consistently
- Path, query, and fragment components are ignored for validation

## API Reference

### IdentityProvider Model Fields

| Field | Type | Description |
|-------|------|-------------|
| `provider` | String | Provider identifier (tara, keycloak, eduteams) |
| `allowed_redirects` | JSON Array | List of allowed redirect URLs (origin-only, no trailing slashes) |
| `client_id` | String | OIDC client ID |
| `client_secret` | String | OIDC client secret |
| `discovery_url` | String | OIDC discovery endpoint |
| `is_active` | Boolean | Whether provider is enabled |

### Field Validation Rules

The `allowed_redirects` field enforces strict validation:

1. **Format**: Must be a JSON array of strings
2. **URL structure**: Each entry must be origin-only (scheme + domain + optional port)
3. **HTTPS requirement**: Must use `https://` (except `http://localhost` or `http://127.0.0.1`)
4. **No trailing slashes**: URLs must not end with `/`
5. **No paths/query/fragments**: Only the origin part is allowed
6. **Exact matching**: No wildcard or pattern matching support
7. **Empty arrays**: Valid (falls back to `HOMEPORT_URL` setting)

## See Also
- [OIDC Authentication Configuration](./admin/configuration-guide.md#waldur_auth_social-plugin)
