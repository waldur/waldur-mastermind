# REST permissions

## Permissions for viewing

Implemented through the usage of permission classes and filters that are applied to the viewset's queryset.

```python
  class MyModelViewSet(
    # ...
    filter_backends = (filters.GenericRoleFilter,)
    permission_classes = (rf_permissions.IsAuthenticated,
                          rf_permissions.DjangoObjectPermissions)
```

## Permissions for creation/deletion/update

CRU permissions should be implemented using ActionsViewSet.
It allows you to define validators for detail actions and define permissions checks
for all actions or each action separately. Please check ActionPermissionsBackend for more details.
