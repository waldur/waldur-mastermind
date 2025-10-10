# Automatic Company Validation

## Overview

Waldur's automatic company validation system verifies that users are authorized representatives of companies during onboarding by querying external business registries. This eliminates manual verification steps for users who can be automatically validated.

Currently supported:

- **Estonia**: Äriregister (Estonian Business Register)

The system is designed for easy extension to support additional countries and validation methods.

## How It Works

### 1. User Identity Validation

Before checking company authorization, the system validates that the user has the required identity information:

```text
User submits validation request
         ↓
Check user has civil_number (personal ID)
         ↓
    ┌────┴────┐
    ↓         ↓
  YES        NO
    ↓         ↓
Continue   FAILED
           (IDENTITY_VALIDATION_FAILED)
```

**Requirements by country:**

- **Estonia**: User must have `civil_number` (isikukood) in their profile
  - Typically obtained via TARA authentication through Keycloak
  - Estonian personal ID code (11 digits)

### 2. Backend Selection

The system automatically selects the appropriate validation backend based on the country code:

```python
# Example: User requests validation for Estonian company
country = "EE"
legal_person_identifier = "70000310"

# System finds EstonianAriregisterBackend
backend = backend_registry.find_backend_for_request(request)
```

**Backend Registry:**

- Maintains list of all available validation backends
- Each backend declares which countries it supports
- Backends can be prioritized if multiple options exist for one country

### 3. Company Registry Query

The selected backend queries the official business registry API:

**For Estonia (Äriregister):**

```text
Request to Äriregister API
         ↓
Provide credentials (username/password)
         ↓
Query company by registration code
         ↓
Receive list of company representatives
         ↓
Parse response and normalize data
```

**API Response includes:**

- Company legal name
- Registration status (active, dissolved, etc.)
- List of authorized representatives
- Each person's role and authority level
- Personal ID codes of representatives

### 4. Authorization Check

The system verifies if the requesting user is listed as an authorized representative:

```text
User's civil_number: 37906094930
         ↓
Compare with company representatives
         ↓
    ┌────┴────┐
    ↓         ↓
  MATCH    NO MATCH
    ↓         ↓
Check role   ESCALATED
    ↓
┌───┴───┐
↓       ↓
Has     No
authority authority
↓       ↓
VERIFIED ESCALATED
```

**Authorization criteria (Estonia):**

- User must be listed in company representatives
- User must have active representation rights
- For Äriregister: `ainuesindusoigus_olemas` must be "JAH" (yes) or user's role must be "ASES" (authorized representative)

### 5. Result Processing

The validation result determines the verification status:

| Backend Result | Verification Status | Next Step |
|---------------|-------------------|-----------|
| User authorized | `VERIFIED` | User can create customer immediately |
| User not found | `ESCALATED` | User must submit justification |
| User found but unauthorized | `ESCALATED` | User must submit justification |
| Company not found | `ESCALATED` | User must submit justification |
| API error | `ESCALATED` | User must submit justification |
| Configuration error | `FAILED` | Admin must configure backend |
| Identity missing | `FAILED` | User must link identity (e.g., TARA) |

## Validation Backends

### Backend Architecture

Each validation backend implements the `CompanyRegistryBackend` interface:

```python
class CompanyRegistryBackend(ABC):
    @classmethod
    @abstractmethod
    def get_supported_countries(cls) -> list[str]:
        """Return list of ISO country codes this backend supports."""
        pass

    @abstractmethod
    def validate_user_identity(self, user: User) -> tuple[bool, str]:
        """Validate user has required identity information."""
        pass

    @abstractmethod
    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        """Query registry and validate user authorization."""
        pass
```

### Estonian Äriregister Backend

**Configuration (Constance):**

```python
ONBOARDING_ARIREGISTER_BASE_URL = "https://ariregxmlv6.rik.ee/"
ONBOARDING_ARIREGISTER_USERNAME = "your_username"
ONBOARDING_ARIREGISTER_PASSWORD = "your_password"
ONBOARDING_ARIREGISTER_TIMEOUT = 30  # seconds
```

**Identity Requirements:**

- User must have `civil_number` field populated
- Format: 11-digit Estonian personal ID (isikukood)

**API Integration:**

- **Endpoint**: XML-based SOAP API
- **Authentication**: HTTP Basic Auth (username/password)
- **Request**: XML payload with company registration code
- **Response**: JSON with company and representative details
- **Timeout**: Configurable (default 30 seconds)

**Data Normalization:**

Raw API response is normalized to standard format:

```python
{
    "name": "Company Legal Name",
    "legal_person_identifier": "12345678",
    "status": "Active",
    "registry": "Estonian Business Register"
}
```

**Role Mapping:**

| Äriregister Role | Role Code | Authorized? |
|-----------------|-----------|-------------|
| Person with right to represent | ASES | ✅ Yes |
| Superior agency | KOAS | ❌ No (institutional role) |
| Board member with sole authority | ASES + ainuesindusoigus=JAH | ✅ Yes |
| Board member without sole authority | ASES + ainuesindusoigus=EI | ❌ No |

## API Endpoints

### Validate Company

**POST** `/api/onboarding-verifications/validate_company/`

Initiates company validation for the authenticated user.

**Request:**

```json
{
  "country": "EE",
  "legal_person_identifier": "70000310",
  "legal_name": "Optional Company Name",
  "user_submitted_customer_metadata": {
    "name": "Preferred customer name",
    "email": "contact@company.ee"
  }
}
```

**Response (Success - Verified):**

```json
{
  "uuid": "a1b2c3d4-...",
  "status": "verified",
  "validation_method": "ariregister",
  "verified_user_roles": ["ASES"],
  "verified_company_data": {
    "name": "Registrite ja Infosüsteemide Keskus",
    "legal_person_identifier": "70000310",
    "status": "Entered into the register",
    "registry": "Estonian Business Register"
  },
  "country": "EE",
  "legal_person_identifier": "70000310",
  "expires_at": "2025-10-11T12:00:00Z",
  "created": "2025-10-10T12:00:00Z"
}
```

**Response (Escalated - Manual Review):**

```json
{
  "uuid": "a1b2c3d4-...",
  "status": "escalated",
  "validation_method": "ariregister",
  "verified_user_roles": [],
  "error_message": "NOT_AUTHORIZED",
  "error_traceback": "User is not listed as authorized representative",
  "country": "EE",
  "legal_person_identifier": "70000310",
  "expires_at": "2025-10-11T12:00:00Z",
  "created": "2025-10-10T12:00:00Z"
}
```

**Response (Failed - Configuration Error):**

```json
{
  "uuid": "a1b2c3d4-...",
  "status": "failed",
  "validation_method": "",
  "error_message": "CONFIGURATION_ERROR",
  "error_traceback": "Äriregister credentials not configured",
  "country": "EE",
  "legal_person_identifier": "70000310",
  "created": "2025-10-10T12:00:00Z"
}
```

### Get Supported Countries

**GET** `/api/onboarding/supported-countries/`

Returns list of countries with available validation backends.

**Response:**

```json
{
  "supported_countries": ["EE"]
}
```

## Error Handling

### Error Codes

| Error Code | Meaning | Status | User Action |
|-----------|---------|--------|-------------|
| `NOT_AUTHORIZED` | User not authorized representative | `ESCALATED` | Submit justification |
| `COMPANY_NOT_FOUND` | Company not in registry | `ESCALATED` | Submit justification |
| `API_ERROR` | Registry API unavailable | `ESCALATED` | Retry later or submit justification |
| `IDENTITY_VALIDATION_FAILED` | User missing personal ID | `FAILED` | Link identity (e.g., TARA) |
| `CONFIGURATION_ERROR` | Backend not configured | `FAILED` | Contact administrator |
| `NO_BACKEND_AVAILABLE` | Country not supported | N/A | Request returns 400 error |

### Retry Logic

- **API timeouts**: Configured per backend (default 30s)
- **Network errors**: Caught and returned as `API_ERROR` with `ESCALATED` status
- **No automatic retries**: User must submit new validation request

## Data Storage

### OnboardingVerification Model

Stores all validation attempts and results:

```python
{
    "uuid": "unique-identifier",
    "user": User,
    "country": "EE",
    "legal_person_identifier": "70000310",
    "legal_name": "Optional name",
    "status": "verified|escalated|failed|expired|pending",
    "validation_method": "ariregister",

    # Results from validation
    "verified_user_roles": ["ASES"],
    "verified_company_data": {
        "name": "Company Name",
        "legal_person_identifier": "70000310",
        "status": "Active",
        "registry": "Estonian Business Register"
    },

    # Raw API response for debugging
    "raw_response": { ... },

    # Error details if validation failed
    "error_message": "NOT_AUTHORIZED",
    "error_traceback": "Detailed error message",

    # Timestamps
    "created": "2025-10-10T12:00:00Z",
    "validated_at": "2025-10-10T12:00:05Z",
    "expires_at": "2025-10-11T12:00:00Z",

    # Result
    "customer": null  # FK to Customer after creation
}
```

## Security Considerations

### Authentication & Authorization

1. **User Authentication**: User must be authenticated to submit validation
2. **Identity Verification**: Personal ID must be verified through trusted source (e.g., TARA)
3. **API Credentials**: Registry credentials stored securely in Constance configuration
4. **Rate Limiting**: Standard Waldur rate limits apply to API endpoints

### Data Privacy

1. **Personal Data**: Civil numbers (personal IDs) are stored only for authorized users
2. **API Responses**: Full raw responses stored for audit purposes
3. **Logging**: All validation attempts logged with user, company, and outcome
4. **Retention**: Verification records retained according to data retention policy

### Audit Trail

Every validation attempt is logged with:

- User who initiated validation
- Company details (country, registration code)
- Validation outcome (verified/escalated/failed)
- Timestamp
- Full API response (for debugging)

## Adding New Countries

To add support for a new country:

### 1. Create New Backend

```python
# src/waldur_core/onboarding/backends/new_country.py

from .base import CompanyRegistryBackend, ValidationRequest, ValidationResult

class NewCountryBackend(CompanyRegistryBackend):
    @classmethod
    def get_supported_countries(cls) -> list[str]:
        return ["XX"]  # ISO country code

    def validate_user_identity(self, user) -> tuple[bool, str]:
        # Check user has required identity field
        if not hasattr(user, 'national_id'):
            return False, "National ID required"
        return True, ""

    def validate_company(self, request: ValidationRequest) -> ValidationResult:
        # Query national business registry
        # Check if user is authorized representative
        # Return validation result
        pass
```

### 2. Register Backend

```python
# src/waldur_core/onboarding/backends/__init__.py

from .new_country import NewCountryBackend

backend_registry.register(NewCountryBackend)
```

### 3. Add Configuration

```python
# Add to constance configuration
NEW_COUNTRY_REGISTRY_API_URL = "..."
NEW_COUNTRY_REGISTRY_API_KEY = "..."
```

### 4. Update Documentation

- Add country to supported list
- Document identity requirements
- Document API integration details
- Add usage examples

## Testing

### Test Coverage

- ✅ Identity validation for each country
- ✅ Successful authorization checks
- ✅ Failed authorization (user not found)
- ✅ Failed authorization (user found but unauthorized)
- ✅ Company not found scenarios
- ✅ API errors and timeouts
- ✅ Configuration errors
- ✅ Backend selection logic
- ✅ Data normalization

### Running Tests

```bash
# Run all onboarding tests
pytest src/waldur_core/onboarding/tests/

# Run only automatic validation tests
pytest src/waldur_core/onboarding/tests/test_estonia_ariregister.py

# Run with verbose output
pytest src/waldur_core/onboarding/tests/test_estonia_ariregister.py -v
```

## Monitoring & Troubleshooting

### Metrics to Monitor

1. **Validation success rate**: % of validations that return `VERIFIED`
2. **Escalation rate**: % of validations that need manual review
3. **API error rate**: % of validations failing due to API issues
4. **Average response time**: Time from request to validation result

### Common Issues

#### "Configuration Error"

- Check Constance settings for backend credentials
- Verify API endpoint URL is correct
- Test credentials directly with registry API

#### "Identity Validation Failed"

- User needs to link TARA account (for Estonia)
- Check that identity provider is properly configured in Keycloak
- Verify `civil_number` field is being populated from TARA

#### High Escalation Rate

- Review common reasons for escalation
- Consider expanding authorization criteria
- Improve user guidance for edge cases

#### API Timeouts

- Increase timeout in Constance configuration
- Check network connectivity to registry
- Consider implementing retry logic
