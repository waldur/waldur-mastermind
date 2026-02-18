from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_keycloak", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="offeringkeycloakgroup",
            name="scope_id",
            field=models.CharField(
                blank=True,
                default="",
                help_text="Sub-entity identifier within a resource, e.g. Rancher project ID within a cluster.",
                max_length=255,
            ),
        ),
    ]
