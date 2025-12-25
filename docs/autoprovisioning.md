# Auto-Provisioning

Waldur's auto-provisioning feature automatically creates projects and provisions resources for new users based on predefined rules. This capability streamlines user onboarding by eliminating manual setup processes.

## Overview

Auto-provisioning works by matching new users against configured rules based on email patterns or affiliations. When a matching rule is found, the system:

1. Creates or assigns users to projects
2. Grants appropriate project roles
3. Optionally provisions marketplace resources
4. Processes orders automatically

## Core Components

### Rule Model

The `Rule` model (`src/waldur_autoprovisioning/models.py:11`) defines auto-provisioning configurations with the following key fields:

- **customer**: Target customer for project creation (optional when using organization mapping)
- **plan**: Optional marketplace plan to provision
- **plan_attributes**: Custom attributes for resource provisioning
- **plan_limits**: Resource limits (e.g., `{"vcpu": 4, "ram": 8192, "storage": 100}`)
- **project_role**: Role assigned to users in created projects
- **use_user_organization_as_customer_name**: Map user's organization claim to existing customer
- **project_name_template**: Template for project naming (e.g., `"{username}_workspace"`)

### User Matching

Rules use the `UserDetailsMatchMixin` for pattern matching:

- **user_email_patterns**: Regex patterns for email matching (e.g., `[".+@example.com"]`)
- **user_affiliations**: Organization affiliations for matching (e.g., `["staff", "faculty"]`)
- **user_identity_sources**: Identity provider matching (e.g., `["eduGAIN", "SAML"]`)

Pattern matching uses OR logic: a user matches if ANY email pattern OR ANY affiliation OR ANY identity source matches. Pattern matching supports standard regex syntax for email patterns and handles invalid patterns gracefully.

## Organization Mapping Feature

### Organization Mapping Overview

The organization mapping feature (added in commit 77c31bb25) allows auto-provisioning rules to dynamically resolve customers based on user organization claims from identity providers. This enables multi-tenant scenarios where each organization has its own customer in Waldur.

### How It Works

When `use_user_organization_as_customer_name` is enabled:

1. System extracts organization claim from user's identity provider data
2. Looks up existing customer with matching name
3. Creates project under the resolved customer
4. Validates user has protected details flag set

### Protected User Details

For security, organization mapping requires users to have protected details:

```python
@property
def should_protect_user_details(self) -> bool:
    """Return True if user profile fields must be read-only."""
    protected_methods = django_settings.WALDUR_CORE[
        "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS"
    ]
    return bool(
        self.registration_method and self.registration_method in protected_methods
    )
```

### Customer Resolution Logic

From `src/waldur_autoprovisioning/handlers.py:33`:

```python
if not rule.use_user_organization_as_customer_name:
    if not rule.customer:
        logger.warning("Rule has no customer configured")
        return
    customer = rule.customer
else:
    if not user.should_protect_user_details:
        logger.warning("User not marked as protected for organization-based rules")
        return

    if not user.organization:
        logger.warning("User has no organization claim")
        return

    customers = Customer.objects.filter(name=user.organization)
    if not customers:
        logger.warning("No Customer found with name='%s'", user.organization)
        return
    elif customers.count() > 1:
        logger.warning("Multiple Customers found with name='%s'", user.organization)
        return
    else:
        customer = customers.first()
```

## API Endpoints

### Rules Management

**Endpoint**: `/api/autoprovisioning-rules/`

**Permissions**:

- List/Read: Customer role permissions
- Create/Update/Delete: Staff only

**Serialization**: `RuleSerializer` provides comprehensive API access with related object details:

```json
{
    "name": "University Users Rule",
    "uuid": "...",
    "user_email_patterns": [".+@university\\.edu"],
    "user_affiliations": ["staff", "faculty"],
    "user_identity_sources": ["eduGAIN"],
    "customer": "customer-uuid",
    "use_user_organization_as_customer_name": false,
    "project_role": "role-uuid",
    "project_role_display_name": "Admin",
    "plan": "plan-uuid",
    "plan_attributes": {"flavor": "m1.small"},
    "plan_limits": {"vcpu": 2, "ram": 4096}
}
```

### Validation Rules

The serializer enforces these validation constraints:

- Either `customer` or `use_user_organization_as_customer_name=true` must be specified
- Either `project_role` or `project_role_name` must be provided (but not both)
- Project role must be valid for project-level permissions
- Email patterns must be valid regex expressions

## Processing Flow

### Trigger Mechanism

Auto-provisioning activates via Django signal (`src/waldur_autoprovisioning/apps.py:13`):

```python
signals.post_save.connect(
    handlers.handle_new_user,
    sender=User,
    dispatch_uid="waldur_autoprovisioning.handle_new_user",
)
```

### Auto-Provisioning Workflow

1. **User Creation**: New user triggers `handle_new_user` handler
2. **Rule Matching**: System finds applicable rules using `Rule.get_objects_by_user_patterns()`
3. **Customer Resolution**: Either use configured customer or resolve from organization
4. **Project Creation**: `get_or_create_project()` creates or assigns project
5. **Resource Provisioning**: If plan is specified, creates marketplace order
6. **Order Processing**: Marketplace processes the order asynchronously

## Configuration Examples

### Basic Project Creation

Create projects without resources:

```json
{
    "name": "Basic Project Rule",
    "user_email_patterns": [".+@company\\.com"],
    "customer": "company-customer-uuid",
    "project_role": "admin-role-uuid"
}
```

### Resource Provisioning

Auto-provision OpenStack tenants:

```json
{
    "name": "OpenStack Auto-Provision",
    "user_email_patterns": [".+@research\\.org"],
    "customer": "research-customer-uuid",
    "plan": "openstack-plan-uuid",
    "plan_limits": {
        "vcpu": 8,
        "ram": 16384,
        "storage": 500
    },
    "plan_attributes": {
        "flavor": "m1.large",
        "network_config": "private"
    }
}
```

### Organization-Based Provisioning

Use user's organization for customer assignment:

```json
{
    "name": "Organization Rule",
    "user_email_patterns": [".+@.*\\.edu"],
    "use_user_organization_as_customer_name": true,
    "project_name_template": "{username}_research_project"
}
```

### Multi-Tenant Academic Setup

Support multiple universities with their own customers:

```json
{
    "name": "Academic Institutions",
    "user_email_patterns": [".+@.*\\.edu", ".+@.*\\.ac\\.[a-z]{2}"],
    "use_user_organization_as_customer_name": true,
    "project_role_name": "PROJECT.ADMIN",
    "plan": "basic-research-plan-uuid",
    "plan_limits": {
        "vcpu": 4,
        "ram": 8192,
        "storage": 200
    }
}
```

## Security and Validation

### Protected Users

Organization-based provisioning requires protected user details to ensure organization claims come from trusted identity providers and cannot be manipulated by users.

### Input Validation

- Email patterns validated as proper regex
- Project role must be valid project-level role
- Customer or organization requirement enforced
- Plan compatibility verified
- Mutual exclusion of customer specification methods

### Permission Controls

- Rules managed by staff users only
- Customer-scoped access for viewing
- Project creation respects customer permissions

## Monitoring and Logging

The system provides comprehensive logging for troubleshooting:

- Invalid regex patterns logged and skipped
- Missing organization claims logged
- Multiple customer matches warned
- Order creation and processing tracked
- Protected user validation failures logged

## Integration Points

### Marketplace Integration

Auto-provisioning integrates with Waldur's marketplace:

- Uses `marketplace_utils.generate_resource_name()` for naming
- Creates `Resource` and `Order` objects
- Triggers `process_order_on_commit()` for async processing
- Respects marketplace offering types and constraints

### User Management Integration

- Hooks into user creation process
- Respects user protection settings
- Leverages organization claims from identity providers
- Integrates with role-based access control

### Identity Provider Integration

- Reads organization claims from SAML/OIDC providers
- Validates user registration method for security
- Maps organization names to existing customers
- Supports multi-tenant identity scenarios

## Best Practices

1. **Rule Design**: Create specific rules for different user groups
2. **Organization Mapping**: Ensure customer names match organization claims exactly
3. **Naming Templates**: Use descriptive project naming templates
4. **Resource Limits**: Set appropriate defaults for auto-provisioned resources
5. **Monitoring**: Monitor logs for failed provisioning attempts
6. **Security**: Configure protected registration methods for organization-based rules
7. **Multi-Tenancy**: Use organization mapping for SaaS scenarios with multiple customers
