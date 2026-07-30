import django_fsm
from django.db import migrations


def strand_terminating_keys(apps, schema_editor):
    """Move any key left mid-revoke to Erred.

    Revoke is gone, so nothing will ever confirm these, and no transition accepts
    Terminating any more — they would hold a state the model no longer knows and spin
    in the portal forever. Erred is the truthful landing place: it says the key needs
    attention, and rotation accepts Erred, which re-mints the value whether or not the
    agent already removed it from the backend.
    """
    ResourceApiKey = apps.get_model("marketplace", "ResourceApiKey")
    ResourceApiKey.objects.filter(state="Terminating").update(
        state="Erred",
        error_message=(
            "Revocation was requested but never confirmed, and revoking keys is no "
            "longer supported. Rotate this key to replace its value."
        ),
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0255_null_stale_scope_content_types"),
    ]

    operations = [
        migrations.RunPython(
            strand_terminating_keys, migrations.RunPython.noop, elidable=True
        ),
        migrations.AlterField(
            model_name="resourceapikey",
            name="state",
            field=django_fsm.FSMField(
                choices=[
                    ("Creating", "Creating"),
                    ("OK", "OK"),
                    ("Updating", "Updating"),
                    ("Erred", "Erred"),
                ],
                default="Creating",
                max_length=50,
            ),
        ),
    ]
