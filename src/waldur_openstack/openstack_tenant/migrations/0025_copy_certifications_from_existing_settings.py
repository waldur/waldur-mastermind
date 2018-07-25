# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.contrib.contenttypes.models import ContentType
from django.db import migrations


def copy_certifications_from_openstack_settings_to_openstack_tenant_settings(apps, schema_editor):
    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    Tenant = apps.get_model('openstack', 'Tenant')
    tenant_content_type = ContentType.objects.get_for_model(Tenant)
    openstack_tenant_settings = ServiceSettings.objects.filter(type='OpenStackTenant')

    for settings in openstack_tenant_settings.iterator():
        # skip all settings linked to different type than tenant content type.
        if settings.content_type_id != tenant_content_type.id:
            continue

        try:
            # GenericRelation is not available in migration, thus tenant has to be accessed directly through object_id
            tenant = Tenant.objects.get(pk=settings.object_id)
        except Tenant.DoesNotExist:
            continue
        else:
            admin_settings = tenant.service_project_link.service.settings
            settings.certifications.clear()
            settings.certifications.add(*admin_settings.certifications.all())


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0001_initial'),
        ('openstack_tenant', '0024_add_backup_size'),
    ]

    operations = [
        migrations.RunPython(copy_certifications_from_openstack_settings_to_openstack_tenant_settings),
    ]
