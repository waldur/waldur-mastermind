from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_long_email'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='query_field',
            field=models.CharField(blank=True, max_length=300),
        ),
    ]
