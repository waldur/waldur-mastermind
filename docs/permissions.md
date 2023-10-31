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

Here we use `permission_factory` function which accepts permission string and list of paths to scopes, either customer, project or offering. It returns function which accepts requst and raises an exception if user doesn't have specified permission in roles connected to current user and one of these scopes.

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

Altough this approach works fine for trivial use cases, often enough permission filtering logic is more involved and we implement `get_queryset` method instead.

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
