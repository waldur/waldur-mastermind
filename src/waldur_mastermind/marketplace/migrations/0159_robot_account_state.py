# Generated manually

import django_fsm
from django.db import migrations, models


def set_initial_states(apps, schema_editor):
    RobotAccount = apps.get_model("marketplace", "RobotAccount")
    RobotAccount.objects.all().update(state=3)


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0158_remove_offeringuser_propagation_date"),
    ]

    operations = [
        migrations.AddField(
            model_name="robotaccount",
            name="state",
            field=django_fsm.FSMIntegerField(
                choices=[
                    (1, "Requested"),
                    (2, "Creating"),
                    (3, "OK"),
                    (4, "Requested deletion"),
                    (5, "Deleted"),
                    (6, "Error"),
                ],
                default=1,
            ),
        ),
        migrations.AddField(
            model_name="robotaccount",
            name="error_message",
            field=models.TextField(blank=True),
        ),
        migrations.AddField(
            model_name="robotaccount",
            name="error_traceback",
            field=models.TextField(blank=True),
        ),
        migrations.RunPython(
            set_initial_states,
        ),
    ]
