# Generated by Django 1.11.18 on 2019-03-12 14:53
from django.db import migrations
from django.db.models import F

PACKAGE_TYPE = 'Packages.Template'
RAM_TYPE = 'ram'
CORES_TYPE = 'cores'
STORAGE_TYPE = 'storage'


def migrate_components(apps, schema_editor):
    PlanComponent = apps.get_model('marketplace', 'PlanComponent')
    queryset = PlanComponent.objects.filter(
        component__offering__type=PACKAGE_TYPE,
        component__type__in=(RAM_TYPE, STORAGE_TYPE),
    )
    # In order to avoid numeric overflow we need to ensure that there are no invalid values.
    # A field with precision 14, scale 10 must round to an absolute value less than 10^4.
    queryset.filter(price__gt=9).update(price=0)
    queryset.update(amount=F('amount') / 1024, price=F('price') * 1024)


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0001_squashed_0093'),
    ]

    operations = [migrations.RunPython(migrate_components)]
