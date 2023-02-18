from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0036_offeringcomponent_backend_id'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.AddField(
            model_name='resource',
            name='backend_id',
            field=models.CharField(blank=True, max_length=255),
        ),
    ]
