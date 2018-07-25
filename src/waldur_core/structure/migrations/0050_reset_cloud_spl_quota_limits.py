# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.contrib.contenttypes import models as ct_models
from django.db import migrations, models

from waldur_core.quotas.models import Quota
from waldur_core.structure.models import CloudServiceProjectLink


def reset_cloud_spl_quota_limits(apps, schema_editor):
    old_limits = {
        'vcpu': 100,
        'ram': 256000,
        'storage': 5120000,
    }

    for model in CloudServiceProjectLink.get_all_models():
        content_type = ct_models.ContentType.objects.get_for_model(model)
        for quota, limit in old_limits.items():
            Quota.objects.filter(content_type=content_type, name=quota, limit=limit).update(limit=-1)


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0049_extend_abbreviation'),
    ]

    operations = [
        migrations.RunPython(reset_cloud_spl_quota_limits),
    ]
