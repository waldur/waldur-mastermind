import django.db.models.deletion
from django.db import migrations, models

from waldur_core.permissions.enums import RoleEnum


def fill_system_roles(apps, schema_editor):
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Role = apps.get_model('permissions', 'Role')
    Customer = apps.get_model('structure', 'Customer')
    Project = apps.get_model('structure', 'Project')
    Offering = apps.get_model('marketplace', 'Offering')

    customer_ct = ContentType.objects.get_for_model(Customer)
    project_ct = ContentType.objects.get_for_model(Project)
    offering_ct = ContentType.objects.get_for_model(Offering)

    Role.objects.filter(name=RoleEnum.CUSTOMER_OWNER).update(content_type=customer_ct)
    Role.objects.filter(name=RoleEnum.CUSTOMER_SUPPORT).update(content_type=customer_ct)
    Role.objects.filter(name=RoleEnum.CUSTOMER_MANAGER).update(content_type=customer_ct)
    Role.objects.filter(name=RoleEnum.PROJECT_ADMIN).update(content_type=project_ct)
    Role.objects.filter(name=RoleEnum.PROJECT_MANAGER).update(content_type=project_ct)
    Role.objects.filter(name=RoleEnum.PROJECT_MEMBER).update(content_type=project_ct)
    Role.objects.filter(name=RoleEnum.OFFERING_MANAGER).update(content_type=offering_ct)


class Migration(migrations.Migration):
    dependencies = [
        ('contenttypes', '0002_remove_content_type_name'),
        ('permissions', '0005_alter_role_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='role',
            name='content_type',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='contenttypes.contenttype',
            ),
        ),
        migrations.RunPython(fill_system_roles),
        migrations.AlterField(
            model_name='role',
            name='content_type',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE,
                related_name='+',
                to='contenttypes.contenttype',
            ),
        ),
    ]
