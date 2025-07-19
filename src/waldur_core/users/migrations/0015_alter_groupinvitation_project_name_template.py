# Generated manually to allow NULL values for project_name_template

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("users", "0014_alter_invitation_extra_invitation_text"),
    ]

    operations = [
        migrations.AlterField(
            model_name="groupinvitation",
            name="project_name_template",
            field=models.CharField(
                blank=True,
                null=True,
                help_text="Template for project name. Supports {username}, {email}, {full_name} variables",
                max_length=255,
            ),
        ),
    ]
