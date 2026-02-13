from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0207_offering_backend_id_rules"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="consumer_rejection_comment",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="order",
            name="provider_rejection_comment",
            field=models.TextField(blank=True, default=""),
        ),
    ]
