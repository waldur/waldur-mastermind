# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
from decimal import Decimal
import django.core.validators


def migrate_package_component_price(apps, schema_editor):
    PackageComponent = apps.get_model('packages', 'PackageComponent')

    for package_component in PackageComponent.objects.all():
        package_component.price *= 24
        package_component.save()


class Migration(migrations.Migration):

    dependencies = [
        ('packages', '0006_trial_packagetemplate'),
    ]

    operations = [
        migrations.AlterField(
            model_name='packagecomponent',
            name='price',
            field=models.DecimalField(default=0, verbose_name='Price per unit per day', max_digits=14, decimal_places=10, validators=[django.core.validators.MinValueValidator(Decimal('0'))]),
        ),
        migrations.RunPython(migrate_package_component_price),
    ]
