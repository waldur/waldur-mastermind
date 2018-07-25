# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import waldur_core.logging.loggers
import waldur_core.core.fields
import django.core.validators
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0037_remove_customer_billing_backend_id'),
        ('openstack_tenant', '0001_initial'),
    ]

    operations = [
        migrations.CreateModel(
            name='Flavor',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, db_index=True)),
                ('cores', models.PositiveSmallIntegerField(help_text=b'Number of cores in a VM')),
                ('ram', models.PositiveIntegerField(help_text=b'Memory size in MiB')),
                ('disk', models.PositiveIntegerField(help_text=b'Root disk size in MiB')),
                ('settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='FloatingIP',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, db_index=True)),
                ('address', models.GenericIPAddressField(protocol=b'IPv4')),
                ('status', models.CharField(max_length=30)),
                ('backend_network_id', models.CharField(max_length=255, editable=False)),
                ('is_booked', models.BooleanField(default=False, help_text=b'Marks if floating IP has been booked for provisioning.')),
                ('settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
                'verbose_name': 'Floating IP',
                'verbose_name_plural': 'Floating IPs',
            },
        ),
        migrations.CreateModel(
            name='Image',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, db_index=True)),
                ('min_disk', models.PositiveIntegerField(default=0, help_text=b'Minimum disk size in MiB')),
                ('min_ram', models.PositiveIntegerField(default=0, help_text=b'Minimum memory size in MiB')),
                ('settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SecurityGroup',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, db_index=True)),
                ('settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SecurityGroupRule',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('protocol', models.CharField(blank=True, max_length=4, choices=[(b'tcp', b'tcp'), (b'udp', b'udp'), (b'icmp', b'icmp')])),
                ('from_port', models.IntegerField(null=True, validators=[django.core.validators.MaxValueValidator(65535)])),
                ('to_port', models.IntegerField(null=True, validators=[django.core.validators.MaxValueValidator(65535)])),
                ('cidr', models.CharField(max_length=32, blank=True)),
                ('backend_id', models.CharField(max_length=128, blank=True)),
                ('security_group', models.ForeignKey(related_name='rules', to='openstack_tenant.SecurityGroup')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AlterUniqueTogether(
            name='image',
            unique_together=set([('settings', 'backend_id')]),
        ),
        migrations.AlterUniqueTogether(
            name='floatingip',
            unique_together=set([('settings', 'backend_id')]),
        ),
        migrations.AlterUniqueTogether(
            name='flavor',
            unique_together=set([('settings', 'backend_id')]),
        ),
    ]
