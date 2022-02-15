from django.conf import settings
from django.db import migrations


def delete_duplicate_records(apps, schema_editor):
    OfferingUser = apps.get_model('marketplace', 'OfferingUser')
    unique_pairs = set()
    for record in OfferingUser.objects.all():
        key = (record.offering_id, record.user_id)
        if key in unique_pairs:
            record.delete()
        else:
            unique_pairs.add(key)


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('marketplace', '0059_offering_image'),
    ]

    operations = [
        migrations.RunPython(delete_duplicate_records),
        migrations.AlterUniqueTogether(
            name='offeringuser',
            unique_together={('offering', 'user')},
        ),
    ]
