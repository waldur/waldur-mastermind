from django.db import migrations


def update_eduteams_user_claim_and_email(apps, schema_editor):
    IdentityProvider = apps.get_model("waldur_auth_social", "IdentityProvider")
    for provider in IdentityProvider.objects.filter(provider="eduteams"):
        changed = False
        # Check and update user_claim
        if provider.user_claim != "sub":
            print(
                f"[MIGRATION] IdentityProvider (id={provider.id}, label={provider.label}): user_claim changed from '{provider.user_claim}' to 'sub'"
            )
            provider.user_claim = "sub"
            changed = True
        # Check and update attribute_mapping['email']
        mapping = provider.attribute_mapping or {}
        old_email = mapping.get("email")
        if old_email != "email":
            print(
                f"[MIGRATION] IdentityProvider (id={provider.id}, label={provider.label}): attribute_mapping['email'] changed from '{old_email}' to 'email'"
            )
            mapping["email"] = "email"
            provider.attribute_mapping = mapping
            changed = True
        if changed:
            provider.save()


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_auth_social", "0011_fill_extra_scope"),
    ]

    operations = [
        migrations.RunPython(update_eduteams_user_claim_and_email),
    ]
