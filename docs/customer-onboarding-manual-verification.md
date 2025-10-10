# Manual Verification Flow

## Status Flow

### Automatic Validation Process

1. **PENDING** → Initial state when validation request is created
2. **VERIFIED** → Automatic validation succeeded (user is authorized representative)
3. **ESCALATED** → Automatic validation failed, needs manual review
4. **FAILED** → Manual rejection
5. **EXPIRED** → Verification expired without completion

### Manual Verification Process

When automatic validation returns **ESCALATED** status:

1. **User submits justification**:
   - Provides written explanation for why they should be authorized
   - Can attach supporting documents (multiple files allowed)

2. **Staff reviews justification**:
   - Reviews user's explanation and documents
   - Makes decision: **Approve** or **Reject**

3. **Final status**:
   - **Approve** → Status changes to **VERIFIED** (customer can be created)
   - **Reject** → Status changes to **FAILED** (cannot create customer)

### Automatic Expiration

- Verifications that remain in **PENDING** or **ESCALATED** status past their `expires_at` timestamp are automatically marked as **EXPIRED**
- An hourly background task checks for and expires stale verifications
- Once expired, the verification cannot be completed and the user must start a new validation request

## Status Transitions Diagram

```text
User Request
     ↓
  PENDING
     ↓
[Automatic Validation]
     ↓
  ┌──────┴──────┐
  ↓             ↓
VERIFIED    ESCALATED ← (automatic validation failed)
  ↓             ↓
  ↓      [User submits justification]
  ↓             ↓
  ↓      [Staff reviews]
  ↓         ┌───┴───┐
  ↓         ↓       ↓
  ↓    VERIFIED  FAILED
  ↓         ↓       ↓
  └─────→ [Customer Creation]
```

## API Endpoints

### 1. Start Validation

**POST** `/api/onboarding-verifications/validate_company/`

Request:

```json
{
  "country": "EE",
  "legal_person_identifier": "70000310",
  "legal_name": "Company Name",
  "user_submitted_customer_metadata": {
    "name": "My Company"
  }
}
```

Response includes `status` field:

- `verified`: Can proceed to create customer
- `escalated`: Need to submit justification
- `failed`: Cannot proceed (configuration/identity error)

### 2. Create Justification (User Action)

**POST** `/api/onboarding-justifications/create_justification/`

Request:

```json
{
  "verification_uuid": "xxx-xxx-xxx",
  "user_justification": "I am the technical lead of this research project..."
}
```

### 3. Attach Documents (User Action)

**POST** `/api/onboarding-justifications/{uuid}/attach_document/`

Request: multipart/form-data with `file` field

Can be called multiple times to attach multiple documents.

### 4. Approve Justification (Staff Only)

**POST** `/api/onboarding-justifications/{uuid}/approve/`

Request:

```json
{
  "staff_notes": "Verified user's role via email confirmation"
}
```

Effect:

- Justification status → `approved`
- Verification status → `verified`
- User can now create customer

### 5. Reject Justification (Staff Only)

**POST** `/api/onboarding-justifications/{uuid}/reject/`

Request:

```json
{
  "staff_notes": "Insufficient documentation provided"
}
```

Effect:

- Justification status → `rejected`
- Verification status → `failed`
- User cannot create customer

### 6. Create Customer (After Verification)

**POST** `/api/onboarding-verifications/{uuid}/create_customer/`

Only works when verification status is `verified`.

## Use Cases

### Use Case 1: Successful Automatic Validation

1. User submits validation request
2. System checks Äriregister API
3. User found as authorized representative → status: `verified`
4. User creates customer immediately

### Use Case 2: Failed Automatic Validation (Research Group)

1. User submits validation request
2. System checks Äriregister API
3. User not found in company representatives → status: `escalated`
4. User submits justification: "I'm leading a research project under this organization"
5. User uploads: project authorization letter, email from supervisor
6. Staff reviews and approves
7. Verification status → `verified`
8. User creates customer

### Use Case 3: Failed Automatic Validation (Rejected)

1. User submits validation request
2. System checks Äriregister API
3. User not found → status: `escalated`
4. User submits justification with insufficient explanation
5. Staff reviews and rejects with notes
6. Verification status → `failed`
7. User cannot proceed

## Frontend Integration Guide

### Step 1: Submit Validation

```javascript
const response = await api.post('/api/onboarding-verifications/validate_company/', {
  country: 'EE',
  legal_person_identifier: companyCode,
  legal_name: companyName,
});

if (response.data.status === 'verified') {
  // Show success, allow customer creation
} else if (response.data.status === 'escalated') {
  // Show justification form
} else {
  // Show error message
}
```

### Step 2: Submit Justification (if escalated)

```javascript
await api.post('/api/onboarding-justifications/create_justification/', {
  verification_uuid: verificationUuid,
  user_justification: explanationText,
});

// Upload documents
for (const file of documents) {
  const formData = new FormData();
  formData.append('file', file);
  await api.post(`/api/onboarding-justifications/${justificationUuid}/attach_document/`, formData);
}
```

### Step 3: Staff Review (Admin UI)

```javascript
// Approve
await api.post(`/api/onboarding-justifications/${uuid}/approve/`, {
  staff_notes: 'Verified via external documentation',
});

// Or reject
await api.post(`/api/onboarding-justifications/${uuid}/reject/`, {
  staff_notes: 'Insufficient proof of authorization',
});
```

## Background Tasks

### Automatic Verification Expiration

An hourly Celery task (`expire_stale_verifications`) runs to automatically expire stale verifications:

- **Schedule**: Every hour at the top of the hour (e.g., 1:00, 2:00, 3:00)
- **What it does**: Finds all verifications with `expires_at` in the past and status of `PENDING` or `ESCALATED`, then marks them as `EXPIRED`
- **Purpose**: Ensures that incomplete verification requests don't remain indefinitely in the system
- **Impact**: Users with expired verifications must start a new validation request
