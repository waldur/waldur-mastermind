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

Project metadata endpoints are available at both project and customer levels:

### Project-Level Endpoints

Base URL: `/api/projects/<project-uuid>/`

### Customer-Level Compliance Endpoints

Base URL: `/api/customers/<customer-uuid>/`

These endpoints provide aggregated compliance information across all projects in a customer organization. All endpoints support efficient database-level pagination to handle large numbers of projects.

#### Customer-Level Compliance Overview

Get an overview of project metadata compliance across all customer projects.

```http
GET /api/customers/<customer-uuid>/project-metadata-compliance-overview/
Authorization: Token <token>
```

**Permissions Required:**

- Customer owner
- Customer support
- Staff user

**Response:**

```json
{
  "checklist_configured": true,
  "checklist": {
    "uuid": "checklist-uuid",
    "name": "Project Metadata Collection",
    "description": "Standard metadata required for all projects"
  },
  "total_projects": 25,
  "projects_with_completion": 20,
  "projects_without_completion": 5,
  "average_completion_percentage": 75.5,
  "fully_completed_projects": 15,
  "partially_completed_projects": 5,
  "not_started_projects": 5
}
```

#### Customer-Level Compliance Projects List

Get paginated list of projects with their completion status.

```http
GET /api/customers/<customer-uuid>/project-metadata-compliance-projects/
Authorization: Token <token>
```

**Query Parameters:**

- `page` - Page number (default: 1)
- `page_size` - Number of projects per page (default: 10, max: 300)

**Permissions Required:**

- Customer owner
- Customer support
- Staff user

**Response:**

```json
[
  {
    "uuid": "project-uuid-1",
    "name": "AI Research Project",
    "completion_uuid": "completion-uuid-1",
    "completion_percentage": 100.0,
    "is_completed": true,
    "unanswered_required_questions": 0
  },
  {
    "uuid": "project-uuid-2",
    "name": "Development Project",
    "completion_uuid": "completion-uuid-2",
    "completion_percentage": 66.7,
    "is_completed": false,
    "unanswered_required_questions": 1
  }
]
```

**Response Headers:**

- `X-Result-Count` - Total number of projects
- `Link` - Pagination links (first, prev, next, last)

#### Customer-Level Question Answers

Get paginated list of questions with answers across all projects.

```http
GET /api/customers/<customer-uuid>/project-metadata-question-answers/
Authorization: Token <token>
```

**Query Parameters:**

- `page` - Page number (default: 1)
- `page_size` - Number of questions per page (default: 10, max: 300)

**Permissions Required:**

- Customer owner
- Customer support
- Staff user

**Response:**

```json
[
  {
    "uuid": "question-uuid-1",
    "description": "Project purpose",
    "question_type": "text_area",
    "required": true,
    "order": 1,
    "projects_with_answers": [
      {
        "project_uuid": "project-uuid-1",
        "project_name": "AI Research Project",
        "answer_data": "Research project for AI development",
        "user_name": "John Doe",
        "created": "2024-01-15T14:20:00Z",
        "modified": "2024-01-15T14:20:00Z"
      },
      {
        "project_uuid": "project-uuid-2",
        "project_name": "Development Project",
        "answer_data": "Software development project",
        "user_name": "Jane Smith",
        "created": "2024-01-16T10:30:00Z",
        "modified": "2024-01-16T10:30:00Z"
      }
    ]
  }
]
```

**Response Headers:**

- `X-Result-Count` - Total number of questions
- `Link` - Pagination links (first, prev, next, last)

#### Customer-Level Compliance Details

Get paginated detailed compliance information for each project.

```http
GET /api/customers/<customer-uuid>/project-metadata-compliance-details/
Authorization: Token <token>
```

**Query Parameters:**

- `page` - Page number (default: 1)
- `page_size` - Number of projects per page (default: 10, max: 300)

**Permissions Required:**

- Customer owner
- Customer support
- Staff user

**Response:**

```json
[
  {
    "project": {
      "uuid": "project-uuid-1",
      "name": "AI Research Project"
    },
    "completion": {
      "uuid": "completion-uuid-1",
      "is_completed": true,
      "completion_percentage": 100.0,
      "created": "2024-01-15T10:30:00Z",
      "modified": "2024-01-15T15:45:00Z"
    },
    "answers": [
      {
        "question_uuid": "question-uuid-1",
        "question_description": "Project purpose",
        "answer_data": "Research project for AI development",
        "user_name": "John Doe",
        "created": "2024-01-15T14:20:00Z"
      },
      {
        "question_uuid": "question-uuid-2",
        "question_description": "Project category",
        "answer_data": ["option-uuid-1"],
        "user_name": "John Doe",
        "created": "2024-01-15T14:25:00Z"
      }
    ],
    "unanswered_required_questions": []
  }
]
```

**Response Headers:**

- `X-Result-Count` - Total number of projects
- `Link` - Pagination links (first, prev, next, last)

### Performance Notes

All customer-level compliance endpoints use database-level pagination for optimal performance:

- **Efficient Data Loading**: Only retrieves data for the current page, not all records
- **Bulk Operations**: Uses optimized database queries with `select_related()` and `prefetch_related()`
- **Memory Efficient**: Handles large numbers of projects without memory issues
- **Pagination Headers**: Returns `X-Result-Count` header with total count and `Link` header with navigation links

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
6. **Use Customer-Level Endpoints**: For organizational dashboards, use customer-level compliance endpoints for efficient aggregated views
7. **Leverage Pagination**: Take advantage of database-level pagination for large datasets with appropriate page sizes

### For Administrators

1. **Plan Question Structure**: Design questions before creating projects
2. **Use Clear Descriptions**: Make question descriptions self-explanatory
3. **Set Appropriate Requirements**: Mark essential questions as required
4. **Test Thoroughly**: Test the complete flow before deploying to users
5. **Monitor Adoption**: Track completion rates to ensure effective use

## Related Documentation

- [Core Checklist System](./core-concepts/checklists.md)
- [Permission System](./core-concepts/permissions.md)
