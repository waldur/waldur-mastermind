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
