from django.db import migrations


def drop_quota_revisions(apps, schema_editor):
    Version = apps.get_model('reversion', 'Version')
    ContentType = apps.get_model('contenttypes', 'ContentType')
    Quota = apps.get_model('quotas', 'Quota')
    ct = ContentType.objects.get_for_model(Quota)
    Version.objects.filter(content_type=ct).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('quotas', '0001_squashed_0004'),
        ('reversion', '__latest__'),
        ('contenttypes', '0001_initial'),
    ]

    operations = [
        migrations.RunPython(drop_quota_revisions),
    ]
