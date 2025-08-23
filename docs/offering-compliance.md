# Offering Compliance API Documentation

Offering compliance functionality enables service providers to define and track compliance requirements for their marketplace offerings. Users of these offerings must complete compliance checklists, and service providers can monitor completion status across all their offerings.

## Overview

The offering compliance system allows service providers to:

- Define compliance checklists for their offerings
- Track user compliance across all offerings
- Monitor completion rates and identify users needing attention
- Ensure regulatory and organizational compliance requirements are met

## Architecture

The system consists of three main components:

1. **Compliance Checklists** - Customizable questionnaires attached to offerings
2. **Offering Users** - Users who have access to offerings and must complete compliance
3. **Service Provider Management** - Dashboard and APIs for compliance monitoring

## Configuration

### Setting Up Offering Compliance

#### 1. Create a Compliance Checklist

First, create a checklist with type `offering_compliance`:

```http
POST /api/checklists-admin/
Content-Type: application/json
Authorization: Token <admin-token>

{
  "name": "Security Compliance Checklist",
  "description": "Required security compliance information for cloud services",
  "checklist_type": "offering_compliance"
}
```

#### 2. Add Questions to the Checklist

```http
POST /api/checklists-admin/questions/
Content-Type: application/json
Authorization: Token <admin-token>

{
  "checklist": "<checklist-uuid>",
  "description": "Have you completed security training?",
  "question_type": "boolean",
  "required": true,
  "order": 1
}
```

#### 3. Assign Checklist to Offering

Attach the compliance checklist to an offering:

```http
PATCH /api/marketplace-provider-offerings/<offering-uuid>/
Content-Type: application/json
Authorization: Token <service-provider-token>

{
  "compliance_checklist": "<checklist-uuid>"
}
```

## API Endpoints

### Checking Offering Compliance Requirements

Before accessing compliance-specific endpoints, you can check if an offering has compliance requirements using the standard offering API.

#### Get Offering Details

```http
GET /api/marketplace-public-offerings/<offering-uuid>/
```

**Response includes:**

```json
{
  "uuid": "offering-uuid",
  "name": "Cloud VM Service",
  "has_compliance_requirements": true,
  // ... other offering fields
}
```

**Field Description:**

| Field | Type | Description |
|-------|------|-------------|
| `has_compliance_requirements` | Boolean | Indicates whether this offering requires users to complete compliance checklists |

**Usage:**

- `true` - This offering requires compliance. Users must complete checklists before using the service.
- `false` - This offering has no compliance requirements. Users can use the service without additional checklists.

### For Offering Users

These endpoints allow offering users to view and complete their compliance requirements.

#### Base URL Pattern

```http
/api/marketplace-offering-users/<offering-user-uuid>/
```

#### 1. Get Compliance Checklist

Retrieves the compliance checklist for an offering user, including all questions and existing answers.

```http
GET /api/marketplace-offering-users/<offering-user-uuid>/checklist/
Authorization: Token <user-token>
```

**Permissions Required:**

- The authenticated user must be the offering user
- Service provider staff with UPDATE_OFFERING_USER permission

**Response:**

```json
{
  "checklist": {
    "uuid": "checklist-uuid",
    "name": "Security Compliance Checklist",
    "description": "Required security compliance information",
    "checklist_type": "offering_compliance"
  },
  "completion": {
    "uuid": "completion-uuid",
    "is_completed": false,
    "completion_percentage": 50.0,
    "unanswered_required_questions": [
      {
        "uuid": "question-uuid",
        "description": "Data handling procedures",
        "question_type": "text_area"
      }
    ],
    "checklist_name": "Security Compliance Checklist",
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T14:20:00Z"
  },
  "questions": [
    {
      "uuid": "question-uuid-1",
      "description": "Have you completed security training?",
      "question_type": "boolean",
      "required": true,
      "order": 1,
      "existing_answer": {
        "uuid": "answer-uuid",
        "answer_data": true,
        "user": "user-uuid",
        "user_name": "John Doe",
        "created": "2024-01-15T14:20:00Z"
      }
    },
    {
      "uuid": "question-uuid-2",
      "description": "Compliance level",
      "question_type": "single_select",
      "required": true,
      "order": 2,
      "existing_answer": null,
      "question_options": [
        {
          "uuid": "option-uuid-1",
          "label": "Basic",
          "order": 1
        },
        {
          "uuid": "option-uuid-2",
          "label": "Advanced",
          "order": 2
        }
      ]
    }
  ]
}
```

#### 2. Get Completion Status

Retrieves only the completion status for the offering user's compliance.

```http
GET /api/marketplace-offering-users/<offering-user-uuid>/completion_status/
Authorization: Token <user-token>
```

**Response:**

```json
{
  "uuid": "completion-uuid",
  "is_completed": false,
  "completion_percentage": 75.0,
  "unanswered_required_questions": [
    {
      "uuid": "question-uuid",
      "description": "Emergency contact",
      "question_type": "text_input"
    }
  ],
  "checklist_name": "Security Compliance Checklist",
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-15T14:20:00Z"
}
```

#### 3. Submit Compliance Answers

Submit or update answers to compliance questions.

```http
POST /api/marketplace-offering-users/<offering-user-uuid>/submit_answers/
Content-Type: application/json
Authorization: Token <user-token>

[
  {
    "question_uuid": "question-uuid-1",
    "answer_data": true
  },
  {
    "question_uuid": "question-uuid-2",
    "answer_data": "Completed advanced security training on 2024-01-10"
  },
  {
    "question_uuid": "question-uuid-3",
    "answer_data": ["option-uuid-1"]
  }
]
```

**Answer Data Formats:**

| Question Type | Format | Example |
|---------------|--------|---------|
| `text_input` | String | `"Short text answer"` |
| `text_area` | String | `"Detailed compliance information"` |
| `boolean` | Boolean | `true` or `false` |
| `number` | Number | `42` or `3.14` |
| `single_select` | Array with one UUID | `["option-uuid"]` |
| `multi_select` | Array with multiple UUIDs | `["option-1", "option-2"]` |

**Response:**

```json
{
  "detail": "Answers submitted successfully",
  "completion": {
    "uuid": "completion-uuid",
    "is_completed": true,
    "completion_percentage": 100.0,
    "unanswered_required_questions": [],
    "checklist_name": "Security Compliance Checklist",
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T15:45:00Z"
  }
}
```

### For Service Providers

These endpoints allow service providers to monitor and manage compliance across all their offerings.

#### Base URL Pattern

```http
/api/marketplace-service-providers/<service-provider-uuid>/compliance/
```

#### 1. Compliance Overview

Get aggregated compliance statistics for all offerings managed by the service provider.

```http
GET /api/marketplace-service-providers/<service-provider-uuid>/compliance/compliance_overview/
Authorization: Token <service-provider-token>
```

**Permissions Required:**

- Customer owner or staff with LIST_SERVICE_PROVIDER_CUSTOMERS permission

**Response:**

```json
[
  {
    "offering_uuid": "offering-uuid-1",
    "offering_name": "Cloud VM Service",
    "checklist_name": "Security Compliance Checklist",
    "total_users": 25,
    "users_with_completions": 23,
    "completed_users": 18,
    "pending_users": 5,
    "compliance_rate": 72.0
  },
  {
    "offering_uuid": "offering-uuid-2",
    "offering_name": "Storage Service",
    "checklist_name": "Data Protection Compliance",
    "total_users": 15,
    "users_with_completions": 15,
    "completed_users": 15,
    "pending_users": 0,
    "compliance_rate": 100.0
  },
  {
    "offering_uuid": "offering-uuid-3",
    "offering_name": "Basic Compute",
    "checklist_name": null,
    "total_users": 10,
    "users_with_completions": 0,
    "completed_users": 0,
    "pending_users": 0,
    "compliance_rate": 0.0
  }
]
```

#### 2. List Offering Users with Compliance Status

Get detailed compliance status for individual offering users.

```http
GET /api/marketplace-service-providers/<service-provider-uuid>/compliance/offering_users/
Authorization: Token <service-provider-token>
```

**Query Parameters:**

| Parameter | Type | Description |
|-----------|------|-------------|
| `offering_uuid` | UUID | Filter by specific offering |
| `compliance_status` | String | Filter by status: `completed`, `pending`, `no_checklist` |

**Example Request:**

```http
GET /api/marketplace-service-providers/<uuid>/compliance/offering_users/?compliance_status=pending
```

**Response:**

```json
[
  {
    "uuid": "offering-user-uuid-1",
    "user_full_name": "John Doe",
    "user_email": "john.doe@example.com",
    "offering_name": "Cloud VM Service",
    "checklist_name": "Security Compliance Checklist",
    "username": "johndoe",
    "state": "OK",
    "completion_percentage": 75,
    "compliance_status": "pending",
    "last_updated": "2024-01-19T10:30:00Z",
    "created": "2024-01-15T09:00:00Z"
  },
  {
    "uuid": "offering-user-uuid-2",
    "user_full_name": "Jane Smith",
    "user_email": "jane.smith@example.com",
    "offering_name": "Cloud VM Service",
    "checklist_name": "Security Compliance Checklist",
    "username": "janesmith",
    "state": "OK",
    "completion_percentage": 50,
    "compliance_status": "pending",
    "last_updated": "2024-01-18T14:20:00Z",
    "created": "2024-01-10T11:00:00Z"
  }
]
```

## Workflow Examples

### Complete User Compliance Flow

1. **Check if offering requires compliance**

   ```http
   GET /api/marketplace-public-offerings/<offering-uuid>/
   ```

   Check the `has_compliance_requirements` field in the response.

2. **User receives offering access**
  - When an offering user is created for an offering with compliance requirements, a compliance checklist completion is automatically created

3. **User views compliance requirements** (only if `has_compliance_requirements` is `true`)

   ```http
   GET /api/marketplace-offering-users/<uuid>/checklist/
   ```

4. **User submits compliance answers**

   ```http
   POST /api/marketplace-offering-users/<uuid>/submit_answers/
   ```

5. **Service provider monitors compliance**

   ```http
   GET /api/marketplace-service-providers/<uuid>/compliance/compliance_overview/
   ```

### Service Provider Monitoring Flow

1. **View overall compliance statistics**

   ```http
   GET /api/marketplace-service-providers/<uuid>/compliance/compliance_overview/
   ```

2. **Identify non-compliant users**

   ```http
   GET /api/marketplace-service-providers/<uuid>/compliance/offering_users/?compliance_status=pending
   ```

3. **Follow up with specific users**

   Use the user details from the response to send reminders or take action

## Lifecycle Management

### Automatic Checklist Creation

When an offering user is created:

1. System checks if the offering has a compliance checklist
2. If present, creates a ChecklistCompletion object automatically
3. Links it to the offering user via generic foreign key

### Automatic Cleanup

When an offering user is deleted:

1. Associated ChecklistCompletion is automatically deleted
2. All related answers are cascade deleted
3. No orphaned data remains

## Error Handling

### Common Error Responses

#### 400 Bad Request - No Checklist Configured

```json
{
  "detail": "No checklist configured for this object"
}
```

This occurs when trying to access compliance endpoints for an offering without a configured checklist.

#### 403 Forbidden - Insufficient Permissions

```json
{
  "detail": "You do not have permission to perform this action."
}
```

#### 404 Not Found - Resource Not Found

```json
{
  "detail": "Not found."
}
```

This can occur when:

- The offering user doesn't exist
- The service provider doesn't exist
- The user doesn't have access to the resource

### Validation Errors

When submitting invalid answers:

```json
[
  {},  // First answer valid
  {
    "non_field_errors": [
      "Answer value 'invalid text' is not valid for the question 'Accept terms' (type: boolean)."
    ]
  },
  {}   // Third answer valid
]
```

## Integration Patterns

### Frontend Integration

When displaying offerings in your application, use the `has_compliance_requirements` field to show appropriate UI elements:

```javascript
// Example: React component for offering display
function OfferingCard({ offering }) {
  return (
    <div className="offering-card">
      <h3>{offering.name}</h3>
      <p>{offering.description}</p>

      {offering.has_compliance_requirements && (
        <div className="compliance-notice">
          <Icon name="shield" />
          <span>Compliance requirements apply</span>
        </div>
      )}

      <button onClick={() => handleOrder(offering)}>
        Order Service
      </button>
    </div>
  );
}
```

### Order Flow Integration

```javascript
// Example: Check compliance before allowing order completion
async function handleOfferingOrder(offering, user) {
  if (offering.has_compliance_requirements) {
    // Redirect to compliance checklist first
    const complianceStatus = await checkUserCompliance(offering, user);

    if (!complianceStatus.is_completed) {
      return redirectToCompliance(offering, user);
    }
  }

  // Proceed with normal order flow
  return processOrder(offering, user);
}
```

### Service Provider Dashboard

Use the compliance overview endpoints to build monitoring dashboards:

```javascript
// Example: Compliance dashboard component
function ComplianceDashboard({ serviceProvider }) {
  const [complianceData, setComplianceData] = useState([]);

  useEffect(() => {
    fetch(`/api/marketplace-service-providers/${serviceProvider.uuid}/compliance/compliance_overview/`)
      .then(response => response.json())
      .then(data => setComplianceData(data));
  }, [serviceProvider.uuid]);

  return (
    <div className="compliance-dashboard">
      {complianceData.map(offering => (
        <div key={offering.offering_uuid} className="offering-compliance">
          <h4>{offering.offering_name}</h4>
          {offering.checklist_name ? (
            <div className="compliance-stats">
              <span>Compliance Rate: {offering.compliance_rate}%</span>
              <span>Pending Users: {offering.pending_users}</span>
            </div>
          ) : (
            <span className="no-compliance">No compliance requirements</span>
          )}
        </div>
      ))}
    </div>
  );
}
```

## Best Practices

### For Service Providers

1. **Define Clear Questions**
  - Use descriptive question text
  - Mark truly required fields as required
  - Provide helpful guidance text where needed

2. **Regular Monitoring**
  - Check compliance overview regularly
  - Follow up with users who have pending compliance
  - Consider setting up automated reminders

3. **Checklist Design**
  - Keep checklists concise and relevant
  - Group related questions logically
  - Use appropriate question types for each data point

### For Developers

1. **Performance Optimization**
  - Use the `has_compliance_requirements` field to avoid unnecessary API calls
  - Use the `compliance_overview` endpoint for dashboard views
  - Apply filters to reduce data transfer
  - Cache compliance status where appropriate

2. **Error Handling**
  - Always check `has_compliance_requirements` before accessing compliance endpoints
  - Handle gracefully when compliance endpoints return 400 (no checklist configured)
  - Handle permission errors gracefully
  - Provide clear feedback to users

3. **Integration**
  - Check `has_compliance_requirements` in offering lists to show UI indicators
  - Only call compliance endpoints when `has_compliance_requirements` is `true`
  - Consider webhooks for compliance completion events
  - Integrate with notification systems for reminders
  - Build dashboards using the overview endpoints

## Migration Guide

### Adding Compliance to Existing Offerings

1. Create the compliance checklist
2. Update the offering with the checklist UUID
3. Existing offering users will need to complete compliance
4. New offering users will get compliance requirements automatically

### Removing Compliance Requirements

1. Set offering's `compliance_checklist` to null
2. Existing completions remain for audit purposes
3. No new completions will be created

## Security Considerations

1. **Data Privacy**
  - Compliance data is only visible to authorized users
  - Service providers can only see data for their own offerings
  - Users can only see and edit their own compliance data

2. **Audit Trail**
  - All answer submissions are tracked with timestamps
  - User information is preserved with each answer
  - Changes are logged for compliance auditing

3. **Permission Model**
  - Based on existing Waldur permission system
  - Respects customer and project boundaries
  - Service provider permissions are strictly scoped
