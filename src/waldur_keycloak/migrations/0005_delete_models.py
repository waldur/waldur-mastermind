from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("waldur_keycloak", "0004_alter_offeringkeycloakgroup_role"),
    ]

    operations = [
        migrations.DeleteModel(
            name="OfferingKeycloakMembership",
        ),
        migrations.DeleteModel(
            name="OfferingKeycloakGroup",
        ),
    ]
