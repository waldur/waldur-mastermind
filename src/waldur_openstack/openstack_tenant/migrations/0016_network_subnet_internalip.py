# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.core.fields
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0041_servicesettings_domain'),
        ('openstack_tenant', '0015_snapshotrestoration'),
    ]

    operations = [
        migrations.CreateModel(
            name='InternalIP',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('mac_address', models.CharField(max_length=32, blank=True)),
                ('ip4_address', models.GenericIPAddressField(null=True, protocol=b'IPv4', blank=True)),
                ('ip6_address', models.GenericIPAddressField(null=True, protocol=b'IPv6', blank=True)),
                ('instance', models.ForeignKey(related_name='internal_ips_set', to='openstack_tenant.Instance')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Network',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, db_index=True)),
                ('is_external', models.BooleanField(default=False)),
                ('type', models.CharField(max_length=50, blank=True)),
                ('segmentation_id', models.IntegerField(null=True)),
                ('settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SubNet',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, db_index=True)),
                ('cidr', models.CharField(max_length=32, blank=True)),
                ('gateway_ip', models.GenericIPAddressField(null=True, protocol='IPv4')),
                ('allocation_pools', waldur_core.core.fields.JSONField(default={})),
                ('ip_version', models.SmallIntegerField(default=4)),
                ('enable_dhcp', models.BooleanField(default=True)),
                ('dns_nameservers', waldur_core.core.fields.JSONField(default=[], help_text='List of DNS name servers associated with the subnet.')),
                ('network', models.ForeignKey(related_name='subnets', to='openstack_tenant.Network')),
                ('settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
                'verbose_name': 'Subnet',
                'verbose_name_plural': 'Subnets',
            },
        ),
        migrations.AddField(
            model_name='internalip',
            name='subnet',
            field=models.ForeignKey(related_name='internal_ips', to='openstack_tenant.SubNet'),
        ),
    ]
