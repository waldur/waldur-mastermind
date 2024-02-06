import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("permissions", "0013_alter_rolepermission_unique_together"),
        ("users", "0005_fill_data"),
    ]

    operations = [
        migrations.AlterField(
            model_name="groupinvitation",
            name="role",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="permissions.role"
            ),
        ),
        migrations.AlterField(
            model_name="invitation",
            name="role",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="permissions.role"
            ),
        ),
        migrations.AlterField(
            model_name="groupinvitation",
            name="customer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="structure.customer"
            ),
        ),
        migrations.AlterField(
            model_name="invitation",
            name="customer",
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.CASCADE, to="structure.customer"
            ),
        ),
        migrations.RemoveField(
            model_name="groupinvitation",
            name="customer_role",
        ),
        migrations.RemoveField(
            model_name="groupinvitation",
            name="project",
        ),
        migrations.RemoveField(
            model_name="groupinvitation",
            name="project_role",
        ),
        migrations.RemoveField(
            model_name="invitation",
            name="customer_role",
        ),
        migrations.RemoveField(
            model_name="invitation",
            name="project",
        ),
        migrations.RemoveField(
            model_name="invitation",
            name="project_role",
        ),
    ]
