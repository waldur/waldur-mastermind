# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations
from django.db.models import Count


def find_duplicates(model):
    return model.objects\
                .values('settings_id', 'backend_id')\
                .annotate(Count('pk'))\
                .filter(pk__count__gt=1)


def get_duplicate_set(model, item):
    affected = model.objects.filter(
        settings_id=item['settings_id'],
        backend_id=item['backend_id']
    )
    chosen = affected.first()
    duplicates = affected.exclude(pk=chosen.pk)
    return chosen, duplicates


def drop_duplicate_security_groups(apps, schema_editor):
    SecurityGroup = apps.get_model('openstack_tenant', 'SecurityGroup')

    for item in find_duplicates(SecurityGroup):
        securitygroup, duplicates = get_duplicate_set(SecurityGroup, item)
        SecurityGroup.instances.through.objects\
            .filter(securitygroup__in=duplicates)\
            .update(securitygroup=securitygroup)
        duplicates.delete()


def drop_duplicate_networks(apps, schema_editor):
    Network = apps.get_model('openstack_tenant', 'Network')
    SubNet = apps.get_model('openstack_tenant', 'SubNet')

    for item in find_duplicates(Network):
        network, duplicates = get_duplicate_set(Network, item)
        SubNet.objects.filter(network__in=duplicates).update(network=network)
        duplicates.delete()


def drop_duplicate_subnets(apps, schema_editor):
    SubNet = apps.get_model('openstack_tenant', 'SubNet')
    InternalIP = apps.get_model('openstack_tenant', 'InternalIP')

    for item in find_duplicates(SubNet):
        subnet, duplicates = get_duplicate_set(SubNet, item)
        InternalIP.objects.filter(subnet__in=duplicates).update(subnet=subnet)
        duplicates.delete()


class Migration(migrations.Migration):

    dependencies = [
        ('openstack_tenant', '0027_remove_duplicate_floating_ips'),
    ]

    operations = [
        migrations.RunPython(drop_duplicate_security_groups),
        migrations.RunPython(drop_duplicate_networks),
        migrations.RunPython(drop_duplicate_subnets),
    ]
