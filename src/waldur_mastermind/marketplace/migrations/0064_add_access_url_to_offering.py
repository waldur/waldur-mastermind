from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0063_resource_effective_id'),
    ]

    operations = [
        migrations.AddField(
            model_name='offering',
            name='access_url',
            field=models.URLField(
                blank=True, help_text='URL for accessing management console.'
            ),
        ),
    ]
