from django.contrib.auth.hashers import make_password
from django.db import migrations


def create_robot_user(apps, schema_editor):
    User = apps.get_model('core', 'User')

    User.objects.create(
        first_name='System',
        last_name='Robot',
        username='system_robot',
        description='Special user used for performing actions on behalf of Waldur.',
        is_staff=True,
        is_active=True,
        password=make_password(None),
    )


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0022_project_end_date'),
    ]

    operations = [
        migrations.RunPython(create_robot_user),
    ]
