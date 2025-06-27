from django.db import migrations

from waldur_auth_social.const import ProviderChoices


def fill_eduteams_extra_scope(apps, schema_editor):
    IdentityProvider = apps.get_model("waldur_auth_social", "IdentityProvider")

    try:
        provider_instance = IdentityProvider.objects.get(
            provider=ProviderChoices.EDUTEAMS
        )
    except IdentityProvider.DoesNotExist:
        return
    provider_instance.extra_scope = "profile email eduperson_assurance ssh_public_key"
    provider_instance.save()


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_auth_social", "0010_apply_provider_defaults"),
    ]

    operations = [
        migrations.RenameField(
            model_name="identityprovider", old_name="scope", new_name="extra_scope"
        ),
        migrations.RunPython(fill_eduteams_extra_scope),
    ]
