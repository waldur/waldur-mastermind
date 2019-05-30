# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations

from waldur_mastermind.marketplace_openstack import STORAGE_TYPE, RAM_TYPE, CORES_TYPE, PACKAGE_TYPE


def create_category_components(apps, schema_editor):
    CATEGORY_TITLE = 'Private clouds'

    Category = apps.get_model('marketplace', 'Category')
    CategoryComponent = apps.get_model('marketplace', 'CategoryComponent')
    OfferingComponent = apps.get_model('marketplace', 'OfferingComponent')

    try:
        vpc_category = Category.objects.get(title=CATEGORY_TITLE)
    except Category.DoesNotExist:
        return

    storage_gb_cc, _ = CategoryComponent.objects.get_or_create(
        category=vpc_category,
        type=STORAGE_TYPE,
        name='Storage',
        measured_unit='GB'
    )

    ram_gb_cc, _ = CategoryComponent.objects.get_or_create(
        category=vpc_category,
        type=RAM_TYPE,
        name='RAM',
        measured_unit='GB'
    )

    cores_cc, _ = CategoryComponent.objects.get_or_create(
        category=vpc_category,
        type=CORES_TYPE,
        name='Cores',
        measured_unit='cores'
    )

    components = OfferingComponent.objects.filter(offering__type=PACKAGE_TYPE, parent=None)

    components.filter(type=STORAGE_TYPE).update(parent=storage_gb_cc)
    components.filter(type=RAM_TYPE).update(parent=ram_gb_cc)
    components.filter(type=CORES_TYPE).update(parent=cores_cc)


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0082_orderitem_activated'),
    ]

    operations = [
        migrations.RunPython(create_category_components),
    ]
