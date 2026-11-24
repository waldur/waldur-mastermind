from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0329_offeringcomponent_limit_increase_only"),
    ]

    operations = [
        migrations.AddField(
            model_name="resourceapikey",
            name="rotation_due",
            field=models.BooleanField(
                default=False,
                help_text="The key's assignee's account was deleted while the key could not be rotated: it was paused, its last command had failed, or one was in flight. Waldur rotates it once it settles, so the departed person's copy stops working. A rotation that settles clears it.",
            ),
        ),
    ]
