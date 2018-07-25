# -*- coding: utf-8 -*-
from __future__ import unicode_literals

import logging

from django.db import models, migrations
import waldur_core.structure.models

logger = logging.getLogger(__name__)


def create_service_type(apps, schema_editor):
    service_types = {
        1: 'OpenStack',
        2: 'DigitalOcean',
        3: 'Amazon',
        4: 'Jira',
        5: 'GitLab',
        6: 'Oracle',
        7: 'Azure',
        8: 'SugarCRM',
        9: 'SaltStack',
        10: 'Zabbix'
    }

    ServiceSettings = apps.get_model('structure', 'ServiceSettings')
    for service in ServiceSettings.objects.all():
        try:
            service.service_type = service_types[service.type]
            service.save()
        except KeyError:
            if service.type in service_types.values():
                service.service_type = service.type
                service.save()
            else:
                logger.warning('Cannot migrate service type %s' % service.type)


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0027_servicesettings_service_type'),
    ]

    operations = [
        migrations.RunPython(create_service_type),
        migrations.RemoveField(
            model_name='servicesettings',
            name='type',
        ),
        migrations.RenameField(
            model_name='servicesettings',
            old_name='service_type',
            new_name='type'
        ),
        migrations.AlterField(
            model_name='servicesettings',
            name='type',
            field=models.CharField(max_length=255, db_index=True, validators=[waldur_core.structure.models.validate_service_type]),
            preserve_default=True,
        )
    ]
