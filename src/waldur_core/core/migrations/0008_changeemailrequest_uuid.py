from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ('core', '0007_changeemailrequest'),
    ]

    operations = [
        migrations.AddField(
            model_name='changeemailrequest',
            name='uuid',
            field=models.UUIDField(null=True),
        ),
    ]
