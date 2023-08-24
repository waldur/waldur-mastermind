from django.db import migrations

from waldur_core.permissions.enums import RoleEnum


def fill_system_roles(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')

    Role = apps.get_model('permissions', 'Role')
    UserRole = apps.get_model('permissions', 'UserRole')

    Customer = apps.get_model('structure', 'Customer')
    CustomerPermission = apps.get_model('structure', 'CustomerPermission')

    Project = apps.get_model('structure', 'Project')
    ProjectPermission = apps.get_model('structure', 'ProjectPermission')

    Offering = apps.get_model('marketplace', 'Offering')
    OfferingPermission = apps.get_model('marketplace', 'OfferingPermission')

    customer_owner = Role.objects.create(
        name=RoleEnum.CUSTOMER_OWNER,
        description='Organization owner',
    )
    customer_support = Role.objects.create(
        name=RoleEnum.CUSTOMER_SUPPORT,
        description='Organization support',
    )
    customer_manager = Role.objects.create(
        name=RoleEnum.CUSTOMER_MANAGER,
        description='Organization service manager',
    )
    project_admin = Role.objects.create(
        name=RoleEnum.PROJECT_ADMIN,
        description='Project administator',
    )
    project_manager = Role.objects.create(
        name=RoleEnum.PROJECT_MANAGER,
        description='Project manager',
    )
    project_member = Role.objects.create(
        name=RoleEnum.PROJECT_MEMBER,
        description='Project member',
    )
    offering_manager = Role.objects.create(
        name=RoleEnum.OFFERING_MANAGER,
        description='Offering manager',
    )

    def create_user_role(permission, **kwargs):
        UserRole.objects.create(
            user=permission.user,
            created_by=permission.created_by,
            created=permission.created,
            expiration_time=permission.expiration_time,
            is_active=permission.is_active,
            **kwargs
        )

    customer_ct = ContentType.objects.get_for_model(Customer)
    for permission in CustomerPermission.objects.all():
        role = None
        if permission.role == 'owner':
            role = customer_owner
        elif permission.role == 'support':
            role = customer_support
        elif permission.role == 'service_manager':
            role = customer_manager
        create_user_role(
            permission,
            content_type=customer_ct,
            object_id=permission.customer_id,
            role=role,
        )

    project_ct = ContentType.objects.get_for_model(Project)
    for permission in ProjectPermission.objects.all():
        role = None
        if permission.role == 'admin':
            role = project_admin
        elif permission.role == 'manager':
            role = project_manager
        elif permission.role == 'member':
            role = project_member
        create_user_role(
            permission,
            content_type=project_ct,
            object_id=permission.project_id,
            role=role,
        )

    offering_ct = ContentType.objects.get_for_model(Offering)
    for permission in OfferingPermission.objects.all():
        create_user_role(
            permission,
            content_type=offering_ct,
            object_id=permission.offering_id,
            role=offering_manager,
        )


class Migration(migrations.Migration):
    dependencies = [
        ('permissions', '0001_initial'),
        ('structure', '0001_squashed_0036'),
        ('marketplace', '0001_squashed_0076'),
    ]

    operations = [migrations.RunPython(fill_system_roles)]
