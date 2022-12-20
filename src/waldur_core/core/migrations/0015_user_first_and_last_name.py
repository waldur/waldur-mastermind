from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0014_user_affiliations'),
    ]

    operations = [
        migrations.AddField(
            model_name='user',
            name='first_name',
            field=models.CharField(
                blank=True, max_length=100, verbose_name='first name'
            ),
        ),
        migrations.AddField(
            model_name='user',
            name='last_name',
            field=models.CharField(
                blank=True, max_length=100, verbose_name='last name'
            ),
        ),
        migrations.RemoveField(
            model_name='user',
            name='full_name',
        ),
    ]
