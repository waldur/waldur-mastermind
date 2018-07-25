# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations
from django.db.models import Count


def remove_duplicates(apps, schema_editor):
    SecurityGroup = apps.get_model('openstack', 'SecurityGroup')
    OpenStackServiceProjectLink = apps.get_model('openstack', 'OpenStackServiceProjectLink')
    OK_STATE = 3

    for spl in OpenStackServiceProjectLink.objects.iterator():
        spl_security_groups = SecurityGroup.objects.filter(service_project_link=spl)
        for duplicated_backend_id in spl_security_groups.values_list('backend_id', flat=True).annotate(
                duplicates=Count('backend_id')).filter(duplicates__gt=1):

            duplicates_query = spl_security_groups.filter(backend_id=duplicated_backend_id)

            # try to leave only support groups in OK state
            if duplicates_query.filter(state=OK_STATE).count() > 0:
                security_group = duplicates_query.filter(state=OK_STATE).first()
            else:
                security_group = duplicates_query.first()

            duplicates_query.exclude(id=security_group.id).delete()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack', '0035_remove_ipmapping'),
    ]

    operations = [
        migrations.RunPython(remove_duplicates),
    ]
