# Waldur Permission System Guide

## Permission Factory Usage

**ALWAYS use `permission_factory` instead of manual `has_permission` checks in ViewSets.**

### For ViewSet Actions

```python
# Define permissions as class attributes
compliance_overview_permissions = [
    permission_factory(PermissionEnum.UPDATE_CALL)
]

@action(detail=True, methods=["get"])
def compliance_overview(self, request, uuid=None):
    # No manual permission check needed - handled by permission_factory
    pass
```

### Permission Factory Patterns

- **Current Object**: `permission_factory(PermissionEnum.PERMISSION_NAME)` - no path needed
- **Related Object**: `permission_factory(PermissionEnum.PERMISSION_NAME, ["customer"])` - for related objects
- **Nested Path**: `permission_factory(PermissionEnum.PERMISSION_NAME, ["project.customer"])` - for nested relationships

### For perform_create/perform_destroy Methods

```python
# Use declarative permission attributes instead of manual perform_* overrides
def check_create_permissions(request, view, obj=None):
    """Check permissions for creating reviews."""
    serializer = view.get_serializer(data=request.data)
    serializer.is_valid(raise_exception=True)
    proposal = serializer.validated_data["proposal"]

    if not has_permission(
        request.user,
        PermissionEnum.MANAGE_PROPOSAL_REVIEW,
        proposal.round.call,
    ):
        raise exceptions.PermissionDenied()

def check_destroy_permissions(request, view, obj=None):
    """Check permissions for destroying reviews."""
    if obj and not has_permission(
        request.user,
        PermissionEnum.MANAGE_PROPOSAL_REVIEW,
        obj.proposal.round.call,
    ):
        raise exceptions.PermissionDenied()

create_permissions = [check_create_permissions]
destroy_permissions = [check_destroy_permissions]
```

### When to Use Manual Checks

- Complex permission logic that doesn't map to standard object relationships
- Custom validation that requires dynamic permission targets
- Legacy code not yet refactored to declarative patterns

## Adding New Permissions

### 1. Define the Permission Enum

Add the new permission to `PermissionEnum` in `src/waldur_core/permissions/enums.py`:

```python
class PermissionEnum(StrEnum):
    MY_NEW_PERMISSION = "RESOURCE.MY_ACTION"
```

If the permission is for managing team members (creating/updating/deleting roles) on a scope type, also add it to the `CREATE_PERMISSIONS`, `UPDATE_PERMISSIONS`, and `DELETE_PERMISSIONS` dicts in the same file.

### 2. Assign to Roles via permissions.yaml

**Do NOT use data migrations to assign permissions to roles.** Instead, add the permission to the appropriate roles in `docker/rootfs/etc/waldur/permissions.yaml`:

```yaml
- role: CUSTOMER.OWNER
  scope: customer
  permissions:
    - RESOURCE.MY_ACTION   # Add here
```

This file is loaded by the `import_roles` management command, which runs on deployment. The command creates roles and syncs their permissions from the YAML definition.

### 3. Use in ViewSets

```python
my_action_permissions = [
    permission_factory(PermissionEnum.MY_NEW_PERMISSION, ["project.customer"])
]
```

## Permission System Behavior

### Expiration Handling

- Basic permission queries (`get_users_with_permission`, `get_scope_ids`) include all roles regardless of expiration
- Expiration checking is explicit via `has_user(expiration_time=False)`, not implicit in `has_permission()`
- Use `has_user(expiration_time=current_time)` for time-based validation

### Error Handling

- `permission_factory` doesn't catch `AttributeError` and convert to `PermissionDenied`
- Test for actual exceptions the system raises, not ideal ones
- Handle `AttributeError` when accessing missing nested attributes

## Data Accuracy Critical Areas

- **User counting**: Always use `distinct()` on user_id to avoid double-counting users with multiple roles
- **Permission checks**: Handle edge cases (None scope, missing attributes) gracefully
- **Financial calculations**: Never approximate - exact calculations required

## Performance Optimization

### Query Optimization Strategy

- Use `select_related()` for foreign keys
- Use `prefetch_related()` for reverse relationships
- Use `distinct()` for deduplication instead of manual logic
- Accept 20-30 queries for complex operations rather than approximations
- Verify permission checks use reasonable query counts (≤3 for most operations)

## Personal Access Tokens — entity scoping

`PersonalAccessToken` has two scope layers:

1. `scopes` — the permission allowlist (subset of `PermissionEnum`). A PAT
   can only ever exercise permissions that the user holds *and* that are
   listed here.
2. `allowed_scopes` — optional list of entity bindings restricting *where*
   the PAT can act. Stored as `[{content_type_id, object_id}, …]`. Created
   from `[{type, uuid}, …]` where `type` is a key of
   `permissions.enums.TYPE_MAP` (e.g. `customer`, `project`, `offering`,
   `resource`, `resource_project`, `call`, `proposal`, `service_provider`,
   `call_organizer`).

### Enforcement

`_pat_scope_check` (and the `_pat_entity_check` helper in
`waldur_core.permissions.utils`) runs ahead of the `is_staff` bypass so a
scoped PAT narrows even staff users. The rules:

- Empty `allowed_scopes` → no entity restriction (legacy behaviour).
- `scope=None` request + non-empty bindings → denied. A scoped PAT cannot
  perform scope-less / global actions.
- Otherwise → allowed iff the request scope, or any of its ancestors per
  `get_scope_ancestors`, matches one of the PAT's bindings. The walk is
  upward-only: a PAT bound to a child entity does **not** authorise actions
  on its parent.

### Restrictions

- `STAFF.ACCESS` / `SUPPORT.ACCESS` cannot be combined with `allowed_scopes` —
  those scopes are global by design.
- Non-staff users may only bind a PAT to entities where they hold at least
  one of the requested permissions (directly or via an ancestor) — this
  guard prevents privilege escalation through binding.
- Bindings are immutable. `rotate` preserves them; there is no PATCH
  endpoint. To change bindings, create a new PAT.

### Known limitation (will land in a follow-up MR)

List endpoints (e.g. `GET /api/customers/`, `GET /api/orders/`) are **not**
yet filtered by `allowed_scopes`. A scoped PAT still sees the same list
results as the underlying user; only write/detail/action permission checks
are restricted. A global filter backend that intersects each list queryset
with the PAT's bindings is planned as a separate change.
