# Project Metadata API Documentation

Project metadata functionality allows organizations to collect structured information about their
projects using customizable checklists. This feature is built on top of the core checklist system
and provides a standardized way to gather project details, compliance information, and other metadata.

## Overview

Project metadata uses the checklist system to enable organizations to:

- Define custom metadata collection forms for their projects
- Ensure consistent data collection across all projects
- Track completion status of metadata requirements
- Manage access controls for viewing and editing metadata

## Configuration

### Setting Up Project Metadata

1. **Create a Project Metadata Checklist**

   First, create a checklist with type `PROJECT_METADATA`:

   ```http
   POST /api/checklists-admin/
   Content-Type: application/json
   Authorization: Token <admin-token>

   {
     "name": "Project Metadata Collection",
     "description": "Standard metadata required for all projects",
     "checklist_type": "PROJECT_METADATA"
   }
   ```

2. **Add Questions to the Checklist**

   ```http
   POST /api/checklists-admin/questions/
   Content-Type: application/json
   Authorization: Token <admin-token>

   {
     "checklist": "<checklist-uuid>",
     "description": "Project purpose",
     "question_type": "text_area",
     "required": true,
     "order": 1
   }
   ```

3. **Assign Checklist to Customer**

   Assign the checklist to a customer to enable metadata collection for all their projects:

   ```http
   PATCH /api/customers/<customer-uuid>/
   Content-Type: application/json
   Authorization: Token <admin-token>

   {
     "project_metadata_checklist": "<checklist-uuid>"
   }
   ```

## API Endpoints

All project metadata endpoints are available under the project resource:

### Base URL Pattern

```http
/api/projects/<project-uuid>/
```

### Available Actions

#### 1. Get Project Metadata Checklist

Retrieves the metadata checklist for a project, including all questions and existing answers.

```http
GET /api/projects/<project-uuid>/checklist/
Authorization: Token <token>
```

**Permissions Required:**

- Project member (admin, manager, or member)
- Customer owner

**Response:**

```json
{
  "checklist": {
    "uuid": "checklist-uuid",
    "name": "Project Metadata Collection",
    "description": "Standard metadata required for all projects",
    "checklist_type": "PROJECT_METADATA"
  },
  "completion": {
    "uuid": "completion-uuid",
    "is_completed": false,
    "completion_percentage": 33.3,
    "unanswered_required_questions": [
      {
        "uuid": "question-uuid",
        "description": "Project budget",
        "question_type": "number"
      }
    ],
    "checklist_name": "Project Metadata Collection",
    "checklist_description": "Standard metadata required for all projects",
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T14:20:00Z"
  },
  "questions": [
    {
      "uuid": "question-uuid",
      "description": "Project purpose",
      "question_type": "text_area",
      "required": true,
      "order": 1,
      "existing_answer": {
        "uuid": "answer-uuid",
        "answer_data": "Research project for AI development",
        "user": "user-uuid",
        "user_name": "John Doe",
        "created": "2024-01-15T14:20:00Z",
        "modified": "2024-01-15T14:20:00Z"
      },
      "question_options": []
    },
    {
      "uuid": "question-uuid-2",
      "description": "Project category",
      "question_type": "single_select",
      "required": true,
      "order": 2,
      "existing_answer": null,
      "question_options": [
        {
          "uuid": "option-uuid-1",
          "label": "Research",
          "order": 1
        },
        {
          "uuid": "option-uuid-2",
          "label": "Development",
          "order": 2
        }
      ]
    }
  ]
}
```

#### 2. Get Completion Status

Retrieves only the completion status information for the project metadata.

```http
GET /api/projects/<project-uuid>/completion_status/
Authorization: Token <token>
```

**Permissions Required:**

- Project member (admin, manager, or member)
- Customer owner

**Response:**

```json
{
  "uuid": "completion-uuid",
  "is_completed": false,
  "completion_percentage": 66.7,
  "unanswered_required_questions": [
    {
      "uuid": "question-uuid",
      "description": "Project budget",
      "question_type": "number"
    }
  ],
  "checklist_name": "Project Metadata Collection",
  "checklist_description": "Standard metadata required for all projects",
  "created": "2024-01-15T10:30:00Z",
  "modified": "2024-01-15T14:20:00Z"
}
```

#### 3. Submit Metadata Answers

Submit or update answers to metadata questions.

```http
POST /api/projects/<project-uuid>/submit_answers/
Content-Type: application/json
Authorization: Token <token>

[
  {
    "question_uuid": "question-uuid-1",
    "answer_data": "This is a research project for machine learning applications"
  },
  {
    "question_uuid": "question-uuid-2",
    "answer_data": true
  },
  {
    "question_uuid": "question-uuid-3",
    "answer_data": ["option-uuid-1"]
  },
  {
    "question_uuid": "question-uuid-4",
    "answer_data": ["option-uuid-2", "option-uuid-3"]
  }
]
```

**Permissions Required:**

- Customer owner
- Project manager

**Answer Data Formats:**

| Question Type | Format | Example |
|---------------|--------|---------|
| `text_input` | String | `"Short text answer"` |
| `text_area` | String | `"Long text answer with multiple lines"` |
| `boolean` | Boolean | `true` or `false` |
| `number` | Number | `42` or `3.14` |
| `single_select` | Array with one UUID | `["option-uuid"]` |
| `multi_select` | Array with multiple UUIDs | `["option-uuid-1", "option-uuid-2"]` |

**Response:**

```json
{
  "detail": "Answers submitted successfully",
  "completion": {
    "uuid": "completion-uuid",
    "is_completed": true,
    "completion_percentage": 100.0,
    "unanswered_required_questions": [],
    "checklist_name": "Project Metadata Collection",
    "checklist_description": "Standard metadata required for all projects",
    "created": "2024-01-15T10:30:00Z",
    "modified": "2024-01-15T15:45:00Z"
  }
}
```

## Error Responses

### Common Error Codes

#### 400 Bad Request

```json
{
  "detail": "No checklist configured for this object"
}
```

#### 403 Forbidden

```json
{
  "detail": "You do not have permission to perform this action."
}
```

#### 404 Not Found

```json
{
  "detail": "Not found."
}
```

### Validation Errors

When submitting invalid answers:

```json
[
  {},  // First answer valid
  {
    "non_field_errors": [
      "Answer value 'invalid' is not valid for the question 'Project category' (type: single_select)."
    ]
  },  // Second answer invalid
  {}   // Third answer valid
]
```

## Permission Model

The project metadata system uses a granular permission model:

### View Permissions (checklist, completion_status)

- **Project Admin**: Can view metadata for their projects
- **Project Manager**: Can view metadata for their projects
- **Project Member**: Can view metadata for their projects
- **Customer Owner**: Can view metadata for all projects in their organization

### Update Permissions (submit_answers)

- **Customer Owner**: Can update metadata for all projects in their organization
- **Project Manager**: Can update metadata for their projects

### Administrative Permissions (checklist management)

- **Staff Users**: Can create and manage checklists
- **Customer Owners**: Can assign checklists to their organization

## Lifecycle Management

### Automatic Checklist Completion Creation

When a customer has a project metadata checklist configured:

1. **New Project Creation**: ChecklistCompletion is automatically created for new projects
2. **Existing Projects**: ChecklistCompletion is created for all existing projects when checklist is assigned
3. **Checklist Removal**: All associated ChecklistCompletions are automatically deleted

### Data Integrity

- Answers are tied to the user who submitted them
- Multiple users can provide answers to the same checklist
- Answer history is maintained with creation and modification timestamps
- Completion status is calculated in real-time based on required questions

## Best Practices

### For API Consumers

1. **Check Completion Status**: Always check if metadata is required before showing forms
2. **Handle Missing Checklists**: Gracefully handle cases where no checklist is configured
3. **Validate Before Submit**: Validate answer formats client-side to reduce API errors
4. **Show Progress**: Use completion_percentage to show users their progress
5. **Cache Appropriately**: Checklist structure changes infrequently, status changes often

### For Administrators

1. **Plan Question Structure**: Design questions before creating projects
2. **Use Clear Descriptions**: Make question descriptions self-explanatory
3. **Set Appropriate Requirements**: Mark essential questions as required
4. **Test Thoroughly**: Test the complete flow before deploying to users
5. **Monitor Adoption**: Track completion rates to ensure effective use

## Related Documentation

- [Core Checklist System](./core-concepts/checklists.md)
- [Permission System](./core-concepts/permissions.md)
