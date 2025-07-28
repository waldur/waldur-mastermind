from django.db import migrations

from waldur_auth_social.const import ProviderChoices


def enable_notifications_for_eduteams_users(apps, schema_editor):
    User = apps.get_model("core", "User")

    User.objects.filter(
        registration_method=ProviderChoices.EDUTEAMS,
        is_active=True,
        notifications_enabled=False,
    ).update(notifications_enabled=True)


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_auth_social", "0012_update_eduteams_user_claim_and_email"),
    ]

    operations = [
        migrations.RunPython(
            enable_notifications_for_eduteams_users,
        ),
    ]
