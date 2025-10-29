# Complete Customer Onboarding Guide: From Setup to Customer Creation

This guide walks through the entire customer onboarding process using the checklist-based system, from initial configuration to customer creation.

---

## 1. Setting Up a Country Checklist

Before users can onboard, staff must create a country-specific checklist.

### Example: Estonia Onboarding Setup

#### Step 1.1: Create the Checklist

Navigate to **Administration → Checklist management → Add Checklist**

```text
Name: Estonia SME Onboarding
Description: Onboarding form for Estonian companies with automatic validation via Äriregister
Checklist Type: Customer onboarding
Country: EE
```

**Save the checklist.**

#### Step 1.2: Create Country Configuration

Navigate to **Administration → Organizations → Country Checklist Configurations → Add**

```text
Country: EE
Checklist: Estonia SME Onboarding (select from dropdown)
Is Active: ✓ Yes
```

**Save the configuration.**

This configuration maps the country code "EE" to your checklist. When a user starts onboarding with `country="EE"`, the system will automatically use this checklist.

---

#### Step 1.3: Add Questions

Now add questions to the checklist. Questions must be added in order.

##### Question 1: Primary Contact Email

```text
Description: Primary Contact Email
Question Type: Text input
Required: ✓ Yes
Order: 1

User Guidance:
This email will be used for important notifications and communications.
```

**Add OnboardingQuestionMetadata:**

```text
Question: Primary Contact Email
Maps to Customer Field: email
Intent Field: (leave empty)
```

**Explanation:**

- `maps_to_customer_field: "email"` - Saved to `Customer.email`
- Used for customer communication

---

##### Question 2: VAT Code (Estonia-specific, optional)

```text
Description: VAT Registration Number (KMKR)
Question Type: Text input
Required: ✗ No
Order: 2

User Guidance:
Your Estonian VAT registration number in format EEXXXXXXXXX.
Leave empty if your company is not VAT registered.
```

**Add OnboardingQuestionMetadata:**

```text
Question: VAT Registration Number (KMKR)
Maps to Customer Field: vat_code
Intent Field: (leave empty)
```

**Explanation:**

- Only `maps_to_customer_field` - Goes directly to `Customer.vat_code`
- Not used for validation, just stored

---

##### Question 3: Intended Use (Multi-select)

```text
Description: What is your intended use of the platform?
Question Type: Multi select
Required: ✓ Yes
Order: 3

User Guidance:
Select all that apply. This helps us understand your needs and provide appropriate support.
```

**Add Question Options:**

1. **Option 1:**
   - Label: `Research & Development`
   - Order: `1`

2. **Option 2:**
   - Label: `Commercial Operations`
   - Order: `2`

3. **Option 3:**
   - Label: `Educational Purposes`
   - Order: `3`

4. **Option 4:**
   - Label: `Government Services`
   - Order: `4`

5. **Option 5:**
   - Label: `Other`
   - Order: `5`

**Add OnboardingQuestionMetadata:**

```text
Question: What is your intended use of the platform?
Maps to Customer Field: (leave empty)
Intent Field: intent
```

**Explanation:**

- `intent_field: "intent"` - Stored with verification, NOT in Customer model
- Multi-select allows multiple choices
- Accessible via `verification.onboarding_metadata['intent']`

---

##### Question 4: Contact Phone Number

```text
Description: Contact Phone Number
Question Type: Text input
Required: ✗ No
Order: 4

User Guidance:
Include country code (e.g., +372 1234567)
```

**Add OnboardingQuestionMetadata:**

```text
Question: Contact Phone Number
Maps to Customer Field: phone_number
Intent Field: (leave empty)
```

---

##### Question 5: Company Address

```text
Description: Legal Business Address
Question Type: Text area
Required: ✗ No
Order: 5

User Guidance:
Full business address as registered in official documents.
```

**Add OnboardingQuestionMetadata:**

```text
Question: Legal Business Address
Maps to Customer Field: address
Intent Field: (leave empty)
```

---

### Summary: Estonia Checklist Structure

```text
Estonia SME Onboarding (country=EE)
├── Q1: Primary Contact Email
│   └── Metadata: Customer.email
├── Q2: VAT Code (optional)
│   └── Metadata: Customer.vat_code
├── Q3: Intended Use (multi-select, required)
│   └── Metadata: intent_field="intent" (stays with verification)
├── Q4: Phone Number (optional)
│   └── Metadata: Customer.phone_number
└── Q5: Company Address (optional)
    └── Metadata: Customer.address
```

**Important Changes:**

- ✅ **Checklist is now optional**: Countries can enable automatic validation without configuring a checklist
- ✅ **Checklist for supplemental data only**: Email, VAT code, intent, contact info, etc.

**Note:** Metadata is configured via the **OnboardingQuestionMetadata** model in Django Admin. The `verification_field` concept has been removed - verification fields are provided directly in API requests, not extracted from checklists.

---

## 2. User Onboarding Flow

Now let's walk through what happens when a user onboards.

### Step 2.1: User Initiates Onboarding

**Frontend Action:** User clicks "Register Company" and selects country "Estonia", then provides company registration code and name

**API Call:**

```http
POST /api/onboarding-verifications/start_verification/
Authorization: Token <user_token>
Content-Type: application/json

{
  "country": "EE",
  "legal_person_identifier": "12345678",
  "legal_name": "Example Technologies OÜ"
}
```

**Response (201 Created):**

```json
{
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "user": "http://localhost:8000/api/users/a1b2c3d4.../",
  "country": "EE",
  "legal_person_identifier": "12345678",
  "legal_name": "Example Technologies OÜ",
  "status": "pending",
  "validation_method": "",
  "verified_user_roles": [],
  "verified_company_data": {},
  "onboarding_metadata": {},
  "customer": null,
  "created": "2025-10-24T10:30:00Z",
  "modified": "2025-10-24T10:30:00Z"
}
```

**What happened:**

- OnboardingVerification created with status="pending"
- `legal_person_identifier` and `legal_name` stored immediately on the verification object
- ChecklistCompletion created if checklist is configured for country (optional)
- User receives verification UUID to continue

**Note:** If no checklist is configured for the country, user can proceed directly to validation (Step 2.4) without submitting additional answers.

---

### Step 2.2: User Fetches Onboarding Form (Optional)

**Note:** This step is optional. If no checklist is configured for the country, users can skip directly to validation.

**API Call:**

```http
GET /api/onboarding-verifications/550e8400-e29b-41d4-a716-446655440000/checklist/
Authorization: Token <user_token>
```

**Response (200 OK):**

```json
{
  "checklist": {
    "uuid": "c1c2c3c4-e5e6-47e8-a9a0-b1b2b3b4b5b6",
    "name": "Estonia SME Onboarding",
    "description": "Onboarding form for Estonian companies with automatic validation via Äriregister",
    "checklist_type": "customer_onboarding",
    "country": "EE"
  },
  "questions": [
    {
      "uuid": "q1111111-1111-1111-1111-111111111111",
      "description": "Primary Contact Email",
      "user_guidance": "This email will be used for important notifications and communications.",
      "question_type": "text_input",
      "required": true,
      "order": 1,
      "existing_answer": null,
      "question_options": [],
      "min_value": null,
      "max_value": null
    },
    {
      "uuid": "q2222222-2222-2222-2222-222222222222",
      "description": "VAT Registration Number (KMKR)",
      "user_guidance": "Your Estonian VAT registration number in format EEXXXXXXXXX...",
      "question_type": "text_input",
      "required": false,
      "order": 2,
      "existing_answer": null,
      "question_options": [],
      "min_value": null,
      "max_value": null
    },
    {
      "uuid": "q3333333-3333-3333-3333-333333333333",
      "description": "What is your intended use of the platform?",
      "user_guidance": "Select all that apply. This helps us understand your needs...",
      "question_type": "multi_select",
      "required": true,
      "order": 3,
      "existing_answer": null,
      "question_options": [
        {
          "uuid": "o1111111-1111-1111-1111-111111111111",
          "label": "Research & Development",
          "order": 1
        },
        {
          "uuid": "o2222222-2222-2222-2222-222222222222",
          "label": "Commercial Operations",
          "order": 2
        },
        {
          "uuid": "o3333333-3333-3333-3333-333333333333",
          "label": "Educational Purposes",
          "order": 3
        },
        {
          "uuid": "o4444444-4444-4444-4444-444444444444",
          "label": "Government Services",
          "order": 4
        },
        {
          "uuid": "o5555555-5555-5555-5555-555555555555",
          "label": "Other",
          "order": 5
        }
      ],
      "min_value": null,
      "max_value": null
    },
    {
      "uuid": "q4444444-4444-4444-4444-444444444444",
      "description": "Contact Phone Number",
      "user_guidance": "Include country code (e.g., +372 1234567)",
      "question_type": "text_input",
      "required": false,
      "order": 4,
      "existing_answer": null,
      "question_options": [],
      "min_value": null,
      "max_value": null
    },
    {
      "uuid": "q5555555-5555-5555-5555-555555555555",
      "description": "Legal Business Address",
      "user_guidance": "Full business address as registered in official documents.",
      "question_type": "text_area",
      "required": false,
      "order": 5,
      "existing_answer": null,
      "question_options": [],
      "min_value": null,
      "max_value": null
    }
  ],
  "completion_percentage": 0,
  "is_completed": false
}
```

**What the frontend does:**

- Dynamically renders form based on questions
- Shows multi-select for intents
- Marks required fields
- User fills out the supplemental information

---

### Step 2.3: User Submits Answers (Optional)

**Note:** This step is only needed if a checklist exists. Users can skip this if no checklist is configured.

**API Call:**

```http
POST /api/onboarding-verifications/550e8400-e29b-41d4-a716-446655440000/submit_answers/
Authorization: Token <user_token>
Content-Type: application/json

[
  {
    "question_uuid": "q1111111-1111-1111-1111-111111111111",
    "answer_data": "contact@example.ee"
  },
  {
    "question_uuid": "q2222222-2222-2222-2222-222222222222",
    "answer_data": "EE123456789"
  },
  {
    "question_uuid": "q3333333-3333-3333-3333-333333333333",
    "answer_data": ["Research & Development", "Commercial Operations"]
  },
  {
    "question_uuid": "q4444444-4444-4444-4444-444444444444",
    "answer_data": "+372 1234567"
  },
  {
    "question_uuid": "q5555555-5555-5555-5555-555555555555",
    "answer_data": "Tallinn, Harju County, 10111, Estonia"
  }
]
```

**Response (200 OK):**

```json
{
  "message": "Answers submitted successfully",
  "completion_percentage": 100,
  "is_completed": true
}
```

**What happened:**

- All answers stored in Answer table linked to ChecklistCompletion
- Completion status updated to 100%
- Ready for validation

---

## 3. Automatic Validation

### Step 3.1: Trigger Validation

After submitting answers (or immediately if no checklist), user triggers automatic validation.

**API Call:**

```http
POST /api/onboarding-verifications/550e8400-e29b-41d4-a716-446655440000/run_validation/
Authorization: Token <user_token>
```

**What happens internally:**

1. **Use verification data:**

   ```python
   # legal_person_identifier and legal_name are already on the verification object
   # from the start_verification request
   legal_person_identifier = verification.legal_person_identifier  # "12345678"
   legal_name = verification.legal_name  # "Example Technologies OÜ"
   ```

2. **Find validation backend:**

   ```python
   backend = backend_registry.find_backend_for_request(
       ValidationRequest(
           country="EE",
           person_identifier=user.civil_number,
           legal_person_identifier="12345678",
           legal_name="Example Technologies OÜ"
       )
   )
   # Uses EstonianAriregisterBackend
   ```

3. **Validate user identity:**

   ```python
   identity_valid, error = backend.validate_user_identity(user)
   # Checks if user has Estonian civil_number
   ```

4. **Query Äriregister API:**

   ```python
   result = backend_registry.validate_company(request)
   # Calls Estonian Business Register API
   # Checks if user is authorized to represent company
   ```

5. **Update verification:**

   ```python
   verification.status = "verified"  # or "escalated"
   verification.verified_user_roles = ["board_member", "authorized_representative"]
   verification.verified_company_data = {
       "name": "Example Technologies OÜ",
       "registration_code": "12345678",
       "status": "active"
   }
   verification.save()
   ```

---

### Step 3.2: Successful Validation Response

**Response (200 OK):**

```json
{
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "user": "http://localhost:8000/api/users/a1b2c3d4.../",
  "country": "EE",
  "legal_person_identifier": "12345678",
  "legal_name": "Example Technologies OÜ",
  "status": "verified",
  "validation_method": "ariregister",
  "verified_user_roles": ["board_member", "authorized_representative"],
  "verified_company_data": {
    "name": "Example Technologies OÜ",
    "registration_code": "12345678",
    "status": "active",
    "vat_number": "EE123456789"
  },
  "onboarding_metadata": {
    "intent": ["Research & Development", "Commercial Operations"]
  },
  "raw_response": { /* Full API response */ },
  "error_message": "",
  "validated_at": "2025-10-24T10:35:00Z",
  "expires_at": "2025-10-25T10:35:00Z",
  "customer": null,
  "created": "2025-10-24T10:30:00Z",
  "modified": "2025-10-24T10:35:00Z"
}
```

**Key fields:**

- ✅ `status`: "verified" - Automatic validation succeeded
- ✅ `verified_user_roles`: User is authorized representative
- ✅ `onboarding_metadata`: Intent visible here, not on Customer

---

### Step 3.3: Failed Validation Response (Escalated)

If validation fails (user not authorized, API error, etc.):

**Response (200 OK):**

```json
{
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "status": "escalated",
  "validation_method": "ariregister",
  "verified_user_roles": [],
  "verified_company_data": {},
  "error_message": "USER_NOT_AUTHORIZED",
  "error_traceback": "User is not listed as authorized representative in Äriregister",
  "validated_at": "2025-10-24T10:35:00Z",
  ...
}
```

**Next step:** User must provide justification (see Manual Review section)

---

## 4. Manual Review Process

When automatic validation fails, users can submit a justification for manual review.

### Step 4.1: User Submits Justification

**API Call:**

```http
POST /api/onboarding-justifications/create_justification/
Authorization: Token <user_token>
Content-Type: application/json

{
  "verification_uuid": "550e8400-e29b-41d4-a716-446655440000",
  "user_justification": "I am a member of the research group affiliated with this company. Our group is conducting a research project funded by the Estonian Research Council. While I am not listed as a board member, I have authorization from the company director to register on behalf of our research team."
}
```

**Response (201 Created):**

```json

{
  "uuid": "j1j2j3j4-j5j6-j7j8-j9j0-jajbjcjdjejf",
  "verification": "http://localhost:8000/api/onboarding-verifications/550e8400.../",
  "user": "http://localhost:8000/api/users/a1b2c3d4.../",
  "legal_person_identifier": "12345678",
  "legal_name": "Example Technologies OÜ",
  "user_justification": "I am a member of the research group...",
  "validation_decision": "pending",
  "validated_by": null,
  "validated_at": null,
  "staff_notes": "",
  "supporting_documentation": [],
  "created": "2025-10-24T10:40:00Z"
}
```

---

### Step 4.2: Upload Supporting Documents (Optional)

**API Call:**

```http
POST /api/onboarding-justifications/j1j2j3j4-j5j6-j7j8-j9j0-jajbjcjdjejf/attach_document/
Authorization: Token <user_token>
Content-Type: multipart/form-data

file: <authorization_letter.pdf>
```

**Response (201 Created):**

```json
{
  "uuid": "d1d2d3d4-d5d6-d7d8-d9d0-dadbdcdddedf",
  "file": "/media/onboarding_justification_documentation/authorization_letter.pdf",
  "file_name": "authorization_letter.pdf",
  "file_size": 245678,
  "created": "2025-10-24T10:42:00Z"
}
```

---

### Step 4.3: Staff Reviews Justification

Staff member reviews in Django Admin or via API:

**View Justification Details:**

- Verification info: company, user, validation results
- User's justification text
- Checklist answers (including intents)
- Supporting documents

**Staff Decision:**

```http
POST /api/onboarding-justifications/j1j2j3j4-j5j6-j7j8-j9j0-jajbjcjdjejf/approve/
Authorization: Token <staff_token>
Content-Type: application/json

{
  "staff_notes": "Verified authorization."
}
```

**Response (200 OK):**

```json
{
  "uuid": "j1j2j3j4-j5j6-j7j8-j9j0-jajbjcjdjejf",
  "validation_decision": "approved",
  "validated_by": "http://localhost:8000/api/users/staff123.../",
  "validated_at": "2025-10-24T14:00:00Z",
  "staff_notes": "Verified authorization.",
}
```

**What happened:**

- Justification status → "approved"
- **Verification status → "verified"** ← Now eligible for customer creation!

---

## 5. Customer Creation

Once verification status is "verified" (either automatic or manual approval), user can create customer.

### Step 5.1: Create Customer from Verification

**API Call:**

```http
POST /api/onboarding-verifications/550e8400-e29b-41d4-a716-446655440000/create_customer/
Authorization: Token <user_token>
```

**What happens internally:**

```python
# 1. Check eligibility
# - Status must be "verified" ✓
# - Customer doesn't exist yet ✓
# - No duplicate registration_code ✓
# - If checklist exists, it must be completed ✓

# 2. Extract supplemental data from checklist (if exists)
extracted = verification.extract_data_from_checklist()
# Returns:
# {
#   'customer_data': {
#     'email': 'contact@example.ee',
#     'vat_code': 'EE123456789',
#     'phone_number': '+372 1234567',
#     'address': 'Tallinn, Harju County, 10111, Estonia'
#   },
#   'onboarding_metadata': {
#     'intent': ['Research & Development', 'Commercial Operations']
#   }
# }

# 3. Merge with verified_company_data
customer_data = {
    # Prioritize API data from business registry
    "name": verified_company_data.get("name") or verification.legal_name,
    "country": "EE",
    "registration_code": verification.legal_person_identifier,
    # Add checklist data
    "vat_code": "EE123456789",
    "email": "contact@example.ee",
    "phone_number": "+372 1234567",
    "address": "Tallinn, Harju County, 10111, Estonia"
}

# 4. Create Customer
customer = Customer.objects.create(**customer_data)

# 5. Link to verification
verification.customer = customer
verification.save()
```

**Response (201 Created):**

```json
{
  "uuid": "c1c2c3c4-c5c6-c7c8-c9c0-cacbcccdcecf",
  "url": "http://localhost:8000/api/customers/c1c2c3c4.../",
  "name": "Example Technologies OÜ",
  "abbreviation": "",
  "native_name": "",
  "registration_code": "12345678",
  "vat_code": "EE123456789",
  "country": "EE",
  "email": "contact@example.ee",
  "phone_number": "+372 1234567",
  "address": "Tallinn, Harju County, 10111, Estonia",
  "created": "2025-10-24T14:05:00Z",
  "owners": [
    {
      "uuid": "a1b2c3d4-a5a6-a7a8-a9a0-aaabacadaeaf",
      "username": "user@example.com",
      "full_name": "John Doe"
    }
  ],
  "projects": []
}
```

**Key points:**

- ✅ Customer created with data from verification + checklist
- ✅ API-verified data prioritized over user input
- ✅ User who verified becomes owner
- ✅ If checklist has required fields, they must be completed first
- ❌ Intent NOT in Customer model (stays with verification)

**Important:** If a checklist exists with required fields and the user hasn't completed them, customer creation will fail with:

```json
{
  "detail": "Cannot create customer: checklist has required fields that are not completed. Please complete all required checklist questions before creating a customer."
}
```

---

### Step 5.2: View Verification with Customer Link

**API Call:**

```http
GET /api/onboarding-verifications/550e8400-e29b-41d4-a716-446655440000/
Authorization: Token <user_token>
```

**Response:**

```json
{
  "uuid": "550e8400-e29b-41d4-a716-446655440000",
  "status": "verified",
  "customer": "http://localhost:8000/api/customers/c1c2c3c4.../",
  "onboarding_metadata": {
    "intent": ["Research & Development", "Commercial Operations"]
  },
  ...
}
```

- Customer object with business data
- Verification object with onboarding context (intents)
- Link between them via `verification.customer`

---

## 6. Complete API Examples

### Scenario A: Successful Automatic Validation (Estonia)

```bash
# Step 1: Start verification with required fields
curl -X POST http://localhost:8000/api/onboarding-verifications/start_verification/ \
  -H "Authorization: Token abc123" \
  -H "Content-Type: application/json" \
  -d '{
    "country": "EE",
    "legal_person_identifier": "12345678",
    "legal_name": "Example Technologies OÜ"
  }'

# Response: {"uuid": "550e8400...", "status": "pending", "legal_person_identifier": "12345678"}

# Step 2: Get form (optional - only if checklist configured)
curl http://localhost:8000/api/onboarding-verifications/550e8400.../checklist/ \
  -H "Authorization: Token abc123"

# Response: {"checklist": {...}, "questions": [...]}

# Step 3: Submit answers (optional - only if checklist exists)
curl -X POST http://localhost:8000/api/onboarding-verifications/550e8400.../submit_answers/ \
  -H "Authorization: Token abc123" \
  -H "Content-Type: application/json" \
  -d '[
    {"question_uuid": "q111...", "answer_data": "info@example.ee"},
    {"question_uuid": "q222...", "answer_data": "EE123456789"},
    {"question_uuid": "q333...", "answer_data": ["Research & Development"]}
  ]'

# Step 4: Run validation (can be done immediately after Step 1 if no checklist)
curl -X POST http://localhost:8000/api/onboarding-verifications/550e8400.../run_validation/ \

# Response: {"status": "verified", "verified_user_roles": ["board_member"]}

# Step 5: Create customer
curl -X POST http://localhost:8000/api/onboarding-verifications/550e8400.../create_customer/ \
  -H "Authorization: Token abc123"

# Response: {"uuid": "c1c2c3c4...", "name": "Example OÜ", ...}
```

---

### Scenario B: Manual Review Required

```bash
# Steps 1-4: Same as above, but validation returns:
# {"status": "escalated", "error_message": "USER_NOT_AUTHORIZED"}

# Step 5: Submit justification
curl -X POST http://localhost:8000/api/onboarding-justifications/create_justification/ \
  -H "Authorization: Token abc123" \
  -H "Content-Type: application/json" \
  -d '{
    "verification_uuid": "550e8400...",
    "user_justification": "I am authorized by company director..."
  }'

# Response: {"uuid": "j1j2j3j4...", "validation_decision": "pending"}

# Step 6: Upload document
curl -X POST http://localhost:8000/api/onboarding-justifications/j1j2j3j4.../attach_document/ \
  -H "Authorization: Token abc123" \
  -F "file=@authorization.pdf"

# Step 7: Staff approves (staff token required)
curl -X POST http://localhost:8000/api/onboarding-justifications/j1j2j3j4.../approve/ \
  -H "Authorization: Token staff_token" \
  -H "Content-Type: application/json" \
  -d '{"staff_notes": "Authorization verified. Approved."}'

# Response: {"validation_decision": "approved"}

# Step 8: Check verification status
curl http://localhost:8000/api/onboarding-verifications/550e8400.../ \
  -H "Authorization: Token abc123"

# Response: {"status": "verified"}  ← Now can create customer!

# Step 9: Create customer
curl -X POST http://localhost:8000/api/onboarding-verifications/550e8400.../create_customer/ \
  -H "Authorization: Token abc123"
```

---

## Summary

### Data Flow Diagram

```text
1. START VERIFICATION
   └─> OnboardingVerification created (status=pending)
       └─> CountryChecklistConfiguration queried by country
           └─> Checklist found
               └─> ChecklistCompletion created

2. GET CHECKLIST
   └─> Returns country-specific questions

3. SUBMIT ANSWERS
   └─> Answer objects created
       └─> OnboardingQuestionMetadata defines how to map answers:
           ├─> verification_field → Used for validation
           ├─> maps_to_customer_field → Extracted for Customer model
           └─> intent_field → Stored in onboarding_metadata

4. RUN VALIDATION
   ├─> Extract legal_person_identifier from checklist via OnboardingQuestionMetadata
   ├─> Call validation backend (e.g., Äriregister)
   └─> Update verification.status
       ├─> "verified" → Ready for customer creation
       └─> "escalated" → Requires manual review
           └─> CREATE JUSTIFICATION
               └─> Staff reviews → Approve/Reject
                   └─> Approved → status="verified"

5. CREATE CUSTOMER
   ├─> Extract data from checklist answers using OnboardingQuestionMetadata mappings
   ├─> Merge with verified_company_data from API
   ├─> Create Customer with essential fields
   └─> Link: verification.customer → Customer
       ├─> Customer has: name, registration_code, email, etc.
       └─> Verification has: onboarding_metadata (intents)
```

### Key Architecture Components

#### 1. CountryChecklistConfiguration

- Maps ISO country codes to checklists
- Lives in onboarding app (not in generic checklist system)
- Allows multiple checklists per country (toggle with is_active)
- Example: Country "EE" → "Estonia SME Onboarding" checklist

#### 2. OnboardingQuestionMetadata

- Defines how question answers should be used
- Three mapping types:
  - `verification_field` - For validation (legal_person_identifier, legal_name)
  - `maps_to_customer_field` - For Customer model (registration_code, email, etc.)
  - `intent_field` - For verification metadata (intent, purpose)
- Lives in onboarding app (not in generic checklist system)
- Configured per question in Django Admin

#### 3. Generic Checklist System

- Remains completely generic and reusable
- No onboarding-specific logic
- Used for project compliance, proposals, offerings, and onboarding

### Key Takeaways

✅ **Flexible per country** - Each checklist completely independent

✅ **Checklist is truly optional** - Countries can enable automatic validation without configuring a checklist

✅ **Required fields in API request** - `legal_person_identifier` and `legal_name` provided directly in `start_verification`, not through checklist

✅ **Essential data → Customer** - email, vat_code, phone_number, address (from checklist)

✅ **Context → Verification** - intents, purposes, onboarding metadata (from checklist)

✅ **No pollution** - Generic checklist stays generic, onboarding logic in onboarding app

✅ **Clean separation** - CountryChecklistConfiguration and OnboardingQuestionMetadata isolate onboarding concerns

✅ **Staff configurable** - Add countries and configure mappings without code changes

✅ **Two validation paths** - Automatic + manual fallback

✅ **Complete audit trail** - All answers preserved in checklist

✅ **Checklist completion enforced** - If checklist exists with required fields, they must be completed before customer creation

✅ **User-friendly** - No errors if checklist not configured; works seamlessly with or without checklists

This architecture provides maximum flexibility while keeping models focused and properly separated!
