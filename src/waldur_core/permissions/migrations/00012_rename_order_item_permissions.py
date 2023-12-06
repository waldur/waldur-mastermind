from django.db import migrations


def rename_order_item_permissions(apps, schema_editor):
    RolePermission = apps.get_model('permissions', 'RolePermission')

    for role in RolePermission.objects.filter(permission__contains='ORDER_ITEM'):
        role.permission = role.permission.replace('ORDER_ITEM', 'ORDER').replace(
            'ORDER.TERMINATE', 'ORDER.CANCEL'
        )
        role.save()


class Migration(migrations.Migration):
    dependencies = [
        ('permissions', '0011_role_description_cs'),
    ]

    operations = [migrations.RunPython(rename_order_item_permissions)]
