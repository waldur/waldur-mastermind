# Waldur Support Module

The Support module provides a comprehensive helpdesk and ticketing system with multi-backend integration, enabling organizations to manage support requests through JIRA, SMAX, Zammad, or a basic built-in system.

## Overview

The support module acts as an abstraction layer over multiple ticketing backends, providing:

- Unified API for ticket management across different backends
- Bidirectional synchronization with external ticketing systems
- Template-based issue creation
- Customer feedback collection
- SLA tracking and reporting
- Advanced permission management

## Architecture

```mermaid
graph TB
    subgraph "Waldur Support"
        API[Support API]
        Models[Support Models]
        Backend[Backend Interface]
    end

    subgraph "External Systems"
        JIRA[JIRA/Service Desk]
        SMAX[Micro Focus SMAX]
        Zammad[Zammad]
    end

    subgraph "Integration"
        Webhook[Webhooks]
        Sync[Synchronization]
    end

    API --> Models
    Models --> Backend
    Backend --> JIRA
    Backend --> SMAX
    Backend --> Zammad

    JIRA --> Webhook
    SMAX --> Webhook
    Zammad --> Webhook
    Webhook --> Models

    Sync --> Backend
```

## Core Components

### 1. Issue Management

The `Issue` model is the central entity for ticket management:

```python
class Issue:
    # Identification
    key: str              # Backend ticket ID (e.g., "PROJ-123")
    backend_id: str       # Internal backend ID
    summary: str          # Issue title
    description: str      # Detailed description

    # Classification
    type: str            # INFORMATIONAL, SERVICE_REQUEST, CHANGE_REQUEST, INCIDENT
    status: str          # Backend-specific status
    priority: Priority   # Priority level

    # Relationships
    caller: User         # Waldur user who created the issue
    reporter: SupportUser # Backend user who reported
    assignee: SupportUser # Backend user assigned
    customer: Customer   # Associated customer
    project: Project     # Associated project
    resource: GenericFK  # Linked resource (VM, network, etc.)

    # SLA & Timing
    created: datetime
    modified: datetime
    deadline: datetime
    first_response_sla: datetime
    resolution_date: datetime
```

**Issue Types:**

- `INFORMATIONAL` - General information requests
- `SERVICE_REQUEST` - Service provisioning requests
- `CHANGE_REQUEST` - Change management requests
- `INCIDENT` - Incident reports and outages

### 2. Comment System

Comments provide threaded discussions on issues:

```python
class Comment:
    issue: Issue         # Parent issue
    description: str     # Comment text
    author: SupportUser  # Comment author
    is_public: bool      # Visibility control
    backend_id: str      # Backend comment ID
```

**Comment Features:**

- Public/private visibility control
- Automatic user information formatting for backends
- Bidirectional synchronization

### 3. Attachment Management

File attachments for issues and templates:

```python
class Attachment:
    issue: Issue
    file: FileField     # Stored in 'support_attachments/'
    backend_id: str     # Backend attachment ID

    # Properties from FileMixin
    mime_type: str
    file_size: int
```

### 4. User Management

`SupportUser` bridges Waldur users with backend systems:

```python
class SupportUser:
    user: User          # Waldur user (optional)
    name: str           # Display name
    backend_id: str     # Backend user ID
    backend_name: str   # Backend type
    is_active: bool     # Activity status
```

### 5. Template System

Templates enable standardized issue creation:

```python
class Template:
    name: str
    description: str
    issue_type: str     # Issue classification

class TemplateConfirmationComment:
    template: Template
    comment: str        # Auto-response text

class TemplateStatusNotification:
    template: Template
    status: str         # Trigger status
    html_template: str  # Email template
```

### 6. Status Management

`IssueStatus` maps backend statuses to resolution types:

```python
class IssueStatus:
    name: str           # Backend status name
    type: int           # RESOLVED or CANCELED

    # Types
    RESOLVED = 0        # Successfully completed
    CANCELED = 1        # Failed or canceled
```

### 7. Feedback System

Customer satisfaction tracking:

```python
class Feedback:
    issue: Issue
    evaluation: int     # 1-10 scale
    comment: str        # Optional feedback text
```

## Backend Integration

### Supported Backends

#### 1. JIRA/Atlassian Service Desk

Full-featured integration with:

- Service Desk project support
- Request type management
- Customer portal integration
- Webhook support for real-time updates
- Custom field mapping

#### 2. Micro Focus SMAX

Enterprise ITSM integration:

- Request and incident management
- Change management workflows
- Service catalog integration
- REST API-based synchronization
- Webhook support for real-time updates

#### 3. Zammad

Open-source ticketing system:

- Multi-channel support (email, web, phone)
- Customer organization management
- Tag-based categorization
- Webhook integration

#### 4. Basic Backend

No-op implementation for:

- Development and testing
- Environments without external ticketing
- Minimal support requirements

### Backend Interface

All backends implement the `SupportBackend` interface:

```python
class SupportBackend:
    # Issue operations
    def create_issue(issue: Issue) -> Issue
    def update_issue(issue: Issue) -> Issue
    def delete_issue(issue: Issue)
    def sync_issues(issue_id: Optional[str])

    # Comment operations
    def create_comment(comment: Comment) -> Comment
    def update_comment(comment: Comment) -> Comment
    def delete_comment(comment: Comment)

    # Attachment operations
    def create_attachment(attachment: Attachment) -> Attachment
    def delete_attachment(attachment: Attachment)

    # User management
    def pull_support_users()
    def get_or_create_support_user(backend_user) -> SupportUser

    # Configuration
    def pull_priorities()
    def pull_request_types()
```

## API Endpoints

### Issue Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/support-issues/` | GET | List issues with filtering |
| `/api/support-issues/` | POST | Create new issue |
| `/api/support-issues/{uuid}/` | GET | Retrieve issue details |
| `/api/support-issues/{uuid}/` | PATCH | Update issue |
| `/api/support-issues/{uuid}/` | DELETE | Delete issue |
| `/api/support-issues/{uuid}/comment/` | POST | Add comment to issue |
| `/api/support-issues/{uuid}/sync/` | POST | Sync issue with backend |

### Comments

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/support-comments/` | GET | List comments |
| `/api/support-comments/{uuid}/` | GET | Retrieve comment |
| `/api/support-comments/{uuid}/` | PATCH | Update comment |
| `/api/support-comments/{uuid}/` | DELETE | Delete comment |

### Attachments

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/support-attachments/` | GET | List attachments |
| `/api/support-attachments/` | POST | Upload attachment |
| `/api/support-attachments/{uuid}/` | GET | Download attachment |
| `/api/support-attachments/{uuid}/` | DELETE | Delete attachment |

### Configuration & Management

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/support-users/` | GET | List support users |
| `/api/support-priorities/` | GET | List priorities |
| `/api/support-templates/` | GET/POST | Manage templates |
| `/api/support-feedback/` | GET/POST | Manage feedback |

### Webhooks

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/support-jira-webhook/` | POST | JIRA webhook receiver |
| `/api/support-smax-webhook/` | POST | SMAX webhook receiver |
| `/api/support-zammad-webhook/` | POST | Zammad webhook receiver |

### Reports

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/support-statistics/` | GET | Dashboard statistics |
| `/api/support-feedback-report/` | GET | Feedback summary |
| `/api/support-feedback-average-report/` | GET | Average ratings |

## Permissions

### Permission Model

The support module uses Waldur's standard permission system with additional paths:

```python
# Issue permissions follow customer/project hierarchy
permission_paths = [
    'customer',
    'project',
    'project.customer',
]

# Role-based access
- Customer Owner: Full access to customer issues
- Project Admin: Full access to project issues
- Project Manager: Read/comment on project issues
- Staff/Support: Full system access
```

### Filtering

Advanced filtering capabilities:

- Customer/project-based filtering
- Resource-based filtering (VMs, networks)
- IP address lookup for resource issues
- Full-text search across summary/description
- Status, priority, and type filtering

## Configuration

### Django Settings

```python
# Disable email notifications
WALDUR_SUPPORT['SUPPRESS_NOTIFICATION_EMAILS'] = True

# Enable feedback collection
WALDUR_SUPPORT['ISSUE_FEEDBACK_ENABLE'] = True

# Feedback token validity (days)
WALDUR_SUPPORT['ISSUE_FEEDBACK_TOKEN_PERIOD'] = 7
```

### Constance Settings

Dynamic configuration via admin:

```python
# Enable/disable support module
WALDUR_SUPPORT_ENABLED = True

# Select active backend
WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE = 'atlassian'  # or 'zammad', 'smax', 'basic'
```

### Backend Configuration

#### JIRA Configuration

```python
WALDUR_SUPPORT['BACKEND'] = {
    'backend': 'waldur_mastermind.support.backend.atlassian.ServiceDeskBackend',
    'server': 'https://jira.example.com',
    'username': 'waldur-bot',
    'password': 'secret',
    'project_key': 'SUPPORT',
    'verify_ssl': True,
}
```

#### SMAX Configuration

```python
WALDUR_SUPPORT['BACKEND'] = {
    'backend': 'waldur_mastermind.support.backend.smax.SmaxBackend',
    'api_url': 'https://smax.example.com/rest',
    'tenant_id': '12345',
    'user': 'integration-user',
    'password': 'secret',
}
```

#### Zammad Configuration

```python
WALDUR_SUPPORT['BACKEND'] = {
    'backend': 'waldur_mastermind.support.backend.zammad.ZammadBackend',
    'api_url': 'https://zammad.example.com/api/v1',
    'token': 'api-token',
    'group': 'Support',
}
```

## Workflows

### Issue Creation Flow

```mermaid
sequenceDiagram
    participant User
    participant API
    participant Models
    participant Backend
    participant External

    User->>API: POST /support-issues/
    API->>Models: Create Issue
    Models->>Backend: create_issue()
    Backend->>External: Create ticket
    External-->>Backend: Ticket ID
    Backend-->>Models: Update backend_id
    Models-->>API: Issue created
    API-->>User: 201 Created
```

### Synchronization Flow

```mermaid
sequenceDiagram
    participant Scheduler
    participant Backend
    participant External
    participant Models

    Scheduler->>Backend: sync_issues()
    Backend->>External: Fetch updates
    External-->>Backend: Issue data
    Backend->>Models: Update issues

    Note over Backend,Models: Update status, comments, attachments

    Backend->>Models: Process callbacks
    Note over Models: Trigger marketplace callbacks if needed
```

### Webhook Flow

```mermaid
sequenceDiagram
    participant External
    participant Webhook
    participant Models
    participant Callbacks

    External->>Webhook: POST /support-*-webhook/
    Webhook->>Webhook: Validate signature
    Webhook->>Models: Update issue/comment

    alt Status changed
        Models->>Callbacks: Trigger callbacks
        Note over Callbacks: Resource state updates
    end

    Webhook-->>External: 200 OK
```

## Celery Tasks

Scheduled background tasks:

| Task | Schedule | Description |
|------|----------|-------------|
| `pull-support-users` | Every 6 hours | Sync support users from backend |
| `pull-priorities` | Daily at 1 AM | Update priority levels |
| `sync_request_types` | Daily at 1 AM | Sync JIRA request types |
| `sync-issues` | Configurable | Full issue synchronization |

## Best Practices

### 1. Backend Selection

- Use JIRA for enterprise environments with existing Atlassian infrastructure
- Use SMAX for ITIL-compliant service management
- Use Zammad for open-source, multi-channel support
- Use Basic for development or minimal requirements

### 2. Status Configuration

- Map all backend statuses to IssueStatus entries
- Define clear RESOLVED vs CANCELED mappings
- Test status transitions before production

### 3. Performance Optimization

- Enable webhooks for real-time updates
- Configure appropriate sync intervals
- Use pagination for large issue lists
- Implement caching for frequently accessed data

### 4. Security

- Use secure webhook endpoints with signature validation
- Implement proper permission checks
- Sanitize user input in comments/descriptions
- Use HTTPS for all backend connections

### 5. Monitoring

- Monitor sync task execution
- Track webhook delivery failures
- Log backend API errors
- Set up alerts for SLA breaches

## Troubleshooting

### Common Issues

#### 1. Issues Not Syncing

- Check backend connectivity
- Verify API credentials
- Review sync task logs
- Ensure webhook configuration

#### 2. Missing Status Updates

- Verify IssueStatus configuration
- Check webhook signature validation
- Review backend field mappings
- Monitor sync intervals

#### 3. Permission Errors

- Verify user roles and permissions
- Check customer/project associations
- Review permission paths configuration
- Validate backend user permissions

#### 4. Attachment Upload Failures

- Check file size limits
- Verify MIME type restrictions
- Review storage permissions
- Monitor backend API limits

## Integration with Marketplace

The support module integrates with the marketplace for ticket-based offerings:

1. Orders create support issues automatically
2. Issue status changes trigger order callbacks
3. Resolution status determines order success/failure
4. Comments and attachments sync bidirectionally

See [Ticket-Based Offerings Documentation](plugins/ticket-based-offerings.md) for detailed marketplace integration.

## Extension Points

The support module provides several extension points:

1. **Custom Backends**: Implement `SupportBackend` interface
2. **Template Processors**: Custom template variable processing
3. **Notification Handlers**: Custom email/notification logic
4. **Webhook Processors**: Custom webhook payload processing
5. **Feedback Collectors**: Alternative feedback mechanisms

## Appendix

### Database Schema

Key database tables:

- `support_issue` - Issue records
- `support_comment` - Issue comments
- `support_attachment` - File attachments
- `support_supportuser` - Backend user mapping
- `support_priority` - Priority levels
- `support_issuestatus` - Status configuration
- `support_template` - Issue templates
- `support_feedback` - Customer feedback

### API Filters

Available query parameters:

```text
?customer=<uuid>           # Filter by customer
?project=<uuid>            # Filter by project
?status=<status>           # Filter by status
?priority=<uuid>           # Filter by priority
?type=<type>               # Filter by issue type
?caller=<uuid>             # Filter by caller
?assignee=<uuid>           # Filter by assignee
?created_after=<date>      # Created after date
?created_before=<date>     # Created before date
?search=<text>             # Full-text search
?resource=<uuid>           # Filter by resource
?o=<field>                 # Order by field
```

### Error Codes

Common error responses:

| Code | Description |
|------|-------------|
| 400 | Invalid request data |
| 401 | Authentication required |
| 403 | Permission denied |
| 404 | Issue/resource not found |
| 409 | Conflict (duplicate, state issue) |
| 424 | Backend dependency failed |
| 500 | Internal server error |
