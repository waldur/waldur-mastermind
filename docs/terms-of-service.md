# Terms of Service API Documentation

The Terms of Service (ToS) functionality enables service providers to define Terms of Service for their marketplace offerings and track user consent. If consent enforcement is active, users must accept the Terms of Service before accessing certain resources.

## Overview

The Terms of Service system consists of three main components:

1. **Terms of Service Configurations** - Service providers define ToS documents with versioning support
2. **User Consents** - Users grant consent to specific ToS versions for offerings
3. **Consent Enforcement** - System enforces consent requirements for resource access

## Key Features

- **Versioning**: Track different versions of Terms of Service
- **Re-consent Requirements**: Force users to re-consent when ToS is updated
- **Grace Periods**: Allow time for users to update consent before access is revoked
- **Consent Tracking**: Comprehensive tracking of user consents and revocations
- **Order Integration**: Require ToS acceptance during order creation

## Configuration

### Enabling ToS Enforcement

ToS consent enforcement is controlled by the `ENFORCE_USER_CONSENT_FOR_OFFERINGS` setting. When enabled, users must have active consent to access resources from offerings that:

- Have active Terms of Service configured
- Have `service_provider_can_create_offering_user` enabled in the offering's plugin options

## API Endpoints

### Terms of Service Management

Base URL: `/api/marketplace-offering-terms-of-service/`

#### List Terms of Service Configurations

Get all Terms of Service configurations visible to the current user.

```http
GET /api/marketplace-offering-terms-of-service/
Authorization: Token <token>
```

**Permissions:**

- **Staff/Support**: See all ToS configurations
- **Service Providers**: See ToS for their own offerings
- **Regular Users**: See ToS for offerings they've consented to or shared offerings

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `offering` | URL | Filter by offering URL |
| `offering_uuid` | UUID | Filter by offering UUID |
| `is_active` | Boolean | Filter by active status |
| `version` | String | Filter by version |
| `requires_reconsent` | Boolean | Filter by re-consent requirement |
| `o` | String | Order by (`created`, `-created`, `modified`, `-modified`, `version`, `-version`) |

**Example Request:**

```http
GET /api/marketplace-offering-terms-of-service/?offering_uuid=a1b2c3d4-e5f6-7890-1234-567890abcdef&is_active=true
```

**Response:**

```json
[
  {
    "url": "/api/marketplace-offering-terms-of-service/b2c3d4e5-f678-9012-3456-7890abcdef12/",
    "uuid": "b2c3d4e5-f678-9012-3456-7890abcdef12",
    "offering_uuid": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
    "offering_name": "Cloud VM Service",
    "terms_of_service": "<h1>Terms of Service</h1><p>By using this service...</p>",
    "terms_of_service_link": "https://example.com/tos",
    "version": "2.0",
    "is_active": true,
    "requires_reconsent": true,
    "grace_period_days": 60,
    "user_consent": {
      "uuid": "c3d4e5f6-7890-1234-5678-90abcdef1234",
      "version": "2.0",
      "agreement_date": "2024-01-15T10:30:00Z",
      "is_revoked": false
    },
    "has_user_consent": true,
    "created": "2024-01-10T09:00:00Z",
    "modified": "2024-01-15T14:20:00Z"
  }
]
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | UUID | Unique identifier for the ToS configuration |
| `offering_uuid` | UUID | UUID of the associated offering |
| `offering_name` | String | Name of the offering |
| `terms_of_service` | String (HTML) | The Terms of Service content (HTML formatted) |
| `terms_of_service_link` | URL | Optional external link to Terms of Service |
| `version` | String | Version identifier (e.g., "1.0", "2.0") |
| `is_active` | Boolean | Whether this ToS configuration is currently active |
| `requires_reconsent` | Boolean | Whether users must re-consent when this version is active |
| `grace_period_days` | Integer | Number of days before outdated consents are revoked (only when `requires_reconsent=True`) |
| `user_consent` | Object/null | Current user's consent information (if any) |
| `has_user_consent` | Boolean | Whether current user has valid consent for this ToS version |
| `created` | DateTime | When the ToS configuration was created |
| `modified` | DateTime | When the ToS configuration was last modified |

#### Retrieve a Terms of Service Configuration

Get details of a specific ToS configuration.

```http
GET /api/marketplace-offering-terms-of-service/<tos-uuid>/
Authorization: Token <token>
```

**Response:** Same structure as list endpoint, single object.

#### Create a Terms of Service Configuration

Create a new Terms of Service configuration for an offering.

```http
POST /api/marketplace-offering-terms-of-service/
Content-Type: application/json
Authorization: Token <service-provider-token>

{
  "offering": "/api/marketplace-provider-offerings/a1b2c3d4-e5f6-7890-1234-567890abcdef/",
  "terms_of_service": "<h1>Terms of Service</h1><p>By using this service, you agree to...</p>",
  "terms_of_service_link": "https://example.com/tos",
  "version": "2.0",
  "is_active": true,
  "requires_reconsent": true,
  "grace_period_days": 60
}
```

**Permissions Required:**

- `UPDATE_OFFERING` permission on the offering, its customer, or service provider

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `offering` | URL | Yes | URL to the offering |
| `terms_of_service` | String (HTML) | No | HTML content of the Terms of Service |
| `terms_of_service_link` | URL | No | External link to Terms of Service |
| `version` | String | No | Version identifier |
| `is_active` | Boolean | No | Whether to activate this ToS (default: `false`) |
| `requires_reconsent` | Boolean | No | Whether to require re-consent (default: `false`) |
| `grace_period_days` | Integer | No | Grace period in days (default: `60`, only used when `requires_reconsent=True`) |

**Validation Rules:**

- Only one active ToS configuration is allowed per offering
- If `is_active=true`, any existing active ToS for the offering must be deactivated first
- `version` and `requires_reconsent` cannot be changed after creation

**Response:** 201 Created with the created ToS configuration object.

#### Update a Terms of Service Configuration

Update an existing ToS configuration. This is intended for minor changes, major ToS changes must be done via creating a new ToS and requiring reconsent. Note that `version` and `requires_reconsent` are protected and cannot be changed.

```http
PATCH /api/marketplace-offering-terms-of-service/<tos-uuid>/
Content-Type: application/json
Authorization: Token <service-provider-token>

{
  "terms_of_service": "<h1>Updated Terms</h1><p>Revised terms...</p>",
  "terms_of_service_link": "https://example.com/tos-v2",
  "is_active": false,
  "grace_period_days": 90
}
```

**Permissions Required:**

- `UPDATE_OFFERING` permission on the offering's customer

**Updatable Fields:**

- `terms_of_service`
- `terms_of_service_link`
- `is_active`
- `grace_period_days`

**Protected Fields (cannot be changed):**

- `version`
- `requires_reconsent`

#### Delete a Terms of Service Configuration

Delete a ToS configuration. This is a hard delete.

```http
DELETE /api/marketplace-offering-terms-of-service/<tos-uuid>/
Authorization: Token <service-provider-token>
```

**Permissions Required:**

- `UPDATE_OFFERING` permission on the offering's customer

### User Consent Management

Base URL: `/api/marketplace-user-offering-consents/`

#### List User Consents

Get all consent records for the current user (or all consents for staff/support).

```http
GET /api/marketplace-user-offering-consents/
Authorization: Token <token>
```

**Permissions:**

- **Regular Users**: See only their own consents
- **Staff/Support**: See all consents

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `user` | URL | Filter by user URL |
| `user_uuid` | UUID | Filter by user UUID |
| `offering` | URL | Filter by offering URL |
| `offering_uuid` | UUID | Filter by offering UUID |
| `version` | String | Filter by ToS version |
| `has_consent` | Boolean | Filter by active consent status (`true` for active, `false` for revoked) |
| `requires_reconsent` | Boolean | Filter by whether re-consent is required |

**Example Request:**

```http
GET /api/marketplace-user-offering-consents/?offering_uuid=a1b2c3d4-e5f6-7890-1234-567890abcdef&has_consent=true
```

**Response:**

```json
[
  {
    "url": "/api/marketplace-user-offering-consents/c3d4e5f6-7890-1234-5678-90abcdef1234/",
    "uuid": "c3d4e5f6-7890-1234-5678-90abcdef1234",
    "user": "/api/users/d4e5f678-9012-3456-7890-abcdef123456/",
    "user_uuid": "d4e5f678-9012-3456-7890-abcdef123456",
    "username": "johndoe",
    "offering": "/api/marketplace-provider-offerings/a1b2c3d4-e5f6-7890-1234-567890abcdef/",
    "offering_uuid": "a1b2c3d4-e5f6-7890-1234-567890abcdef",
    "offering_name": "Cloud VM Service",
    "agreement_date": "2024-01-15T10:30:00Z",
    "version": "2.0",
    "revocation_date": null,
    "is_revoked": false,
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T10:30:00Z"
  }
]
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `uuid` | UUID | Unique identifier for the consent record |
| `user_uuid` | UUID | UUID of the user who granted consent |
| `username` | String | Username of the consenting user |
| `offering_uuid` | UUID | UUID of the offering |
| `offering_name` | String | Name of the offering |
| `agreement_date` | DateTime | When the consent was granted |
| `version` | String | Version of ToS that was consented to |
| `revocation_date` | DateTime/null | When the consent was revoked (if revoked) |
| `is_revoked` | Boolean | Whether the consent has been revoked |
| `created` | DateTime | When the consent record was created |
| `modified` | DateTime | When the consent record was last modified |

#### Retrieve a User Consent

Get details of a specific consent record.

```http
GET /api/marketplace-user-offering-consents/<consent-uuid>/
Authorization: Token <token>
```

**Response:** Same structure as list endpoint, single object.

#### Grant Consent to Terms of Service

Create a consent record for the current user and a specific offering.

```http
POST /api/marketplace-user-offering-consents/
Content-Type: application/json
Authorization: Token <user-token>

{
  "offering": "a1b2c3d4-e5f6-7890-1234-567890abcdef"
}
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `offering` | UUID | Yes | UUID of the offering |

**Validation:**

- The offering must have active Terms of Service
- If user already has active consent for the current ToS version, returns an error
- If user has revoked consent, it will be reactivated with the current ToS version

**Response:** 201 Created with the consent record.

**Behavior:**

- If consent already exists (even if revoked), it will be reactivated and updated with the current ToS version
- The consent version is automatically set to match the active ToS version

#### Revoke Consent

Revoke a user's consent to Terms of Service.

```http
POST /api/marketplace-user-offering-consents/<consent-uuid>/revoke/
Authorization: Token <user-token>
```

**Permissions:**

- Users can revoke their own consent
- Staff can revoke any consent

**Response:** 200 OK with updated consent record (now with `revocation_date` set).

### Offering Statistics

#### Get ToS Consent Statistics

Get comprehensive consent statistics for a specific offering.

```http
GET /api/marketplace-provider-offerings/<offering-uuid>/tos_stats/
Authorization: Token <service-provider-token>
```

**Permissions Required:**

- `UPDATE_OFFERING` permission on the offering or its customer

**Response:**

```json
{
  "active_users_count": 150,
  "total_users_count": 200,
  "active_users_percentage": 75.0,
  "accepted_consents_count": 180,
  "revoked_consents_count": 20,
  "total_consents_count": 200,
  "revoked_consents_over_time": [
    {
      "date": "2024-01-15",
      "count": 5
    },
    {
      "date": "2024-01-16",
      "count": 3
    }
  ],
  "tos_version_adoption": [
    {
      "version": "2.0",
      "users_count": 120
    },
    {
      "version": "1.0",
      "users_count": 60
    }
  ],
  "active_users_over_time": [
    {
      "date": "2024-01-15",
      "count": 145
    },
    {
      "date": "2024-01-16",
      "count": 150
    }
  ]
}
```

**Field Descriptions:**

| Field | Type | Description |
|-------|------|-------------|
| `active_users_count` | Integer | Number of users with active consent |
| `total_users_count` | Integer | Total number of users for the offering |
| `active_users_percentage` | Float | Percentage of users with active consent |
| `accepted_consents_count` | Integer | Total number of accepted consents |
| `revoked_consents_count` | Integer | Total number of revoked consents |
| `total_consents_count` | Integer | Total number of consent records |
| `revoked_consents_over_time` | Array | Time series of revoked consents |
| `tos_version_adoption` | Array | Distribution of users across ToS versions |
| `active_users_over_time` | Array | Time series of active users |

### Order Integration

When creating an order for an offering with Terms of Service, you must include the `accepting_terms_of_service` field.

#### Create Order with ToS Acceptance

```http
POST /api/marketplace-orders/
Content-Type: application/json
Authorization: Token <user-token>

{
  "offering": "/api/marketplace-public-offerings/a1b2c3d4-e5f6-7890-1234-567890abcdef/",
  "project": "/api/projects/b2c3d4e5-f678-9012-3456-7890abcdef12/",
  "plan": "/api/marketplace-public-offerings/a1b2c3d4-e5f6-7890-1234-567890abcdef/plans/c3d4e5f678901234567890abcdef1234/",
  "attributes": {
    "name": "My Resource"
  },
  "accepting_terms_of_service": true
}
```

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `accepting_terms_of_service` | Boolean | Conditional | Must be `true` if offering has ToS |

**Validation:**

- If the offering has active Terms of Service, `accepting_terms_of_service` must be `true`
- If provided as `true`, a consent record is automatically created for the user
- If the user already has active consent, the order proceeds normally

## Workflows

### Service Provider: Setting Up Terms of Service

1. **Create ToS Configuration**

   ```http
   POST /api/marketplace-offering-terms-of-service/
   {
     "offering": "/api/marketplace-provider-offerings/<uuid>/",
     "terms_of_service": "<h1>Terms</h1><p>Content...</p>",
     "version": "1.0",
     "is_active": true,
     "requires_reconsent": false
   }
   ```

2. **Update ToS (Requiring Re-consent)**

   ```http
   # First, deactivate current ToS
   PATCH /api/marketplace-offering-terms-of-service/<current-tos-uuid>/
   {
     "is_active": false
   }

   # Create new ToS version
   POST /api/marketplace-offering-terms-of-service/
   {
     "offering": "/api/marketplace-provider-offerings/<uuid>/",
     "terms_of_service": "<h1>Updated Terms</h1><p>New content...</p>",
     "version": "2.0",
     "is_active": true,
     "requires_reconsent": true,
     "grace_period_days": 60
   }
   ```

3. **Monitor Consent Statistics**

   ```http
   GET /api/marketplace-provider-offerings/<offering-uuid>/tos_stats/
   ```

### User: Granting Consent

1. **Check if Offering Requires ToS**

   ```http
   GET /api/marketplace-public-offerings/<offering-uuid>/
   ```

   Check the `has_terms_of_service` field in the response.

2. **View Terms of Service**

   ```http
   GET /api/marketplace-offering-terms-of-service/?offering_uuid=<uuid>&is_active=true
   ```

3. **Grant Consent**

   ```http
   POST /api/marketplace-user-offering-consents/
   {
     "offering": "<offering-uuid>"
   }
   ```

4. **Create Order (Consent Included)**

   ```http
   POST /api/marketplace-orders/
   {
     "offering": "...",
     "project": "...",
     "accepting_terms_of_service": true
   }
   ```

### User: Re-consenting After ToS Update

1. **Check Consent Status**

   ```http
   GET /api/marketplace-user-offering-consents/?offering_uuid=<uuid>
   ```

   Check if `requires_reconsent` filter returns the consent.

2. **View Updated ToS**

   ```http
   GET /api/marketplace-offering-terms-of-service/?offering_uuid=<uuid>&is_active=true
   ```

3. **Grant New Consent**

   ```http
   POST /api/marketplace-user-offering-consents/
   {
     "offering": "<offering-uuid>"
   }
   ```

   This will update the existing consent with the new version.

## Permission Model

### Terms of Service Management

- **Create/Update/Delete ToS**: Requires `UPDATE_OFFERING` permission on:
  - The offering itself, OR
  - The offering's customer, OR
  - The offering's customer's service provider

### User Consent

- **View Consents**:
  - Users can see their own consents
  - Staff/Support can see all consents
- **Grant Consent**: Users can grant consent for themselves
- **Revoke Consent**:
  - Users can revoke their own consent
  - Staff can revoke any consent

## Grace Periods

When `requires_reconsent=True` is set on a ToS configuration:

1. **Grace Period**: Users have `grace_period_days` (default: 60) to update their consent
2. **During Grace Period**: Users retain access even with outdated consent
3. **After Grace Period**: Users lose access if consent version doesn't match active ToS version
4. **Automatic Enforcement**: The system checks consent version when accessing resources

## Best Practices

### For Service Providers

1. **Version Management**
   - Use semantic versioning (e.g., "1.0", "2.0", "2.1")
   - Document changes between versions
   - Set appropriate grace periods for major updates
   - Major ToS revisions require creating a new ToS object

2. **Re-consent Strategy**
   - Use `requires_reconsent=true` for significant changes
   - Provide adequate grace periods (60+ days recommended)
   - Communicate ToS updates to users proactively

3. **Content Guidelines**
   - Keep Terms of Service clear and concise
   - Use HTML formatting for better readability
   - Consider providing both inline content and external link

4. **Monitoring**
   - Regularly check consent statistics
   - Monitor grace period expirations
   - Follow up with users who haven't re-consented

## Related Endpoints

- **Offerings**: `/api/marketplace-provider-offerings/` - Check `has_terms_of_service` field
- **Orders**: `/api/marketplace-orders/` - Include `accepting_terms_of_service` when creating orders
- **Resources**: Resource access is automatically enforced based on consent status

## Configuration Settings

- `ENFORCE_USER_CONSENT_FOR_OFFERINGS`: Global setting to enable/disable ToS consent enforcement
- Only applies to offerings with `service_provider_can_create_offering_user` enabled in plugin options

