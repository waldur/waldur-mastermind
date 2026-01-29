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

## OIDC Claim Discovery and Mapping Wizard

Waldur provides API endpoints to help administrators discover available OIDC claims from identity providers and generate appropriate attribute mappings.

### Discover OIDC Metadata

Fetch the OIDC discovery document and get suggested claim mappings for Waldur user fields.

**Endpoint**: `POST /api/identity-providers/discover_metadata/`

**Permissions**: Staff only

**Request**:

```json
{
  "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
  "verify_ssl": true
}
```

**Response**:

```json
{
  "claims_supported": ["sub", "email", "given_name", "family_name", "schacHomeOrganization"],
  "scopes_supported": ["openid", "profile", "email"],
  "endpoints": {
    "authorization_endpoint": "https://idp.example.com/auth",
    "token_endpoint": "https://idp.example.com/token",
    "userinfo_endpoint": "https://idp.example.com/userinfo",
    "end_session_endpoint": "https://idp.example.com/logout"
  },
  "waldur_fields": [
    {
      "field": "first_name",
      "description": "User's first/given name",
      "suggested_claims": ["given_name", "first_name", "firstName"],
      "available_claims": ["given_name"]
    },
    {
      "field": "email",
      "description": "User's email address",
      "suggested_claims": ["email", "mail", "emailAddress"],
      "available_claims": ["email"]
    },
    {
      "field": "organization",
      "description": "User's organization/institution name",
      "suggested_claims": ["schac_home_organization", "schacHomeOrganization", "org"],
      "available_claims": ["schacHomeOrganization"]
    }
  ],
  "suggested_scopes": ["email", "openid", "profile"]
}
```

**Response Fields**:

| Field | Description |
|-------|-------------|
| `claims_supported` | Claims the OIDC provider can return (from discovery document) |
| `scopes_supported` | Scopes the OIDC provider supports |
| `endpoints` | OIDC endpoints extracted from discovery document |
| `waldur_fields` | Waldur User model fields with suggested OIDC claim mappings |
| `waldur_fields[].field` | Waldur User model field name |
| `waldur_fields[].description` | Human-readable field description |
| `waldur_fields[].suggested_claims` | Ordered list of OIDC claims that could map to this field |
| `waldur_fields[].available_claims` | Claims from this IdP that match the suggestions |
| `suggested_scopes` | Recommended scopes to request based on available claims |

### Generate Default Mapping

Generate a ready-to-use `attribute_mapping` configuration based on IdP's supported claims.

**Endpoint**: `POST /api/identity-providers/generate-mapping/`

**Permissions**: Staff only

**Request**:

```json
{
  "discovery_url": "https://idp.example.com/.well-known/openid-configuration",
  "verify_ssl": true
}
```

**Response**:

```json
{
  "attribute_mapping": {
    "first_name": "given_name",
    "last_name": "family_name",
    "email": "email",
    "organization": "schacHomeOrganization",
    "affiliations": "voperson_external_affiliation eduperson_scoped_affiliation"
  },
  "extra_scope": "email profile eduperson_assurance"
}
```

The generated mapping can be used directly when creating or updating an identity provider.

### Wizard Workflow

1. **Discover**: Call `discover_metadata` with the IdP's discovery URL
2. **Review**: Examine available claims and suggested mappings
3. **Generate**: Call `generate-mapping` to get a ready-to-use configuration
4. **Customize**: Modify the generated mapping if needed
5. **Create**: Use the mapping when creating the identity provider

### Example: Setting Up a New Identity Provider

```bash
# Step 1: Discover available claims
curl -X POST https://api.waldur.example.com/api/identity-providers/discover_metadata/ \
  -H "Authorization: Token YOUR_STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "discovery_url": "https://keycloak.example.com/.well-known/openid-configuration"
  }'

# Step 2: Generate mapping
curl -X POST https://api.waldur.example.com/api/identity-providers/generate-mapping/ \
  -H "Authorization: Token YOUR_STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "discovery_url": "https://keycloak.example.com/.well-known/openid-configuration"
  }'

# Step 3: Create identity provider with generated mapping
curl -X POST https://api.waldur.example.com/api/identity-providers/ \
  -H "Authorization: Token YOUR_STAFF_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "provider": "keycloak",
    "label": "Keycloak SSO",
    "client_id": "waldur-client",
    "client_secret": "your-secret",
    "discovery_url": "https://keycloak.example.com/.well-known/openid-configuration",
    "attribute_mapping": {
      "first_name": "given_name",
      "last_name": "family_name",
      "email": "email",
      "organization": "schacHomeOrganization"
    },
    "extra_scope": "email profile"
  }'
```

### Claim Mapping Priority

When generating suggestions, claims are prioritized in this order:

1. **Existing provider defaults**: Claims from Waldur's built-in provider configurations (TARA, eduTEAMS, Keycloak)
2. **Standard OIDC claims**: Common OIDC claim names (e.g., `given_name`, `email`)
3. **SCHAC/eduPerson claims**: Research and education federation attributes

This ensures generated mappings are compatible with known working configurations.

### Supported Waldur User Fields

The following user fields can be mapped from OIDC claims:

| Field | Common OIDC Claims | Description |
|-------|-------------------|-------------|
| `first_name` | `given_name` | User's first/given name |
| `last_name` | `family_name` | User's last/family name |
| `email` | `email`, `mail` | User's email address |
| `organization` | `schac_home_organization`, `org` | Organization name |
| `affiliations` | `voperson_external_affiliation` | Organizational affiliations |
| `civil_number` | `sub`, `schacPersonalUniqueID` | National identity number |
| `phone_number` | `phone_number` | Phone number |
| `birth_date` | `birthdate` | Date of birth |
| `gender` | `gender` | Gender (ISO 5218) |
| `nationality` | `schacCountryOfCitizenship` | Citizenship country code |
| `eduperson_assurance` | `eduperson_assurance` | Identity assurance level |

For a complete list, see [User Profile Attributes](user-profile-attributes.md).

### Notes on `claims_supported`

The `claims_supported` field in OIDC discovery is optional per the specification. If an IdP does not provide this field:

- `claims_supported` will be an empty array in the response
- `available_claims` for each Waldur field will also be empty
- You can still manually configure `attribute_mapping` based on IdP documentation

In such cases, consider performing a test authentication to discover actual claims returned by the IdP's userinfo endpoint.

## See Also

- [OIDC Authentication Configuration](../admin-guide/mastermind-configuration/configuration-guide.md#waldur_auth_social-plugin)
- [User Profile Attributes](user-profile-attributes.md)
