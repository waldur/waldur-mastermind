import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0016_userrole_modified_alter_userrole_created"),
        ("waldur_autoprovisioning", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="rule",
            name="project_role",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                to="permissions.role",
            ),
        ),
    ]
