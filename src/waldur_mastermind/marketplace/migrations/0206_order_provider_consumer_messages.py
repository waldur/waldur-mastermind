from django.db import migrations, models

import waldur_core.media.validators


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0205_backfill_initial_revisions"),
    ]

    operations = [
        migrations.AddField(
            model_name="order",
            name="provider_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="order",
            name="provider_message_url",
            field=models.URLField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="order",
            name="provider_message_attachment",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to="marketplace_order_provider_attachments",
                validators=[
                    waldur_core.media.validators.FileTypeValidator(
                        allowed_types=["application/pdf"]
                    )
                ],
            ),
        ),
        migrations.AddField(
            model_name="order",
            name="consumer_message",
            field=models.TextField(blank=True, default=""),
        ),
        migrations.AddField(
            model_name="order",
            name="consumer_message_attachment",
            field=models.FileField(
                blank=True,
                null=True,
                upload_to="marketplace_order_consumer_attachments",
                validators=[
                    waldur_core.media.validators.FileTypeValidator(
                        allowed_types=["application/pdf"]
                    )
                ],
            ),
        ),
    ]
