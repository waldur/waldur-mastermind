# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from uuid import uuid4

from django.db import migrations


def create_quotas(apps, schema_editor):
    Project = apps.get_model('structure', 'Project')
    Customer = apps.get_model('structure', 'Customer')
    ProjectGroup = apps.get_model('structure', 'ProjectGroup')
    Quota = apps.get_model('quotas', 'Quota')

    # We can not use model constants in migrations because they can be changed in future
    quota_name_map = {
        Project: 'nc_global_project_count',
        Customer: 'nc_global_customer_count',
        ProjectGroup: 'nc_global_project_group_count',
    }

    for model in [Project, Customer, ProjectGroup]:
        name = quota_name_map[model]
        usage = model.objects.count()
        if not Quota.objects.filter(name=name, object_id__isnull=True).exists():
            Quota.objects.create(uuid=uuid4().hex, name=name, usage=usage)
        else:
            Quota.objects.filter(name=name, object_id__isnull=True).update(usage=usage)


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0001_squashed_0021_balancehistory'),
        ('quotas', '0002_make_quota_scope_nullable'),
    ]

    operations = [
        migrations.RunPython(create_quotas),
    ]
