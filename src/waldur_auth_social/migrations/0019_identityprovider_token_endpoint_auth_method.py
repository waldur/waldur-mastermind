from django.db import migrations, models


def use_basic_auth_for_tara(apps, schema_editor):
    # TARA was hard-coded to client_secret_basic before this field existed.
    IdentityProvider = apps.get_model("waldur_auth_social", "IdentityProvider")
    IdentityProvider.objects.filter(provider="tara").update(
        token_endpoint_auth_method="client_secret_basic"
    )


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_auth_social", "0018_encrypt_oauth_tokens"),
    ]

    operations = [
        migrations.AddField(
            model_name="identityprovider",
            name="token_endpoint_auth_method",
            field=models.CharField(
                choices=[
                    ("client_secret_post", "Client secret in request body"),
                    (
                        "client_secret_basic",
                        "Client secret in HTTP Basic auth header",
                    ),
                ],
                default="client_secret_post",
                help_text="How client credentials are sent to the token endpoint: "
                "in the request body (client_secret_post) or "
                "as an HTTP Basic Authorization header (client_secret_basic).",
                max_length=32,
            ),
        ),
        migrations.RunPython(use_basic_auth_for_tara, migrations.RunPython.noop),
    ]
