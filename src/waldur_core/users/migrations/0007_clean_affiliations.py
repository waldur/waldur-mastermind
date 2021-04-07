from django.db import migrations


def clean_affiliations(apps, schema_editor):
    User = apps.get_model('users', 'Invitation')
    User.objects.all().update(affiliations=[])


class Migration(migrations.Migration):

    dependencies = [
        ('users', '0006_invitation_affiliations'),
    ]

    operations = [
        migrations.RunPython(clean_affiliations),
    ]
