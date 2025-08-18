# Role-based Access Control (RBAC)

## Overview

Waldur implements a comprehensive Role-Based Access Control (RBAC) system that determines what actions users can perform within the platform. The authorization system consists of three core components:

1. **Permissions** - Unique strings that designate specific actions (e.g., `OFFERING.CREATE`, `PROJECT.UPDATE`)
2. **Roles** - Named collections of permissions (e.g., `CUSTOMER.OWNER`, `PROJECT.ADMIN`)
3. **User Roles** - Assignments linking users to roles within specific scopes

This functionality is implemented in the `waldur_core.permissions` application and provides fine-grained access control across all platform resources.

First thing to remember is to use `PermissionEnum` to define permissions instead of using plain string or standalone named constant, otherwise they would not be pushed to frontend.

```python
# src/waldur_core/permissions/enums.py
class PermissionEnum(str, Enum):
  CREATE_OFFERING = 'OFFERING.CREATE'
  UPDATE_OFFERING = 'OFFERING.UPDATE'
  DELETE_OFFERING = 'OFFERING.DELETE'
```

Next, let's assign that permissions to role.

```python
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.enums import PermissionEnum

CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_OFFERING)
```

Now, let's assign customer owner role to particular user and customer:

```python
from django.contrib.auth import get_user_model
from waldur_core.structure.models import Customer

User = get_user_model()

user = User.objects.last()
customer = Customer.objects.last()
customer.add_user(user, CustomerRole.OWNER)
```

Finally, we can check whether user is allowed to create offering in particular organization.

```python
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import has_permission

has_permission(request, PermissionEnum.CREATE_OFFERING, customer)
```

Please note that this function accepts not only customer, but also project and offering as a scope.
Consider these models as authorization aggregates. Other models, such as resources and orders, should refer to these aggregates to perform authorization check. For example:

```python
has_permission(request, PermissionEnum.SET_RESOURCE_USAGE, resource.offering.customer)
```

## Core Concepts

### Authorization Scopes

Waldur supports multiple authorization scopes, each representing a different organizational level:

| Scope Type | Model | Description |
|------------|-------|-------------|
| Customer | `structure.Customer` | Organization-level permissions |
| Project | `structure.Project` | Project-level permissions within an organization |
| Offering | `marketplace.Offering` | Service offering permissions |
| Service Provider | `marketplace.ServiceProvider` | Provider-level permissions |
| Call | `proposal.Call` | Call for proposals permissions |
| Proposal | `proposal.Proposal` | Individual proposal permissions |

### System Roles

The platform includes several predefined system roles:

#### Customer Roles

- `CUSTOMER.OWNER` - Full control over the organization
- `CUSTOMER.SUPPORT` - Support access to organization resources
- `CUSTOMER.MANAGER` - Management capabilities for service providers

#### Project Roles

- `PROJECT.ADMIN` - Full project administration
- `PROJECT.MANAGER` - Project management capabilities
- `PROJECT.MEMBER` - Basic project member access

#### Offering Roles

- `OFFERING.MANAGER` - Manage marketplace offerings

#### Call/Proposal Roles

- `CALL.REVIEWER` - Review proposals in calls
- `CALL.MANAGER` - Manage calls for proposals
- `PROPOSAL.MEMBER` - Proposal team member
- `PROPOSAL.MANAGER` - Proposal management

### Role Features

#### Time-based Roles

Roles can have expiration times, allowing for temporary access:

```python
from waldur_core.permissions.utils import add_user
from datetime import datetime, timedelta

expiration = datetime.now() + timedelta(days=30)
add_user(
    scope=project,
    user=user,
    role=ProjectRole.MEMBER,
    expiration_time=expiration
)
```

#### Role Revocation

Roles can be explicitly revoked before expiration:

```python
from waldur_core.permissions.utils import delete_user

delete_user(
    scope=project,
    user=user,
    role=ProjectRole.MEMBER,
    current_user=request.user
)
```

## Migration example

Previously we have relied on hard-coded roles, such as customer owner and project manager. Migration to dynamic roles on backend is relatively straightforward process. Consider the following example.

```python
class ProviderPlanViewSet:
  archive_permissions = [structure_permissions.is_owner]
```

As you may see, we have relied on selectors with hard-coded roles. The main drawback of this approach is that it is very hard to inspect who can do what without reading all source code. And it is even hard to adjust this behaviour. Contrary to its name, by using dynamic roles we don't need to care much about roles though.

```python
class ProviderPlanViewSet:
  archive_permissions = [
    permission_factory(
      PermissionEnum.ARCHIVE_OFFERING_PLAN,
      ['offering.customer'],
    )
  ]
```

Here we use `permission_factory` function which accepts permission string and list of paths to scopes, either customer, project or offering. It returns function which accepts request and raises an exception if user doesn't have specified permission in roles connected to current user and one of these scopes.

## Permissions for viewing

Usually it is implemented filter backend, such as `GenericRoleFilter`, which in turn uses `get_connected_customers` and `get_connected_projects` function because customer and project are two main permission aggregates.

```python
class PaymentProfileViewSet(core_views.ActionsViewSet):
    filter_backends = (
        structure_filters.GenericRoleFilter,
        DjangoFilterBackend,
        filters.PaymentProfileFilterBackend,
    )
```

Although this approach works fine for trivial use cases, often enough permission filtering logic is more involved and we implement `get_queryset` method instead.

```python
class OfferingUserGroupViewSet(core_views.ActionsViewSet):
  def get_queryset(self):
      queryset = super().get_queryset()
      current_user = self.request.user
      if current_user.is_staff or current_user.is_support:
        return queryset

      projects = get_connected_projects(current_user)
      customers = get_connected_customers(current_user)

      subquery = (
        Q(projects__customer__in=customers)
        | Q(offering__customer__in=customers)
        | Q(projects__in=projects)
      )
      return queryset.filter(subquery)
```

## Permissions for object creation and update

Usually it is done in serializer's validate method.

```python
class RobotAccountSerializer:
  def validate(self, validated_data):
    request = self.context['request']
    if self.instance:
      permission = PermissionEnum.UPDATE_RESOURCE_ROBOT_ACCOUNT
    else:
      permission = PermissionEnum.CREATE_RESOURCE_ROBOT_ACCOUNT

    if not has_permission(request, permission, resource.offering.customer):
      raise PermissionDenied()
```

## Permission Checking Utilities

### Core Functions

#### `has_permission(request, permission, scope)`

Checks if a user has a specific permission in a given scope:

```python
from waldur_core.permissions.utils import has_permission
from waldur_core.permissions.enums import PermissionEnum

# Check if user can create an offering
if has_permission(request, PermissionEnum.CREATE_OFFERING, customer):
    # User has permission
    pass
```

**Note:** Staff users automatically pass all permission checks.

#### `permission_factory(permission, sources=None)`

Creates a permission checker function for use in ViewSets:

```python
from waldur_core.permissions.utils import permission_factory
from waldur_core.permissions.enums import PermissionEnum

class ResourceViewSet:
    update_permissions = [
        permission_factory(
            PermissionEnum.UPDATE_RESOURCE,
            ['offering.customer', 'project.customer']
        )
    ]
```

The `sources` parameter specifies paths to traverse from the current object to find the scope.

### User and Role Management

#### Getting Users with Roles

```python
from waldur_core.permissions.utils import get_users, get_users_with_permission

# Get all users with any role in a project
users = get_users(project)

# Get users with specific role
managers = get_users(project, role_name='PROJECT.MANAGER')

# Get users with specific permission
can_update = get_users_with_permission(project, PermissionEnum.UPDATE_PROJECT)
```

#### Managing User Roles

```python
from waldur_core.permissions.utils import add_user, update_user, delete_user, has_user

# Add user to role
permission = add_user(
    scope=project,
    user=user,
    role=ProjectRole.MEMBER,
    created_by=request.user,
    expiration_time=None  # Permanent role
)

# Check if user has role
if has_user(project, user, ProjectRole.MEMBER):
    print("User is a project member")

# Update role expiration
update_user(
    scope=project,
    user=user,
    role=ProjectRole.MEMBER,
    expiration_time=new_expiration,
    current_user=request.user
)

# Remove user from role
delete_user(
    scope=project,
    user=user,
    role=ProjectRole.MEMBER,
    current_user=request.user
)
```

### Filtering by Permissions

#### Using `get_connected_customers` and `get_connected_projects`

These functions return all customers/projects where the user has any role:

```python
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
    get_connected_customers_by_permission,
    get_connected_projects_by_permission
)

# Get all connected customers
customers = get_connected_customers(user)

# Get customers where user is owner
owner_customers = get_connected_customers(user, RoleEnum.CUSTOMER_OWNER)

# Get projects where user can update
can_update_projects = get_connected_projects_by_permission(
    user,
    PermissionEnum.UPDATE_PROJECT
)
```

## Permission Categories

### Offering Permissions

| Permission | Description |
|------------|-------------|
| `OFFERING.CREATE` | Create new offerings |
| `OFFERING.UPDATE` | Update offering details |
| `OFFERING.DELETE` | Delete offerings |
| `OFFERING.PAUSE/UNPAUSE` | Control offering availability |
| `OFFERING.MANAGE_USER_GROUP` | Manage offering user groups |

### Resource Permissions

| Permission | Description |
|------------|-------------|
| `RESOURCE.TERMINATE` | Terminate resources |
| `RESOURCE.SET_USAGE` | Report resource usage |
| `RESOURCE.SET_LIMITS` | Update resource limits |
| `RESOURCE.SET_STATE` | Change resource state |

### Order Permissions

| Permission | Description |
|------------|-------------|
| `ORDER.LIST` | View orders |
| `ORDER.APPROVE` | Approve orders |
| `ORDER.REJECT` | Reject orders |
| `ORDER.CANCEL` | Cancel orders |

### Project/Customer Permissions

| Permission | Description |
|------------|-------------|
| `PROJECT.CREATE` | Create projects |
| `PROJECT.UPDATE` | Update project details |
| `PROJECT.DELETE` | Delete projects |
| `CUSTOMER.CREATE` | Create customers |
| `CUSTOMER.UPDATE` | Update customer details |

## Best Practices

### 1. Always Use PermissionEnum

Define permissions in `PermissionEnum` to ensure they're properly registered and available to the frontend:

```python
# Good
class PermissionEnum(str, Enum):
    MY_ACTION = 'RESOURCE.MY_ACTION'

# Bad - Won't be visible to frontend
MY_ACTION = 'RESOURCE.MY_ACTION'
```

### 2. Use Appropriate Scopes

Choose the right scope for permission checks:

```python
# For customer-level actions
has_permission(request, permission, customer)

# For project-level actions
has_permission(request, permission, project)

# For offering-specific actions
has_permission(request, permission, offering)
```

### 3. Implement Proper Permission Chains

When checking permissions on nested resources, traverse to the appropriate scope:

```python
# Check permission on resource's customer
has_permission(request, permission, resource.offering.customer)

# Check permission on order's project
has_permission(request, permission, order.project)
```

### 4. Use Filter Backends for List Views

For list endpoints, use `GenericRoleFilter` or implement custom filtering:

```python
class MyViewSet(viewsets.ModelViewSet):
    filter_backends = [GenericRoleFilter, DjangoFilterBackend]
```

### 5. Audit Role Changes

Role changes are automatically logged via signals (`role_granted`, `role_updated`, `role_revoked`), but always pass `current_user` for proper audit trails:

```python
add_user(scope, user, role, created_by=request.user)
delete_user(scope, user, role, current_user=request.user)
```

### 6. Performance and Accuracy Guidelines

#### Exact User Counting

When counting users across roles, always use exact calculations to avoid double-counting users with multiple roles:

```python
# Good: Exact counting with distinct()
user_count = (
    UserRole.objects.filter(is_active=True, scope=customer)
    .values("user_id")
    .distinct()
    .count()
)

# Bad: Approximation that double-counts users
customer_users = UserRole.objects.filter(content_type=customer_ct, object_id=customer.id).count()
project_users = UserRole.objects.filter(content_type=project_ct, object_id__in=project_ids).count()
total = customer_users + project_users  # This double-counts users with both roles
```

#### Query Optimization

Use Django ORM efficiently for permission-related queries:

```python
# Use select_related for foreign key relationships
roles = UserRole.objects.filter(user=user).select_related('content_type', 'role')

# Use prefetch_related for reverse relationships
customers = Customer.objects.prefetch_related('userrole_set__user')

# Filter at database level rather than in Python
active_roles = UserRole.objects.filter(is_active=True, expiration_time__gte=timezone.now())
```

#### Error Handling Best Practices

Handle edge cases gracefully in permission checking:

```python
def has_permission(request, permission, scope):
    # Handle None scope gracefully
    if scope is None:
        return False

    # ... rest of permission logic

def permission_factory(permission, sources=None):
    def permission_function(request, view, scope=None):
        if not scope:
            return

        if sources:
            attribute_errors = 0
            for path in sources:
                try:
                    source = scope
                    if path != "*":
                        for part in path.split("."):
                            source = getattr(source, part)
                    if has_permission(request, permission, source):
                        return
                except AttributeError:
                    attribute_errors += 1
                    continue

            # If all paths failed due to AttributeError, it's a configuration error
            if attribute_errors == len(sources):
                raise AttributeError(f"None of the attribute paths {sources} exist on the scope object")

        # If we reach here, permission was denied
        raise exceptions.PermissionDenied()
```

### 7. Time-based Role Best Practices

#### Default Parameter Behavior

The `has_user` function has specific behavior for the `expiration_time` parameter:

```python
# Check for any active role (default behavior)
has_user(scope, user, role)  # expiration_time=False by default

# Check for only permanent roles
has_user(scope, user, role, expiration_time=None)

# Check if role will be active at specific time
future_time = timezone.now() + timedelta(days=30)
has_user(scope, user, role, expiration_time=future_time)
```

#### API Design Consistency

When designing permission-related APIs:

- **Default parameters** should match the most common use case
- **Error types** should be consistent:
  - `AttributeError` for configuration/code errors (invalid attribute paths)
  - `PermissionDenied` for access control failures
  - `ValidationError` for user input errors

## Testing and Debugging Permissions

### Testing Permission Logic

When writing tests for permission functionality:

```python
class PermissionTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.user = self.fixture.owner

    def test_user_counting_accuracy(self):
        """Test that user counting handles overlapping roles correctly."""
        # Create user with multiple roles
        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.fixture.project.add_user(self.user, ProjectRole.ADMIN)

        # Count should be 1, not 2 (no double counting)
        user_count = count_users(self.customer)
        self.assertEqual(user_count, 1)

    def test_permission_factory_error_handling(self):
        """Test permission factory handles invalid paths correctly."""
        permission_func = permission_factory(
            PermissionEnum.UPDATE_OFFERING,
            ["nonexistent.attribute"]
        )

        # Should raise AttributeError for configuration errors
        with self.assertRaises(AttributeError):
            permission_func(mock_request, None, self.customer)
```

### Performance Testing

Monitor query counts for permission-related operations:

```python
@override_settings(DEBUG=True)
def test_permission_query_optimization(self):
    """Test that permission checks use reasonable number of queries."""
    from django.db import connection

    # Create test data
    users = [UserFactory() for _ in range(5)]
    for user in users:
        self.customer.add_user(user, CustomerRole.SUPPORT)

    connection.queries.clear()

    # Test permission check
    has_permission(users[0], PermissionEnum.LIST_ORDERS, self.customer)
    permission_queries = len(connection.queries)

    # Should use reasonable number of queries (not O(n) per user)
    self.assertLessEqual(permission_queries, 3)
```

### Debugging Permission Issues

When debugging permission problems:

1. **Check role assignments**:

   ```python
   # Verify user has expected roles
   roles = UserRole.objects.filter(user=user, is_active=True)
   print(f"User roles: {[(r.content_object, r.role.name) for r in roles]}")
   ```

2. **Verify permission assignments**:

   ```python
   # Check if role has required permissions
   role = CustomerRole.OWNER
   permissions = role.permissions.values_list('permission', flat=True)
   print(f"Role permissions: {list(permissions)}")
   ```

3. **Test permission paths**:

   ```python
   # Test attribute path resolution
   try:
       source = resource
       for part in "offering.customer".split("."):
           source = getattr(source, part)
       print(f"Path resolved to: {source}")
   except AttributeError as e:
       print(f"Path resolution failed: {e}")
   ```

4. **Enable verbose logging**:

   ```python
   import logging
   logging.getLogger('waldur_core.permissions').setLevel(logging.DEBUG)
   ```

### Common Issues and Solutions

#### Issue: User count approximations

**Problem**: Double-counting users with multiple roles

**Solution**: Always use `distinct()` on user_id when counting across multiple role assignments

#### Issue: Permission factory AttributeError

**Problem**: Invalid attribute paths in permission_factory sources

**Solution**: Verify object relationships and use try/catch for graceful error handling

#### Issue: Performance degradation in role filtering

**Problem**: N+1 queries when checking permissions for many objects

**Solution**: Use `select_related()` and `prefetch_related()` to optimize database queries

#### Issue: Time-based role confusion

**Problem**: Unclear behavior of `has_user` with different expiration_time parameters

**Solution**: Understand the three modes:

- `expiration_time=False` (default): Any active role
- `expiration_time=None`: Only permanent roles
- `expiration_time=datetime`: Roles active at specific time
