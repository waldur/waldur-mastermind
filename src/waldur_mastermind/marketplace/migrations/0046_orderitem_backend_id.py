from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0045_unescape_html_fields'),
    ]

    operations = [
        migrations.AddField(
            model_name='orderitem',
            name='backend_id',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
