from django.db import migrations


def drop_duplicates(apps, schema_editor):
    Role = apps.get_model('permissions', 'Role')
    RolePermission = apps.get_model('permissions', 'RolePermission')

    for role in Role.objects.all():
        permissions = set(
            RolePermission.objects.filter(role=role).values_list(
                'permission', flat=True
            )
        )
        role.permissions.all().delete()
        RolePermission.objects.bulk_create(
            [
                RolePermission(role=role, permission=permission)
                for permission in permissions
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ('permissions', '0008_customer_role'),
    ]

    operations = [migrations.RunPython(drop_duplicates)]
