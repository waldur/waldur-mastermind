# Customer Onboarding - Complete Guide


## Overview

The Customer Onboarding system allows users to register their organizations in Waldur through two methods:

1. **Automatic Validation** - Real-time verification via business registry APIs
2. **Manual Validation** - Staff review with supporting documentation (fallback when automatic fails)

### Key Features

- ✅ Multi-country support (Estonia, Austria, Sweden, Norway)
- ✅ Extensible backend system for adding new countries
- ✅ Flexible checklist-based data collection
- ✅ Automatic fallback to manual validation
- ✅ Field mapping to Customer model
- ✅ Intent/purpose metadata collection
- ✅ Document upload support
- ✅ Email notifications
- ✅ Automatic expiration handling

---

## Architecture & Structure

### Directory Structure

```
src/waldur_core/onboarding/
├── __init__.py
├── models.py                    # Core data models
├── views.py                     # API endpoints
├── serializers.py               # Request/response serialization
├── enums.py                     # Status, methods, decisions
├── tasks.py                     # Celery background tasks
├── validators.py                # Business logic validation
├── filters.py                   # Query filtering
├── urls.py                      # URL routing
├── config.py                    # Settings configuration
├── backends/
│   ├── __init__.py             # Backend registry
│   ├── base.py                 # Abstract base classes
│   ├── estonia.py              # Estonian Äriregister
│   ├── austria.py              # Austrian WirtschaftsCompass
│   ├── sweden.py               # Swedish Bolagsverket
│   └── norway.py               # Norwegian Brønnøysund
└── tests/
    └── ...                      # Test files
```

### Core Models

#### 1. OnboardingVerification

The main model tracking the verification process.

```python
class OnboardingVerification:
    user                       # User requesting onboarding
    country                    # ISO country code (EE, AT, SE, NO)
    legal_person_identifier    # Company registration code
    legal_name                 # Company name
    status                     # pending/verified/failed/escalated/expired
    validation_method          # ariregister/wirtschaftscompass/bolagsverket/breg
    verified_user_roles        # Roles user has in company
    verified_company_data      # Normalized company information
    raw_response               # Raw API response (for debugging)
    validated_at               # Timestamp of validation
    expires_at                 # Expiration timestamp
    customer                   # Created Customer (after approval)
```

**Key Methods:**
- `get_or_create_checklist_completion(checklist_type)` - Gets/creates checklist
- `extract_data_from_checklist()` - Extracts answers from checklists
- `can_customer_be_created()` - Validates if customer creation is possible
- `create_customer_if_verified()` - Creates Customer from verification

#### 2. OnboardingQuestionMetadata

Maps checklist questions to Customer fields or intent metadata.

```python
class OnboardingQuestionMetadata:
    question                   # Foreign key to Question
    maps_to_customer_field     # Customer model field name (e.g., 'email', 'address')
    intent_field               # Intent metadata field (e.g., 'intent', 'goals')
```

**Example Mappings:**
- Question "Contact email" → `maps_to_customer_field='email'`
- Question "Purpose" → `intent_field='intent'`

#### 3. OnboardingJustification

Manual validation request with supporting documentation.

```python
class OnboardingJustification:
    verification               # Related verification
    user                       # User submitting justification
    user_justification         # Text explanation
    validated_by               # Staff who reviewed
    validated_at               # Review timestamp
    validation_decision        # approved/rejected/pending
    staff_notes                # Admin notes
```

#### 4. OnboardingJustificationDocumentation

File uploads for manual validation.

```python
class OnboardingJustificationDocumentation:
    justification              # Related justification
    file                       # Uploaded file
```

### Backend System

The backend system is **extensible** - designed to easily add new countries and validation methods.

#### Base Classes

**`ValidationRequest`** - Standardized input:
```python
@dataclass
class ValidationRequest:
    country: str                              # ISO code
    person_identifier: str | dict             # User's ID
    legal_person_identifier: str | None       # Company code
    legal_name: str | None                    # Company name
    additional_params: dict                   # Extra data
```

**`ValidationResult`** - Standardized output:
```python
@dataclass
class ValidationResult:
    is_valid: bool                            # Success/failure
    method_used: str                          # Backend name
    company_data: dict                        # Company info
    user_roles: list[str]                     # User's roles
    raw_response: dict                        # API response (keha only, no credentials)
    error_code: str | None                    # Error type
    error_message: str | None                 # Error details
```

**`CompanyRegistryBackend`** - Abstract base:
```python
class CompanyRegistryBackend(ABC):
    @abstractmethod
    def get_supported_countries() -> set[str]

    @abstractmethod
    def get_validation_method() -> str

    @abstractmethod
    def get_required_fields() -> list[str]

    @abstractmethod
    def get_person_identifier_fields() -> dict

    @abstractmethod
    def validate(request: ValidationRequest) -> ValidationResult
```

#### Available Backends

| Backend | Country | Method | API |
|---------|---------|--------|-----|
| `EstonianAriregisterBackend` | Estonia (EE) | `ariregister` | e-Business Register |
| `AustriaRegisterBackend` | Austria (AT) | `wirtschaftscompass` | WirtschaftsCompass |
| `SwedenRegisterBackend` | Sweden (SE) | `bolagsverket` | Bolagsverket |
| `NorwayRegisterBackend` | Norway (NO) | `breg` | Brønnøysundregistrene |

#### Backend Registry

```python
# backends/__init__.py
backend_registry = BackendRegistry()

# Auto-discovery of backends
backend_registry.register(EstonianAriregisterBackend)
backend_registry.register(AustriaRegisterBackend)
backend_registry.register(SwedenRegisterBackend)
backend_registry.register(NorwayRegisterBackend)

# Usage
backend = backend_registry.get_backend(validation_method='ariregister')
result = backend.validate(request)
```

#### Adding New Countries

To add a new country (e.g., Finland):

1. **Create backend file**: `backends/finland.py`

```python
from .base import CompanyRegistryBackend, ValidationRequest, ValidationResult

class FinlandRegisterBackend(CompanyRegistryBackend):
    @classmethod
    def get_supported_countries(cls) -> set[str]:
        return {"FI"}

    @classmethod
    def get_validation_method(cls) -> str:
        return "ytj"  # Finnish Patent and Registration Office

    @classmethod
    def get_required_fields(cls) -> list[str]:
        return ["legal_person_identifier", "person_identifier"]

    @classmethod
    def get_person_identifier_fields(cls) -> dict:
        return {
            "type": "string",
            "label": "Finnish Personal Identity Code",
            "help_text": "Format: DDMMYYXXXX"
        }

    @classmethod
    def validate(cls, request: ValidationRequest) -> ValidationResult:
        # Implement YTJ API integration
        ...
```

2. **Register backend**: Add to `backends/__init__.py`

```python
from .finland import FinlandRegisterBackend

backend_registry.register(FinlandRegisterBackend)
```

3. **Add enum**: Update `enums.py`

```python
class ValidationMethod:
    ...
    YTJ = "ytj"

    CHOICES = (
        ...
        (YTJ, "Finnish Business Register (YTJ)"),
    )
```

4. **Done!** The new backend is automatically available via API.

---

## Automatic Validation

### How It Works

1. **User initiates** - Provides company registration code and selects validation method
2. **Backend selected** - System routes to appropriate country backend
3. **API call** - Backend queries business registry API
4. **Verification** - Checks if user is authorized to represent the company
5. **Result** - Updates verification with success/failure status

### Validation Flow

```
┌─────────────────────────────────────────────────────────────┐
│  User: "I want to register company X"                        │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 1: start_verification                                  │
│  POST /api/onboarding/verifications/start_verification/     │
│  {                                                            │
│    "validation_method": "ariregister",                       │
│    "legal_person_identifier": "12345678",                    │
│    "legal_name": "Company Name OÜ",                          │
│    "country": "EE"                                            │
│  }                                                            │
│  → Creates OnboardingVerification (status=PENDING)           │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 2: Complete checklists (optional)                      │
│  POST /api/onboarding/verifications/{uuid}/submit_answers/  │
│  → User answers intent questions (purpose, goals, etc.)      │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Step 3: run_validation                                      │
│  POST /api/onboarding/verifications/{uuid}/run_validation/  │
│  {                                                            │
│    "civil_number": "38904032767"  # User's personal ID       │
│  }                                                            │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────────────────┐
│  Backend Execution:                                          │
│  1. backend_registry.get_backend('ariregister')              │
│  2. backend.validate(ValidationRequest(...))                 │
│  3. API call to business registry                            │
│  4. Parse response                                           │
│  5. Check user authorization                                 │
│  6. Return ValidationResult                                  │
└────────────────────┬────────────────────────────────────────┘
                     │
                     ├─ SUCCESS ──────┐
                     │                 ▼
                     │    ┌───────────────────────────────┐
                     │    │  Verification.status=VERIFIED  │
                     │    │  - verified_company_data       │
                     │    │  - verified_user_roles         │
                     │    │  - validated_at                │
                     │    └───────────────────────────────┘
                     │
                     └─ FAILURE ──────┐
                                       ▼
                          ┌───────────────────────────────┐
                          │  Verification.status=FAILED   │
                          │  - error_code                 │
                          │  - error_message              │
                          │  → User can create            │
                          │    justification for manual   │
                          │    validation                 │
                          └───────────────────────────────┘
```

### Success Scenario

**Example: Estonian Company**

Request:
```json
POST /api/onboarding/verifications/{uuid}/run_validation/
{
  "civil_number": "38904032767"
}
```

Backend Process:
1. Query Äriregister API with company code `14684114`
2. Receive company data (name, status, persons)
3. Check if `civil_number` matches authorized person
4. Find user roles (board member, etc.)

Response (verification updated):
```json
{
  "uuid": "abc-def-ghi",
  "status": "verified",
  "validation_method": "ariregister",
  "verified_company_data": {
    "ariregistri_kood": 14684114,
    "arinimi": "Hepsor N170 OÜ",
    "staatus_tekstina": "Entered into the register",
    "oiguslik_vorm_tekstina": "Private limited company"
  },
  "verified_user_roles": ["Management board member"],
  "validated_at": "2026-01-27T10:30:00Z"
}
```

### Failure Scenarios

#### 1. User Not Authorized

```json
{
  "status": "failed",
  "error_code": "NOT_AUTHORIZED",
  "error_message": "User is not authorized to represent this company"
}
```

**What happens:**
- User's personal ID not found in company's authorized persons
- User can create justification with explanation

#### 2. Company Not Found

```json
{
  "status": "failed",
  "error_code": "COMPANY_NOT_FOUND",
  "error_message": "Company with registration code 12345678 not found"
}
```

**What happens:**
- Invalid or non-existent registration code
- User should verify the code or use manual validation

#### 3. API Error

```json
{
  "status": "failed",
  "error_code": "API_ERROR",
  "error_message": "External service unavailable. Please try again later."
}
```

**What happens:**
- Business registry API is down or timeout
- User can retry later or use manual validation

#### 4. Configuration Error

```json
{
  "status": "failed",
  "error_code": "CONFIGURATION_ERROR",
  "error_message": "Missing required API credentials for this validation method"
}
```

**What happens:**
- Backend not properly configured
- Admin must configure settings (API keys, endpoints)

### Security Considerations

**⚠️ Important: Raw Response Sanitization**

The `raw_response` field stores API responses for debugging. To prevent credential exposure:

```python
# WRONG - Exposes credentials
return response_data  # Contains 'paring' section with passwords

# CORRECT - Only return business data
keha = response_data.get("keha", {})
return {"keha": keha}  # No credentials exposed
```

All backends should return **only the business data section**, not the full API response including request parameters (which may contain credentials).

---

## Manual Validation (Fallback)

When automatic validation fails or is unavailable, users can request manual review by staff.

### When to Use Manual Validation

1. **Automatic validation failed** - User not found in registry but has authority
2. **New company** - Not yet in public registry
3. **Special cases** - Complex organizational structures
4. **No automatic method** - Country not supported
5. **API unavailable** - Technical issues with registry

### Manual Validation Flow

```
┌──────────────────────────────────────────────────────────┐
│  Automatic Validation Failed (status=FAILED)              │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────┐
│  Step 1: User creates justification                       │
│  POST /api/onboarding/justifications/create_justification/│
│  {                                                         │
│    "verification_uuid": "abc-def",                        │
│    "user_justification": "I am the CEO but not yet..."   │
│  }                                                         │
│  → Verification.status changes to ESCALATED               │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────┐
│  Step 2: User uploads supporting documents                │
│  POST /api/onboarding/justification-documentation/       │
│  {                                                         │
│    "justification_uuid": "xyz-123",                       │
│    "file": <company_registry_extract.pdf>                │
│  }                                                         │
│  → Multiple files can be uploaded                         │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────┐
│  Step 3: User completes BOTH checklists                   │
│  - ONBOARDING_CUSTOMER_DATA: email, address, VAT, etc.   │
│  - ONBOARDING_INTENT_DATA: purpose, goals, description   │
│  POST /api/onboarding/verifications/{uuid}/submit_answers/│
│  → Must complete ALL required questions                   │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ▼
┌──────────────────────────────────────────────────────────┐
│  Staff Review Process                                     │
│  1. List pending justifications (filter: pending)         │
│  2. Review:                                               │
│     - User explanation                                    │
│     - Uploaded documents                                  │
│     - Checklist answers                                   │
│     - Company information                                 │
│  3. Decision: Approve or Reject                           │
└────────────────────┬─────────────────────────────────────┘
                     │
                     ├─ APPROVE ──────┐
                     │                 ▼
                     │    ┌──────────────────────────────┐
                     │    │  POST .../review_justification/│
                     │    │  {                            │
                     │    │    "decision": "approved",    │
                     │    │    "staff_notes": "..."       │
                     │    │  }                            │
                     │    │  → Verification.status=VERIFIED│
                     │    │  → Email sent to user         │
                     │    └──────────────────────────────┘
                     │
                     └─ REJECT ───────┐
                                       ▼
                          ┌──────────────────────────────┐
                          │  POST .../review_justification/│
                          │  {                            │
                          │    "decision": "rejected",    │
                          │    "staff_notes": "..."       │
                          │  }                            │
                          │  → Verification.status=FAILED │
                          │  → Email sent to user         │
                          └──────────────────────────────┘
```

### Justification Review (Staff)

**List Pending Justifications:**
```bash
GET /api/onboarding/justifications/?validation_decision=pending
```

**Review Justification:**
```bash
POST /api/onboarding/justifications/{uuid}/review_justification/
{
  "decision": "approved",  # or "rejected"
  "staff_notes": "Verified company registration extract. User is listed as CEO."
}
```

**What happens on approval:**
1. `justification.validation_decision` = `approved`
2. `verification.status` = `verified`
3. Email notification sent to user
4. User can now create customer

**What happens on rejection:**
1. `justification.validation_decision` = `rejected`
2. `verification.status` = `failed`
3. Email notification sent to user
4. User must resubmit or provide more evidence

### Document Requirements

Staff should verify:
- ✅ **Company registration document** - Proves company exists
- ✅ **Authorization proof** - User has authority (board appointment, power of attorney)
- ✅ **Identity verification** - User is who they claim to be
- ✅ **Recent date** - Documents are current (not expired)

Common acceptable documents:
- Company registry extract
- Board appointment certificate
- Power of attorney
- Signed company documents
- Government-issued ID

---

## Checklist System

Checklists collect additional data during onboarding. Two types exist:

### 1. ONBOARDING_CUSTOMER_DATA

**Purpose:** Collect data that maps to Customer model fields.

**When used:**
- **Manual validation**: Required (provides customer data)
- **Automatic validation**: Optional/skipped (data comes from registry)

**Example Questions:**
```json
{
  "description": "Contact email",
  "question_type": "email",
  "required": true,
  "onboarding_metadata": {
    "maps_to_customer_field": "email",
    "intent_field": ""
  }
},
{
  "description": "Company address",
  "question_type": "text_input",
  "required": false,
  "onboarding_metadata": {
    "maps_to_customer_field": "address",
    "intent_field": ""
  }
},
{
  "description": "VAT code",
  "question_type": "text_input",
  "required": false,
  "onboarding_metadata": {
    "maps_to_customer_field": "vat_code",
    "intent_field": ""
  }
}
```

**Field Mappings:**

Customer model fields that can be mapped:
- `name`, `native_name`, `abbreviation`
- `email`, `phone_number`, `contact_details`
- `address`, `postal`
- `vat_code`, `registration_code`, `backend_id`
- `bank_name`, `bank_account`
- `homepage`, `domain`
- `agreement_number`, `sponsor_number`

### 2. ONBOARDING_INTENT_DATA

**Purpose:** Understand why user wants to create organization.

**When used:**
- **All validation types**: Required

**Example Questions:**
```json
{
  "description": "Purpose of creating an organization",
  "question_type": "multi_select",
  "required": true,
  "options": [
    {"label": "HPC Resources"},
    {"label": "Training & Education"},
    {"label": "Proof of Concept"}
  ],
  "onboarding_metadata": {
    "maps_to_customer_field": "",
    "intent_field": "intent"
  }
},
{
  "description": "Organization description",
  "question_type": "text_area",
  "required": true,
  "onboarding_metadata": {
    "maps_to_customer_field": "",
    "intent_field": "description"
  }
},
{
  "description": "Goals",
  "question_type": "text_area",
  "required": false,
  "onboarding_metadata": {
    "maps_to_customer_field": "",
    "intent_field": "goals"
  }
}
```

**Intent Metadata:**

Intent data is stored as `onboarding_metadata` in verification, not in Customer model:
```python
verification.get_onboarding_metadata_display()
# Returns:
{
  "intent": "HPC Resources, Training & Education",
  "description": "Research institution needing compute resources",
  "goals": "Run climate simulations"
}
```

### Customizing Checklists

**Via Admin Interface:**
1. Navigate to Checklists section
2. Find "ONBOARDING_CUSTOMER_DATA" or "ONBOARDING_INTENT_DATA"
3. Add/edit questions
4. Set `OnboardingQuestionMetadata` for field mappings

**Via API:**
```bash
# Create question
POST /api/checklists/questions/
{
  "checklist": "<checklist-uuid>",
  "description": "Company website",
  "question_type": "text_input",
  "required": false
}

# Create field mapping
POST /api/onboarding/question-metadata/
{
  "question": "<question-uuid>",
  "maps_to_customer_field": "homepage"
}
```

### Checklist Completion Rules

**Automatic Validation:**
- ✅ ONBOARDING_INTENT_DATA: **Required** (must complete)
- ⚠️ ONBOARDING_CUSTOMER_DATA: **Optional** (data from registry)

**Manual Validation:**
- ✅ ONBOARDING_INTENT_DATA: **Required** (must complete)
- ✅ ONBOARDING_CUSTOMER_DATA: **Required** (must complete)

**Why the difference?**
- Automatic validation gets company data from registry API
- Manual validation relies on user-provided data

---

## Status States & Transitions

### Verification Status

```
┌─────────┐
│ PENDING │  Initial state after creation
└────┬────┘
     │
     ├──► run_validation() ──┐
     │                        │
     │                        ├──► VERIFIED   (automatic validation success)
     │                        │
     │                        └──► FAILED     (automatic validation failed)
     │
     └──► create_justification() ──► ESCALATED  (manual review requested)
          │
          └──► staff approval ──┐
                                 │
                                 ├──► VERIFIED   (approved)
                                 │
                                 └──► FAILED     (rejected)

EXPIRED  ← expires_at passed (background task)
```

**Status Meanings:**

| Status | Meaning | Next Steps |
|--------|---------|------------|
| `PENDING` | Created, awaiting validation | Run automatic validation |
| `VERIFIED` | Successfully verified | Create customer |
| `FAILED` | Validation/review failed | Create justification or give up |
| `ESCALATED` | Manual review requested | Staff reviews justification |
| `EXPIRED` | Timed out without completion | Start over |

### Justification Decision

```
PENDING  →  staff review  →  APPROVED  (verification → VERIFIED)
                          →  REJECTED  (verification → FAILED)
```

### Customer Creation Rules

Customer can only be created when:
1. ✅ `verification.status` = `VERIFIED`
2. ✅ No customer exists for this verification
3. ✅ **Automatic validation**: Intent checklist completed
4. ✅ **Manual validation**: Both checklists completed
5. ✅ No duplicate `registration_code` exists

---

## API Reference

### Endpoints Overview

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/api/onboarding/verifications/start_verification/` | POST | Create verification |
| `/api/onboarding/verifications/{uuid}/run_validation/` | POST | Run automatic validation |
| `/api/onboarding/verifications/{uuid}/checklist/` | GET | Get checklist questions |
| `/api/onboarding/verifications/{uuid}/submit_answers/` | POST | Submit checklist answers |
| `/api/onboarding/verifications/{uuid}/create_customer/` | POST | Create customer |
| `/api/onboarding/verifications/available_checklists/` | GET | Preview checklists |
| `/api/onboarding/justifications/create_justification/` | POST | Request manual review |
| `/api/onboarding/justifications/{uuid}/review_justification/` | POST | Staff review (approve/reject) |
| `/api/onboarding/justification-documentation/` | POST | Upload documents |
| `/api/onboarding/validation-methods/` | GET | List available validation methods |
| `/api/onboarding/person-identifier-fields/` | GET | Get required fields per method |

### Key API Examples

#### 1. Start Verification (Automatic)

```http
POST /api/onboarding/verifications/start_verification/
Content-Type: application/json

{
  "validation_method": "ariregister",
  "legal_person_identifier": "12345678",
  "legal_name": "Acme OÜ",
  "country": "EE"
}
```

Response:
```json
{
  "uuid": "abc-def-ghi",
  "user": "user-uuid",
  "status": "pending",
  "validation_method": "ariregister",
  "legal_person_identifier": "12345678",
  "legal_name": "Acme OÜ",
  "country": "EE",
  "created": "2026-01-27T10:00:00Z",
  "expires_at": "2026-02-03T10:00:00Z"
}
```

#### 2. Run Validation

```http
POST /api/onboarding/verifications/abc-def-ghi/run_validation/
Content-Type: application/json

{
  "civil_number": "38904032767"
}
```

Success Response:
```json
{
  "uuid": "abc-def-ghi",
  "status": "verified",
  "validated_at": "2026-01-27T10:05:00Z",
  "verified_company_data": {
    "ariregistri_kood": 12345678,
    "arinimi": "Acme OÜ",
    "staatus_tekstina": "Entered into the register",
    "oiguslik_vorm_tekstina": "Private limited company"
  },
  "verified_user_roles": ["Management board member"]
}
```

Failure Response:
```json
{
  "uuid": "abc-def-ghi",
  "status": "failed",
  "error_code": "NOT_AUTHORIZED",
  "error_message": "Person with civil number 38904032767 is not authorized to represent company 12345678"
}
```

#### 3. Get Checklist

```http
GET /api/onboarding/verifications/abc-def-ghi/checklist/?checklist_type=intent
```

Response:
```json
{
  "uuid": "checklist-uuid",
  "name": "Intent & Purpose",
  "questions": [
    {
      "uuid": "q1-uuid",
      "description": "Purpose of creating an organization",
      "question_type": "multi_select",
      "required": true,
      "options": [
        {"uuid": "opt1", "label": "HPC Resources"},
        {"uuid": "opt2", "label": "Training & Education"}
      ],
      "answer": null
    }
  ],
  "is_completed": false,
  "completion_percentage": 0
}
```

#### 4. Submit Answers

```http
POST /api/onboarding/verifications/abc-def-ghi/submit_answers/
Content-Type: application/json

[
  {
    "question_uuid": "q1-uuid",
    "answer_data": ["opt1", "opt2"]
  },
  {
    "question_uuid": "q2-uuid",
    "answer_data": "We need HPC resources for research"
  }
]
```

#### 5. Create Justification

```http
POST /api/onboarding/justifications/create_justification/
Content-Type: application/json

{
  "verification_uuid": "abc-def-ghi",
  "user_justification": "I am the CEO of this company but not yet registered in the business registry. I can provide company registration documents and my appointment letter."
}
```

Response:
```json
{
  "uuid": "just-123-uuid",
  "verification": "abc-def-ghi",
  "user": "user-uuid",
  "user_justification": "I am the CEO...",
  "validation_decision": "pending",
  "created": "2026-01-27T10:15:00Z"
}
```

#### 6. Upload Documentation

```http
POST /api/onboarding/justification-documentation/
Content-Type: multipart/form-data

justification_uuid: just-123-uuid
file: <company_registration.pdf>
```

#### 7. Review Justification (Staff)

```http
POST /api/onboarding/justifications/just-123-uuid/review_justification/
Content-Type: application/json

{
  "decision": "approved",
  "staff_notes": "Verified company registration document. User is listed as CEO. Documents are valid and recent."
}
```

Response:
```json
{
  "uuid": "just-123-uuid",
  "validation_decision": "approved",
  "validated_by": "staff-uuid",
  "validated_at": "2026-01-27T11:00:00Z",
  "staff_notes": "Verified company registration...",
  "verification": {
    "status": "verified"
  }
}
```

#### 8. Create Customer

```http
POST /api/onboarding/verifications/abc-def-ghi/create_customer/
```

Response:
```json
{
  "uuid": "customer-uuid",
  "name": "Acme OÜ",
  "registration_code": "12345678",
  "email": "contact@acme.ee",
  "address": "Tallinn, Estonia",
  "country": "EE",
  "owners": [
    {
      "user_uuid": "user-uuid",
      "role": "CUSTOMER.OWNER"
    }
  ]
}
```

---

## Configuration

### Settings (constance)

| Setting | Default | Description |
|---------|---------|-------------|
| `ONBOARDING_VERIFICATION_EXPIRY_HOURS` | -       | Hours until verification expires |
| `ARIREGISTER_USERNAME` | -       | Estonian Äriregister API username |
| `ARIREGISTER_PASSWORD` | -       | Estonian Äriregister API password |
| `WIRTSCHAFTSCOMPASS_API_KEY` | -       | Austrian API key |
| `BOLAGSVERKET_API_KEY` | -       | Swedish API key |
| `BRREG_API_KEY` | -       | Norwegian API key |

### Backend Configuration

Each backend checks for required credentials:

```python
# Estonia
if not config.ARIREGISTER_USERNAME or not config.ARIREGISTER_PASSWORD:
    raise ValueError("ARIREGISTER credentials not configured")

# Austria
if not config.WIRTSCHAFTSCOMPASS_API_KEY:
    raise ValueError("WIRTSCHAFTSCOMPASS_API_KEY not configured")
```

### Background Tasks

Configured in Celery:

```python
# Hourly: Expire stale verifications
@periodic_task(run_every=crontab(minute=0))
def expire_stale_verifications():
    # Marks PENDING/ESCALATED verifications past expires_at as EXPIRED

# Daily: Delete old verifications
@periodic_task(run_every=crontab(hour=2, minute=0))
def delete_old_verifications():
    # Deletes FAILED/EXPIRED verifications older than 30 days
```

### Email Templates

Email notifications use these templates:

- `onboarding/justification_review_notification` - Sent when staff approves/rejects

Template context:
```python
{
  "user_full_name": "John Doe",
  "organization_name": "Acme OÜ",
  "created_at": "2026-01-27 10:00",
  "site_name": "Waldur",
  "link_to_homeport_dashboard": "https://app.waldur.com/profile/onboarding-applications/"
}
```

---

## Troubleshooting

### Common Issues

#### 1. "No backend available for method X"

**Problem:** Validation method not supported or backend not registered.

**Solution:**
```python
# Check available methods
GET /api/onboarding/validation-methods/

# Verify backend registration
from waldur_core.onboarding.backends import backend_registry
print(backend_registry.get_available_methods())
```

#### 2. "Configuration error: Missing API credentials"

**Problem:** Backend credentials not configured.

**Solution:**
```bash
# Set in constance settings
ARIREGISTER_USERNAME=your_username
ARIREGISTER_PASSWORD=your_password
```

#### 3. "Checklist has required fields that are not completed"

**Problem:** User trying to create customer without completing checklists.

**Solution:**
```python
# Check completion status
GET /api/onboarding/verifications/{uuid}/checklist/?checklist_type=customer
GET /api/onboarding/verifications/{uuid}/checklist/?checklist_type=intent

# Complete missing answers
POST /api/onboarding/verifications/{uuid}/submit_answers/
```

#### 4. "Customer with registration code X already exists"

**Problem:** Duplicate registration code.

**Solution:**
- Verify if user should join existing customer instead
- If legitimate new customer, use different registration code
- If error, admin should investigate duplicate

#### 5. Verification expired

**Problem:** User took too long (> 7 days by default).

**Solution:**
- User must start over with new verification
- Adjust `ONBOARDING_VERIFICATION_EXPIRY_HOURS` if needed

#### 6. API timeout or unavailable

**Problem:** Business registry API is down or slow.

**Solution:**
- Retry later (transient issue)
- Fall back to manual validation
- Check backend configuration (timeout settings)

### Debugging

**Enable detailed logging:**
```python
# settings.py
LOGGING = {
    'loggers': {
        'waldur_core.onboarding': {
            'level': 'DEBUG',
        },
    },
}
```

**Check verification state:**
```python
from waldur_core.onboarding.models import OnboardingVerification

verification = OnboardingVerification.objects.get(uuid='abc-def')
print(f"Status: {verification.status}")
print(f"Error: {verification.error_message}")
print(f"Raw response: {verification.raw_response}")
print(f"Validated at: {verification.validated_at}")
```

**Check backend response:**
```python
from waldur_core.onboarding.backends import backend_registry, ValidationRequest

backend = backend_registry.get_backend('ariregister')
request = ValidationRequest(
    country='EE',
    person_identifier='38904032767',
    legal_person_identifier='12345678'
)
result = backend.validate(request)
print(f"Valid: {result.is_valid}")
print(f"Error: {result.error_message}")
print(f"Raw: {result.raw_response}")
```

### Performance Optimization

**Database queries:**
```python
# Use select_related for related objects
verifications = OnboardingVerification.objects.select_related(
    'user', 'customer'
).prefetch_related('justifications')

# Index frequently queried fields
class Meta:
    indexes = [
        models.Index(fields=['status', 'created']),
        models.Index(fields=['legal_person_identifier', 'country']),
    ]
```

**API response caching:**
```python
# Cache checklist questions (rarely change)
@method_decorator(cache_page(60 * 60))  # 1 hour
def available_checklists(self, request):
    ...
```
