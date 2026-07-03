# Invitations

The invitation system in Waldur provides a mechanism for inviting users to join organizations (customers), projects, or other scoped resources with specific roles. The system supports two main invitation types: individual invitations and group invitations, with different workflows and approval mechanisms.

## Architecture Overview

The invitation system is built around three core models in `waldur_core.users.models`:

- **BaseInvitation**: Abstract base class providing common fields and functionality
- **Invitation**: Individual invitations for specific users with email-based delivery
- **GroupInvitation**: Template-based invitations that can be used by multiple users matching specific criteria
- **PermissionRequest**: Approval workflow for group invitation requests

## Invitation Types

### Individual Invitations

Individual invitations are sent to specific email addresses and provide a direct mechanism to grant users access to resources.

#### Key Features

- **Email-based delivery**: Invitations are sent to specific email addresses
- **Civil number validation**: Optional civil number matching for enhanced security
- **State management**: Full lifecycle tracking with states like pending, accepted, canceled, expired
- **Execution tracking**: Background processing with error handling and retry capabilities
- **Expiration handling**: Automatic expiration based on configurable timeouts
- **Webhook support**: External system integration for invitation delivery

#### State Flow

```mermaid
stateDiagram-v2
    [*] --> PENDING: Create invitation
    [*] --> REQUESTED: Staff approval required
    [*] --> PENDING_PROJECT: Project not active yet

    REQUESTED --> PENDING: Staff approves
    REQUESTED --> REJECTED: Staff rejects

    PENDING_PROJECT --> PENDING: Project becomes active

    PENDING --> ACCEPTED: User accepts
    PENDING --> CANCELED: Creator cancels
    PENDING --> EXPIRED: Timeout reached

    CANCELED --> PENDING: Resend invitation
    EXPIRED --> PENDING: Resend invitation

    ACCEPTED --> [*]
    REJECTED --> [*]
```

### Group Invitations

Group invitations provide template-based access that multiple users can request to join, with an approval workflow. They support both private invitations (visible only to authenticated users with appropriate permissions) and public invitations (visible to all users including unauthenticated ones).

#### Key Features

- **Pattern-based matching**: Users can request access if they match email patterns or affiliations
- **Approval workflow**: Requests go through a review process before granting access
- **Auto-approval**: Optionally skip manual review for users matching invitation patterns
- **Project creation option**: Can automatically create projects instead of granting customer-level access
- **Role mapping**: Support for different roles at customer and project levels
- **Template-based naming**: Configurable project name templates for auto-created projects
- **Public visibility**: Public invitations can be viewed and requested by unauthenticated users
- **Duplicate role prevention**: Multiple layers of checks prevent duplicate role assignments

#### Workflow

```mermaid
sequenceDiagram
    participant U as User
    participant GI as GroupInvitation
    participant PR as PermissionRequest
    participant A as Approver
    participant S as System

    U->>GI: Submit request
    GI->>GI: Check user already has role
    GI->>GI: Check INVITATION_DISABLE_MULTIPLE_ROLES
    GI->>GI: Check no existing PermissionRequest (pending/approved)
    GI->>GI: Validate email/affiliation patterns

    alt Validation fails
      GI-->>U: 400 Bad Request
    else Validation passes
      GI->>PR: Create PermissionRequest
      alt auto_approve enabled
        PR->>PR: Auto-approve
        PR->>S: Grant role (with duplicate guard)
        PR-->>U: 200 OK (auto_approved: true)
      else Manual approval
        PR->>A: Notify approvers
        A->>PR: Approve/Reject
        alt Approved & auto_create_project
          PR->>S: Create project (excludes soft-deleted)
          S->>U: Grant project permission
        else Approved & normal
          PR->>S: Grant scope permission
        end
        PR->>U: Notify result
      end
    end
```

#### Public Group Invitations

Public group invitations are a special type of group invitation that can be viewed and requested by unauthenticated users. They are designed for open enrollment scenarios where organizations want to allow external users to request access to projects.

##### Key Characteristics

- **Unauthenticated visibility**: Listed in public API endpoints without authentication
- **Staff-only creation**: Only staff users can create and manage public invitations
- **Project-level access only**: Public invitations can only grant project-level roles, not customer-level roles
- **Automatic project creation**: All public invitations must use the auto-create project feature
- **Enhanced security**: Authentication is still required for submitting actual access requests

##### Constraints and Validation

1. **Staff authorization**: Only `is_staff=True` users can create public group invitations
2. **Auto-creation required**: Public invitations must have `auto_create_project=True`
3. **Project roles only**: Public invitations can only use roles starting with "PROJECT." (e.g., `PROJECT.MANAGER`, `PROJECT.ADMIN`)
4. **No customer-level access**: Cannot grant customer-level roles like `CUSTOMER.OWNER` or `CUSTOMER.SUPPORT`

##### Use Cases

- **Open research projects**: Universities allowing external researchers to request project access
- **Community initiatives**: Organizations providing project spaces for community members
- **Partner collaborations**: Companies offering project access to external partners
- **Educational platforms**: Schools providing project environments for students

## API Endpoints

### Individual Invitations (`/api/user-invitations/`)

- `POST /api/user-invitations/` - Create invitation
- `GET /api/user-invitations/` - List invitations
- `GET /api/user-invitations/{uuid}/` - Retrieve invitation details
- `POST /api/user-invitations/{uuid}/send/` - Resend invitation
- `POST /api/user-invitations/{uuid}/cancel/` - Cancel invitation
- `POST /api/user-invitations/{uuid}/accept/` - Accept invitation (authenticated)
- `POST /api/user-invitations/{uuid}/delete/` - Delete invitation (staff only)
- `POST /api/user-invitations/approve/` - Approve invitation (token-based)
- `POST /api/user-invitations/reject/` - Reject invitation (token-based)
- `POST /api/user-invitations/{uuid}/check/` - Check invitation validity (unauthenticated)
- `GET /api/user-invitations/{uuid}/details/` - Get invitation details for display

### Group Invitations (`/api/user-group-invitations/`)

- `POST /api/user-group-invitations/` - Create group invitation (authentication required)
- `GET /api/user-group-invitations/` - List group invitations (public invitations visible without authentication)
- `GET /api/user-group-invitations/{uuid}/` - Retrieve group invitation (public invitations accessible without authentication)
- `POST /api/user-group-invitations/{uuid}/cancel/` - Cancel group invitation (authentication required)
- `POST /api/user-group-invitations/{uuid}/submit_request/` - Submit access request (authentication required)
- `GET /api/user-group-invitations/{uuid}/projects/` - List available projects (authentication required)

### Permission Requests (`/api/user-permission-requests/`)

- `GET /api/user-permission-requests/` - List permission requests
- `GET /api/user-permission-requests/{uuid}/` - Retrieve permission request
- `POST /api/user-permission-requests/{uuid}/approve/` - Approve request
- `POST /api/user-permission-requests/{uuid}/reject/` - Reject request

## Model Fields and Relationships

### BaseInvitation (Abstract)

```python
class BaseInvitation:
    created_by: ForeignKey[User]      # Who created the invitation
    customer: ForeignKey[Customer]    # Associated customer (computed from scope)
    role: ForeignKey[Role]           # Role to be granted
    scope: GenericForeignKey         # Target scope (customer, project, etc.)
    created: DateTimeField           # Creation timestamp
    uuid: UUIDField                  # Unique identifier
```

### Invitation

```python
class Invitation(BaseInvitation):
    # State management
    state: CharField                 # Current invitation state
    execution_state: FSMField        # Background processing state

    # User identification
    email: EmailField               # Target email address
    civil_number: CharField         # Optional civil number for validation

    # User details (copied from invitation)
    full_name: CharField
    native_name: CharField
    phone_number: CharField
    organization: CharField
    job_title: CharField

    # Processing
    approved_by: ForeignKey[User]   # Staff member who approved
    error_message: TextField        # Processing errors
    extra_invitation_text: TextField # Custom message (max 250 chars)
```

### GroupInvitation

```python
class GroupInvitation(BaseInvitation):
    is_active: BooleanField         # Whether invitation is active
    is_public: BooleanField         # Allow unauthenticated users to see invitation
    auto_approve: BooleanField      # Auto-approve requests from matching users

    # User pattern matching
    user_email_patterns: JSONField  # Email patterns for matching users
    user_affiliations: JSONField    # Affiliation patterns
    user_identity_sources: JSONField  # Allowed identity providers (e.g., eduGAIN, SAML)

    # Project creation alternative
    auto_create_project: BooleanField      # Create project instead of customer role
    project_role: ForeignKey[Role]         # Role for auto-created project
    project_name_template: CharField       # Template for project naming
```

### PermissionRequest

```python
class PermissionRequest(ReviewMixin):
    invitation: ForeignKey[GroupInvitation]  # Associated group invitation
    created_by: ForeignKey[User]            # User requesting access

    # Review workflow (inherited from ReviewMixin)
    state: CharField                        # pending, approved, rejected
    reviewed_by: ForeignKey[User]
    reviewed_at: DateTimeField
    review_comment: TextField
```

## Permission System Integration

### Access Control

Invitation management permissions are controlled through:

1. **Staff privileges**: Staff users can manage all invitations
2. **Scope-based permissions**: Users with CREATE permissions on scopes can manage invitations
3. **Customer-level access**: Customer owners can manage invitations for their resources
4. **Hierarchical permissions**: Customer permissions apply to contained projects

### Permission Checks

The system uses `can_manage_invitation_with()` utility (`src/waldur_core/users/utils.py:179`) for authorization:

```python
def can_manage_invitation_with(request, scope):
    if request.user.is_staff:
        return True

    permission = get_create_permission(scope)
    if has_permission(request, permission, scope):
        return True

    customer = get_customer(scope)
    if has_permission(request, permission, customer):
        return True
```

### Filtering and Visibility

- **InvitationFilterBackend**: Filters invitations based on user permissions
- **GroupInvitationFilterBackend**: Controls group invitation visibility, allows public invitations for unauthenticated users
- **PendingInvitationFilter**: Filters invitations user can accept
- **VisibleInvitationFilter**: Controls invitation detail visibility

## Duplicate Role Prevention

The invitation system enforces multiple layers of protection against granting duplicate roles.
These checks apply to both individual and group invitation flows.

### Validation Layers

```mermaid
flowchart TD
    A[User submits group invitation request] --> B{has_user check:<br/>User already has this role?}
    B -->|Yes| R1[Reject: User already has this role in the scope]
    B -->|No| C{INVITATION_DISABLE_MULTIPLE_ROLES<br/>and user has any role in scope?}
    C -->|Yes| R2[Reject: User already has role within this scope]
    C -->|No| D{Existing PermissionRequest<br/>pending or approved?}
    D -->|Yes| R3[Reject: Permission request already exists for this scope]
    D -->|No| E[Create PermissionRequest]
    E --> F{Auto-approve enabled?}
    F -->|Yes| G[Approve immediately]
    F -->|No| H[Wait for manual approval]
    G --> I{has_user guard in approve:<br/>User already has role?}
    H --> I
    I -->|Yes| S[Skip role creation silently]
    I -->|No| J[Grant role via add_user]
```

#### Layer 1: `has_user()` Check in `submit_request`

Before creating a `PermissionRequest`, the system checks whether the user already holds the
exact role being requested in the target scope. This mirrors the check in individual invitation
acceptance (`InvitationViewSet.accept()`).

#### Layer 2: `INVITATION_DISABLE_MULTIPLE_ROLES` Check

When `INVITATION_DISABLE_MULTIPLE_ROLES=True` (Constance setting), a user cannot hold
**any** active role in the same scope. This prevents a user from accumulating multiple different
roles (e.g., both OWNER and SUPPORT) in a single customer or project. Applies to both
individual and group invitation flows.

#### Layer 3: Existing PermissionRequest Check

The system checks for existing `PermissionRequest` records in `PENDING` or `APPROVED` state
for the same user and scope. This prevents a user from submitting multiple requests even if
the first was auto-approved and already transitioned out of `PENDING` state.

#### Layer 4: Defense-in-Depth in `approve()`

The `PermissionRequest.approve()` method performs a final `has_user()` check before calling
`add_user()`. This catches edge cases where overlapping requests from different group
invitations target the same scope and role. If the user already has the role, approval
completes silently without creating a duplicate.

### Soft-Deleted Project Handling

When `auto_create_project=True`, the system uses `Project.available_objects.get_or_create()`
instead of `Project.objects.get_or_create()`. This ensures soft-deleted projects (with
`is_removed=True`) are excluded, and a fresh project is created if the matching project was
previously deleted.

## User Restrictions

Customers and Projects can define user restrictions that control which users can be added as members. These restrictions apply to both direct membership (via `add_user` API) and invitation acceptance. GroupInvitations can add additional restrictions on top of scope restrictions but cannot bypass them.

### Restriction Fields

Both Customer and Project models support the following restriction fields:

```python
# Available on Customer, Project, and GroupInvitation models
user_email_patterns: JSONField      # Regex patterns for allowed emails
user_affiliations: JSONField        # List of allowed affiliations
user_identity_sources: JSONField    # List of allowed identity providers

# AAI-based filtering (also available on Customer, Project, and GroupInvitation)
user_nationalities: JSONField       # List of allowed nationality codes (ISO 3166-1 alpha-2)
user_organization_types: JSONField  # List of allowed organization type URNs (SCHAC)
user_assurance_levels: JSONField    # List of required assurance URIs (REFEDS)
```

### Validation Logic

Restrictions use **OR logic within a field** and **AND logic across fields and levels**:

- **Within a field**: User matches if ANY email pattern OR ANY affiliation OR ANY identity source matches
- **Across fields**: User must pass ALL fields that have restrictions set (e.g., if both email patterns and affiliations are set, user must match at least one of each)
- **Across levels**: User must pass ALL levels that have restrictions set (Customer → Project → GroupInvitation)

**Special AAI validation rules:**

- **Nationalities**: User must have at least one nationality in the allowed list (checks both `nationality` and `nationalities` fields)
- **Organization types**: User's `organization_type` must be in the allowed list
- **Assurance levels**: User must have ALL required assurance URIs (AND logic, not OR)

```mermaid
flowchart TD
    A[User attempts to join] --> B{Customer has restrictions?}
    B -->|Yes| C{User matches Customer restrictions?}
    B -->|No| D{Project has restrictions?}
    C -->|No| REJECT[Rejected - Customer restrictions not met]
    C -->|Yes| D
    D -->|Yes| E{User matches Project restrictions?}
    D -->|No| F{GroupInvitation has restrictions?}
    E -->|No| REJECT2[Rejected - Project restrictions not met]
    E -->|Yes| F
    F -->|Yes| G{User matches GroupInvitation restrictions?}
    F -->|No| ALLOW[Allowed]
    G -->|No| REJECT3[Rejected - GroupInvitation restrictions not met]
    G -->|Yes| ALLOW
```

### Cascade Validation Table

| Customer | Project | GroupInvitation | User Must Match |
|----------|---------|-----------------|-----------------|
| No restrictions | No restrictions | No restrictions | Anyone allowed |
| Has restrictions | No restrictions | No restrictions | Customer only |
| No restrictions | Has restrictions | No restrictions | Project only |
| Has restrictions | Has restrictions | No restrictions | Customer AND Project |
| Has restrictions | Has restrictions | Has restrictions | Customer AND Project AND GroupInvitation |

### Permission to Set Restrictions

| Scope | Who Can Set Restrictions |
|-------|-------------------------|
| Customer | Staff users only (`is_staff=True`) |
| Project | Users with `CREATE_PROJECT` permission on customer |
| GroupInvitation | Invitation creator (must respect scope restrictions) |

### Examples

#### Customer-Level Email Restriction

```python
# Only users from specific domains can join this customer
customer.user_email_patterns = [".*@university.edu", ".*@research.org"]
customer.save()

# User with email "john@university.edu" can be added - matches pattern
# User with email "jane@gmail.com" cannot be added - no pattern match
```

#### Project-Level Affiliation Restriction

```python
# Project requires staff or faculty affiliation
project.user_affiliations = ["staff", "faculty"]
project.save()

# User with affiliations=["staff"] can be added
# User with affiliations=["student"] cannot be added
```

#### Identity Source Restriction

```python
# Only allow users authenticated via specific identity providers
customer.user_identity_sources = ["eduGAIN", "SAML"]
customer.save()

# User with identity_source="eduGAIN" can be added
# User with identity_source="local" cannot be added
```

#### Nationality Restriction (AAI)

```python
# Only allow users from EU member states
project.user_nationalities = ["DE", "FR", "IT", "ES", "NL", "BE", "AT", "PL"]
project.save()

# User with nationality="DE" or nationalities=["DE", "US"] can be added
# User with nationality="US" and nationalities=["US"] cannot be added
```

#### Organization Type Restriction (AAI)

```python
# Only allow users from universities or research institutions
customer.user_organization_types = [
    "urn:schac:homeOrganizationType:int:university",
    "urn:schac:homeOrganizationType:int:research-institution"
]
customer.save()

# User with organization_type="urn:schac:homeOrganizationType:int:university" can be added
# User with organization_type="urn:schac:homeOrganizationType:int:company" cannot be added
```

#### Assurance Level Restriction (AAI)

```python
# Require high assurance level for sensitive projects
project.user_assurance_levels = [
    "https://refeds.org/assurance/IAP/high",
    "https://refeds.org/assurance/ID/eppn-unique-no-reassign"
]
project.save()

# User must have BOTH assurance URIs in their eduperson_assurance list
# This ensures strong identity verification from the identity provider
```

#### Combined Customer and Project Restrictions

```python
# Customer requires university email
customer.user_email_patterns = [".*@university.edu"]
customer.save()

# Project within customer requires staff affiliation
project.user_affiliations = ["staff"]
project.save()

# User must match BOTH:
# - Email must match .*@university.edu
# - Affiliation must include "staff"
```

### Important Notes

1. **Staff users are NOT exempt**: Restrictions apply to all users including staff
2. **Empty restrictions allow all**: If no restrictions are set, any user is allowed
3. **GroupInvitation inherits scope restrictions**: GroupInvitation cannot bypass Customer/Project restrictions
4. **Validation occurs at multiple points**:
   - Direct membership via `POST /customers/{uuid}/add_user/` or `POST /projects/{uuid}/add_user/`
   - Invitation acceptance via `POST /invitations/{uuid}/accept/`
   - GroupInvitation request via `POST /group-invitations/{uuid}/submit_request/`
   - PermissionRequest approval

## Background Processing

### Celery Tasks

The invitation system uses several background tasks (`src/waldur_core/users/tasks.py`):

#### Core Processing Tasks

- `process_invitation`: Main processing entry point
- `send_invitation_created`: Send invitation emails/webhooks
- `get_or_create_user`: Create user accounts for invitations
- `send_invitation_requested`: Notify staff of invitation requests

#### Maintenance Tasks

- `cancel_expired_invitations`: Clean up expired invitations
- `cancel_expired_group_invitations`: Clean up expired group invitations
- `process_pending_project_invitations`: Activate invitations for started projects
- `send_reminder_for_pending_invitations`: Send reminder emails

#### Notification Tasks

- `send_invitation_rejected`: Notify creators of rejections
- `send_mail_notification_about_permission_request_has_been_submitted`: Notify approvers

### Execution States

Individual invitations track background processing with FSM states:

- `SCHEDULED`: Initial state, queued for processing
- `PROCESSING`: Currently being processed
- `OK`: Successfully processed
- `ERRED`: Processing failed with error details

### Error Handling

The system provides robust error tracking:

- **Error messages**: Human-readable error descriptions
- **Error tracebacks**: Full stack traces for debugging
- **Retry mechanisms**: Failed invitations can be resent
- **Webhook failover**: Falls back to email if webhooks fail

## Configuration Options

### Core Settings (`WALDUR_CORE`)

```python
# Invitation lifecycle
INVITATION_LIFETIME = timedelta(weeks=1)        # Individual invitation expiration
INVITATION_MAX_AGE = 60 * 60 * 24 * 7          # Token validity period
# Note: Group invitations do not expire

# User creation
INVITATION_CREATE_MISSING_USER = False         # Auto-create user accounts
ONLY_STAFF_CAN_INVITE_USERS = False           # Require staff approval

# Validation
VALIDATE_INVITATION_EMAIL = False              # Strict email matching
```

### Constance Settings

```python
# Runtime configuration
ENABLE_STRICT_CHECK_ACCEPTING_INVITATION = True   # Enforce email matching on individual invitations
INVITATION_DISABLE_MULTIPLE_ROLES = False         # Prevent multiple roles in same scope
                                                  # (applies to both individual and group invitations)
```

### Webhook Integration

```python
# External system integration
INVITATION_USE_WEBHOOKS = False                   # Enable webhook delivery
INVITATION_WEBHOOK_URL = ""                       # Target webhook URL
INVITATION_WEBHOOK_TOKEN_URL = ""                 # OAuth token endpoint
INVITATION_WEBHOOK_TOKEN_CLIENT_ID = ""           # OAuth client ID
INVITATION_WEBHOOK_TOKEN_SECRET = ""              # OAuth client secret
```

## Email Templates

The system uses several email templates (`waldur_core/users/templates/`):

- `invitation_created` - New invitation notification
- `invitation_requested` - Staff approval request
- `invitation_rejected` - Rejection notification
- `invitation_expired` - Expiration notification
- `invitation_approved` - Auto-created user credentials
- `permission_request_submitted` - Permission request notification

## Notifications

Group invitations rely on the marketplace notification framework
(`users.*` keys registered in `waldur_core/structure/notifications.py`). Each
notification maps to a database `Notification` row that staff can toggle and
whose templates they can override from the UI
(**Support → User management → Notifications**, backed by
`/api/notification-messages/`). If a notification is **disabled**, delivery is
silently skipped — this is the first thing to check when an expected email is
not received.

### When a request to join is submitted

When a user submits a request against a group invitation, a
`PermissionRequest` is created and transitioned to `PENDING`. The
`post_save` handler
`create_notification_about_permission_request_has_been_submitted`
(`waldur_core/users/handlers.py`) queues
`send_mail_notification_about_permission_request_has_been_submitted`
(`waldur_core/users/tasks.py`) on transaction commit, which sends the
`users.permission_request_submitted` notification to the approvers.

#### Recipient resolution

Recipients are resolved in
`get_users_for_notification_about_request_has_been_submitted` and
`get_customer_notification_emails`
(`waldur_core/users/utils.py`) and combined as follows:

| Priority | Recipient set | How it is resolved |
|----------|---------------|--------------------|
| 1 | **Approvers** | Users holding a role that grants the scope's *create permission* (e.g. `CREATE_CUSTOMER_PERMISSION` for a customer-scoped invitation, `CREATE_PROJECT_PERMISSION` for a project-scoped one). For project scopes, the parent customer's qualifying owners are included too. Users with a blank email or `notifications_enabled=False` are excluded. |
| 2 | **Organization contacts** | The invitation customer's contact `email` field plus every address in its comma-separated `notification_emails` field. |
| 3 | **Staff (fallback)** | Active staff users with a valid email — used **only** when priorities 1 and 2 yield no recipients. |

The final list is the union of priorities 1 and 2 (de-duplicated, blanks
dropped); if that union is empty it falls back to priority 3. If even staff are
unavailable, a warning is logged and no email is sent.

This means an organization can be notified about join requests even when no
member currently holds the approver permission — by setting the organization's
**contact email** or **notification emails**. Those fields are configured on
the organization (**Edit organization → Contact information**).

!!! note
    `auto_approve` invitations still emit `permission_request_submitted` to the
    approvers before the request is immediately approved, because `submit()`
    runs before `approve()`.

### When a request is rejected

Rejecting a `PermissionRequest` triggers
`create_notification_about_permission_request_has_been_rejected`
→ `send_mail_notification_about_permission_request_has_been_rejected`, which
sends `users.permission_request_rejected` to the requester
(`permission_request.created_by`) only.

### Individual invitation requests (for contrast)

The separate individual-invitation flow
(`ONLY_STAFF_CAN_INVITE_USERS`) uses `send_invitation_requested` and the
`users.invitation_requested` template, which notifies **staff only** — it does
not consult organization owners or the customer notification emails.

## Advanced Features

### Auto-Approval

Group invitations with `auto_approve=True` skip manual review and immediately approve
matching users. When a user submits a request:

1. The system validates patterns (email, affiliation, identity source)
2. Creates a `PermissionRequest` in `PENDING` state
3. Immediately transitions it to `APPROVED`
4. Grants the role (subject to duplicate role prevention checks)

All duplicate role prevention layers still apply to auto-approved requests.

### Project Auto-Creation

Group invitations can automatically create projects instead of granting customer-level access:

```python
# Configuration
auto_create_project = True
project_role = ProjectRole.MANAGER
project_name_template = "{user.full_name} Project"

# On approval, creates:
# 1. New project with resolved name (excludes soft-deleted projects)
# 2. Project-level role assignment
# 3. Proper permission hierarchy
```

### Pattern Matching

Group invitations support sophisticated user matching:

```python
# Email patterns (regex)
user_email_patterns = [".*@company.com", ".*@university.edu"]

# Affiliation patterns (exact match)
user_affiliations = ["staff", "student", "faculty"]

# Identity sources (exact match)
user_identity_sources = ["eduGAIN", "SAML", "local"]

# Validation logic in GroupInvitation.get_objects_by_user_patterns()
# Uses OR logic: user matches if ANY email pattern OR ANY affiliation OR ANY identity source matches
```

### Token-Based Security

Staff approval uses cryptographically signed tokens:

```python
# Token format: {user_uuid}.{invitation_uuid}
signer = TimestampSigner()
token = signer.sign(f"{user.uuid.hex}.{invitation.uuid.hex}")

# Tokens expire based on INVITATION_MAX_AGE setting
# Invalid tokens raise ValidationError with descriptive messages
```

## Security Considerations

### Civil Number Validation

When `civil_number` is provided:

- Only users with matching civil numbers can accept invitations
- Provides additional security layer for sensitive resources
- Empty civil numbers allow any user to accept

### Email Validation

Multiple levels of email validation:

1. **Loose matching** (default): Case-insensitive email comparison
2. **Strict validation**: Exact email matching when `ENABLE_STRICT_CHECK_ACCEPTING_INVITATION=True`
3. **Pattern matching**: Group invitations validate against email patterns

### Token Security

- **Cryptographic signing**: Uses Django's TimestampSigner
- **Time-based expiration**: Tokens expire after configurable period
- **Payload validation**: Validates UUID formats and user/invitation existence
- **State verification**: Ensures invitations are in correct state for operation

### Permission Isolation

- **Scope-based filtering**: Users only see invitations they can manage
- **Role validation**: Ensures roles match scope content types, with additional constraints for public invitations
- **Customer isolation**: Prevents cross-customer invitation access
- **Public invitation constraints**: Public invitations restricted to project-level roles only

## Best Practices

### Creating Invitations

1. **Validate scope-role compatibility** before creating invitations
2. **Set appropriate expiration times** based on use case sensitivity
3. **Use civil numbers** for high-security invitations
4. **Include helpful extra_invitation_text** for user context

### Group Invitation Setup

1. **Design clear email patterns** that match intended user base
2. **Choose appropriate role mappings** for auto-created projects
3. **Set meaningful project name templates** for clarity
4. **Configure proper approval workflows** with designated approvers

### Public Invitation Management

1. **Restrict to staff users only** - Only allow trusted staff to create public invitations
2. **Use project-level roles exclusively** - Never grant customer-level access through public invitations
3. **Design clear project naming** - Use descriptive templates since multiple projects may be created
4. **Monitor request volume** - Public invitations may generate high volumes of access requests
5. **Set up proper approval processes** - Ensure adequate staffing to handle public invitation approvals

### Error Handling

1. **Monitor execution states** for processing failures
2. **Set up alerts** for invitation processing errors
3. **Provide clear error messages** to users and administrators
4. **Implement retry strategies** for transient failures

### Performance Optimization

1. **Use bulk operations** for large invitation batches
2. **Index frequently queried fields** (email, state, customer)
3. **Archive old invitations** to prevent table bloat
4. **Monitor background task queues** for processing bottlenecks

## Troubleshooting

### Common Issues

1. **Invitations stuck in PROCESSING state**
  - Check Celery task processing
  - Review error messages in invitation records
  - Verify SMTP/webhook configuration

2. **Users can't accept invitations**
  - Verify email matching settings
  - Check civil number requirements
  - Confirm invitation hasn't expired

3. **Permission denied errors**
  - Validate user has CREATE permissions on scope
  - Check customer-level permissions for hierarchical access
  - Confirm role is compatible with scope type

4. **Group invitation requests not working**
  - Verify email patterns match user addresses
  - Check affiliation matching logic
  - Confirm invitation is still active

5. **"User already has this role" on group invitation submit**
  - User already holds the requested role in the target scope
  - Check if user was previously granted the role via individual invitation or direct assignment
  - If `INVITATION_DISABLE_MULTIPLE_ROLES=True`, the user may hold a different role in the same scope

### Debugging Tools

1. **Admin interface**: View invitation details and states
2. **Celery monitoring**: Track background task execution
3. **Logging**: Enable debug logging for invitation processing
4. **API introspection**: Use `/api/user-invitations/{uuid}/details/` for status checking

## Integration Examples

### Basic Individual Invitation

```python
# Create invitation
invitation = Invitation.objects.create(
    email="user@example.com",
    scope=customer,
    role=CustomerRole.OWNER,
    created_by=current_user,
    extra_invitation_text="Welcome to our platform!"
)

# Process in background
process_invitation.delay(invitation.uuid.hex, sender_name)
```

### Group Invitation with Auto-Project

```python
# Create group invitation
group_invitation = GroupInvitation.objects.create(
    scope=customer,
    role=CustomerRole.OWNER,
    auto_create_project=True,
    project_role=ProjectRole.MANAGER,
    project_name_template="{user.full_name}'s Research Project",
    user_email_patterns=["*@university.edu"],
    created_by=admin_user
)

# Users can submit requests that create projects on approval
```

### Public Group Invitation

```python
# Create public group invitation (staff only)
public_invitation = GroupInvitation.objects.create(
    scope=customer,
    role=ProjectRole.MANAGER,  # Must be project-level role
    is_public=True,            # Makes it visible to unauthenticated users
    auto_create_project=True,  # Required for public invitations
    project_role=ProjectRole.MANAGER,
    project_name_template="{user.full_name} Research Project",
    user_email_patterns=["*@university.edu", "*@research.org"],
    created_by=staff_user      # Must be staff user
)

# Unauthenticated users can list and view this invitation
# Authentication is required only for submitting actual requests
```

### Webhook Integration

```python
# Configure webhook delivery
settings.WALDUR_CORE.update({
    'INVITATION_USE_WEBHOOKS': True,
    'INVITATION_WEBHOOK_URL': 'https://external-system.com/invitations/',
    'INVITATION_WEBHOOK_TOKEN_URL': 'https://auth.external-system.com/token',
    'INVITATION_WEBHOOK_TOKEN_CLIENT_ID': 'waldur-client',
    'INVITATION_WEBHOOK_TOKEN_SECRET': 'secret-key',
})

# Invitations will be posted to external system instead of email
```

This invitation system provides flexible, secure, and scalable user onboarding capabilities that integrate seamlessly with Waldur's permission and organizational structure.
