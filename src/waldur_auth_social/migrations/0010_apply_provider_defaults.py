from django.db import migrations

from waldur_auth_social.const import PROVIDER_DEFAULTS


def apply_provider_defaults(apps, schema_editor):
    IdentityProvider = apps.get_model("waldur_auth_social", "IdentityProvider")

    for provider_key, defaults in PROVIDER_DEFAULTS.items():
        try:
            provider_instance = IdentityProvider.objects.get(provider=provider_key)
        except IdentityProvider.DoesNotExist:
            continue

        provider_instance.user_field = defaults["user_field"]
        provider_instance.user_claim = defaults["user_claim"]
        provider_instance.attribute_mapping = defaults["attribute_mapping"]
        provider_instance.extra_fields = defaults["extra_fields"]
        provider_instance.save()


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_auth_social", "0009_identityprovider_attribute_mapping"),
    ]

    operations = [
        migrations.RunPython(
            apply_provider_defaults, reverse_code=migrations.RunPython.noop
        ),
    ]
