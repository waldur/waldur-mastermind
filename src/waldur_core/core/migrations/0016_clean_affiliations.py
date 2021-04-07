from django.db import migrations


def clean_affiliations(apps, schema_editor):
    User = apps.get_model('core', 'User')
    User.objects.all().update(affiliations=[])


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0015_user_first_and_last_name'),
    ]

    operations = [
        migrations.RunPython(clean_affiliations),
    ]
