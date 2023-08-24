from django.contrib.contenttypes.models import ContentType

from waldur_core.permissions.enums import RoleEnum
from waldur_core.permissions.models import Role, UserRole
from waldur_core.structure.models import Customer, CustomerRole, Project, ProjectRole
from waldur_mastermind.marketplace.models import Offering

# This is temporary code. It it intended for transitional phase only.


def sync_permission_when_role_is_granted(sender, structure, user, role, **kwargs):
    if isinstance(structure, Customer):
        content_type = ContentType.objects.get_for_model(Customer)

        if role == CustomerRole.OWNER:
            new_role, _ = Role.objects.get_or_create(name=RoleEnum.CUSTOMER_OWNER)
            UserRole.objects.create(
                user=user,
                content_type=content_type,
                object_id=structure.id,
                role=new_role,
            )
        elif role == CustomerRole.SERVICE_MANAGER:
            new_role, _ = Role.objects.get_or_create(name=RoleEnum.CUSTOMER_MANAGER)
            UserRole.objects.create(
                user=user,
                content_type=content_type,
                object_id=structure.id,
                role=new_role,
            )
        elif role == CustomerRole.SUPPORT:
            new_role, _ = Role.objects.get_or_create(name=RoleEnum.CUSTOMER_SUPPORT)
            UserRole.objects.create(
                user=user,
                content_type=content_type,
                object_id=structure.id,
                role=new_role,
            )
    elif isinstance(structure, Project):
        content_type = ContentType.objects.get_for_model(Project)

        if role == ProjectRole.ADMINISTRATOR:
            new_role, _ = Role.objects.get_or_create(name=RoleEnum.PROJECT_ADMIN)
            UserRole.objects.create(
                user=user,
                content_type=content_type,
                object_id=structure.id,
                role=new_role,
            )
        elif role == ProjectRole.MANAGER:
            new_role, _ = Role.objects.get_or_create(name=RoleEnum.PROJECT_MANAGER)
            UserRole.objects.create(
                user=user,
                content_type=content_type,
                object_id=structure.id,
                role=new_role,
            )
        elif role == ProjectRole.MEMBER:
            new_role, _ = Role.objects.get_or_create(name=RoleEnum.PROJECT_MEMBER)
            UserRole.objects.create(
                user=user,
                content_type=content_type,
                object_id=structure.id,
                role=new_role,
            )


def sync_permission_when_role_is_revoked(sender, structure, user, role, **kwargs):
    pass


def sync_offering_permission_creation(sender, instance, created=False, **kwargs):
    if not created:
        return
    content_type = ContentType.objects.get_for_model(Offering)
    new_role, _ = Role.objects.get_or_create(name=RoleEnum.OFFERING_MANAGER)
    UserRole.objects.create(
        user=instance.user,
        content_type=content_type,
        object_id=instance.offering_id,
        role=new_role,
    )


def sync_offering_permission_deletion(sender, instance, **kwargs):
    pass
