from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('notifications', '0004_messagetemplate'),
    ]

    operations = [
        migrations.AddField(
            model_name='broadcastmessage',
            name='state',
            field=models.CharField(
                choices=[
                    ('DRAFT', 'Draft'),
                    ('SCHEDULED', 'Scheduled'),
                    ('SENT', 'Sent'),
                ],
                default='DRAFT',
                max_length=30,
            ),
        ),
        migrations.AddField(
            model_name='broadcastmessage',
            name='send_at',
            field=models.DateTimeField(null=True),
        ),
    ]
