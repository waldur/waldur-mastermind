# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations


# Authentication method now uses API v3.
def update_service_settings_backend_url(apps, schema_editor):
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    for service_settings in ServiceSettings.objects.filter(type__in=('OpenStack', 'OpenStackTenant')):
        if service_settings.backend_url.endswith('v2.0'):
            service_settings.backend_url = service_settings.backend_url[:-len('v2.0')] + 'v3'
            service_settings.save()


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0041_servicesettings_domain'),
        ('openstack', '0031_tenant_backup_storage'),
    ]

    operations = [
        migrations.RunPython(update_service_settings_backend_url),
    ]
