# Role-based access control

## Introduction

Waldur authorization system determines what user can do. It consists of permissions and roles. Permission is unique string designating action to be executed. Role is named set of permissions. This functionality is implemented in `waldur_core.permissions` application.

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
