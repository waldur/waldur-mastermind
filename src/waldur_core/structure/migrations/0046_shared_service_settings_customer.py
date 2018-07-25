# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


def drop_customers_for_shared_service_settings(apps, schema_editor):
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    for settings in ServiceSettings.objects.all():
        if settings.shared and settings.customer is not None:
            settings.customer = None
            settings.save()


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0045_project_services_certifications'),
    ]

    operations = [
        migrations.RunPython(drop_customers_for_shared_service_settings),
    ]
