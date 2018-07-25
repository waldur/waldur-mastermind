# -*- coding: utf-8 -*-
from __future__ import unicode_literals

from django.db import migrations, models
import django_fsm
import waldur_core.structure.models
import waldur_core.core.models
import django.core.validators
import taggit.managers
import waldur_core.logging.loggers
import model_utils.fields
import waldur_core.core.fields
import django.db.models.deletion
import django.utils.timezone
import waldur_core.core.validators


class Migration(migrations.Migration):

    dependencies = [
        ('structure', '0035_settings_tags_and_scope'),
        ('taggit', '0002_auto_20150616_2121'),
    ]

    operations = [
        migrations.CreateModel(
            name='Backup',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('kept_until', models.DateTimeField(help_text='Guaranteed time of backup retention. If null - keep forever.', null=True, blank=True)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('state', django_fsm.FSMIntegerField(default=1, choices=[(1, 'Ready'), (2, 'Backing up'), (3, 'Restoring'), (4, 'Deleting'), (5, 'Erred'), (6, 'Deleted')])),
                ('metadata', waldur_core.core.fields.JSONField(help_text='Additional information about backup, can be used for backup restoration or deletion', blank=True)),
            ],
            options={
                'abstract': False,
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name='BackupSchedule',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('schedule', waldur_core.core.fields.CronScheduleField(max_length=15, validators=[waldur_core.core.validators.validate_cron_schedule])),
                ('next_trigger_at', models.DateTimeField(null=True)),
                ('timezone', models.CharField(default=django.utils.timezone.get_current_timezone_name, max_length=50)),
                ('is_active', models.BooleanField(default=False)),
                ('retention_time', models.PositiveIntegerField(help_text='Retention time in days')),
                ('maximal_number_of_backups', models.PositiveSmallIntegerField()),
            ],
            options={
                'abstract': False,
            },
            bases=(models.Model, waldur_core.logging.loggers.LoggableMixin),
        ),
        migrations.CreateModel(
            name='Flavor',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(max_length=255, db_index=True)),
                ('cores', models.PositiveSmallIntegerField(help_text='Number of cores in a VM')),
                ('ram', models.PositiveIntegerField(help_text='Memory size in MiB')),
                ('disk', models.PositiveIntegerField(help_text='Root disk size in MiB')),
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
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('address', models.GenericIPAddressField(protocol='IPv4')),
                ('status', models.CharField(max_length=30)),
                ('backend_id', models.CharField(max_length=255)),
                ('backend_network_id', models.CharField(max_length=255, editable=False)),
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
                ('min_disk', models.PositiveIntegerField(default=0, help_text='Minimum disk size in MiB')),
                ('min_ram', models.PositiveIntegerField(default=0, help_text='Minimum memory size in MiB')),
                ('settings', models.ForeignKey(related_name='+', to='structure.ServiceSettings')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Instance',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('latitude', models.FloatField(null=True, blank=True)),
                ('longitude', models.FloatField(null=True, blank=True)),
                ('key_name', models.CharField(max_length=50, blank=True)),
                ('key_fingerprint', models.CharField(max_length=47, blank=True)),
                ('user_data', models.TextField(help_text='Additional data that will be added to instance on provisioning', blank=True)),
                ('cores', models.PositiveSmallIntegerField(default=0, help_text='Number of cores in a VM')),
                ('ram', models.PositiveIntegerField(default=0, help_text='Memory size in MiB')),
                ('disk', models.PositiveIntegerField(default=0, help_text='Disk size in MiB')),
                ('min_ram', models.PositiveIntegerField(default=0, help_text='Minimum memory size in MiB')),
                ('min_disk', models.PositiveIntegerField(default=0, help_text='Minimum disk size in MiB')),
                ('external_ips', models.GenericIPAddressField(null=True, protocol='IPv4', blank=True)),
                ('internal_ips', models.GenericIPAddressField(null=True, protocol='IPv4', blank=True)),
                ('image_name', models.CharField(max_length=150, blank=True)),
                ('billing_backend_id', models.CharField(help_text='ID of a resource in backend', max_length=255, blank=True)),
                ('last_usage_update_time', models.DateTimeField(null=True, blank=True)),
                ('state', django_fsm.FSMIntegerField(default=1, help_text='WARNING! Should not be changed manually unless you really know what you are doing.', choices=[(1, 'Provisioning Scheduled'), (2, 'Provisioning'), (3, 'Online'), (4, 'Offline'), (5, 'Starting Scheduled'), (6, 'Starting'), (7, 'Stopping Scheduled'), (8, 'Stopping'), (9, 'Erred'), (10, 'Deletion Scheduled'), (11, 'Deleting'), (13, 'Resizing Scheduled'), (14, 'Resizing'), (15, 'Restarting Scheduled'), (16, 'Restarting')])),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('start_time', models.DateTimeField(null=True, blank=True)),
                ('system_volume_id', models.CharField(max_length=255, blank=True)),
                ('system_volume_size', models.PositiveIntegerField(default=0, help_text='Root disk size in MiB')),
                ('data_volume_id', models.CharField(max_length=255, blank=True)),
                ('data_volume_size', models.PositiveIntegerField(default=20480, help_text='Data disk size in MiB', validators=[django.core.validators.MinValueValidator(1024)])),
                ('flavor_name', models.CharField(max_length=255, blank=True)),
                ('flavor_disk', models.PositiveIntegerField(default=0, help_text='Flavor disk size in MiB')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='InstanceSecurityGroup',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('instance', models.ForeignKey(related_name='security_groups', to='openstack.Instance')),
            ],
        ),
        migrations.CreateModel(
            name='OpenStackService',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('available_for_all', models.BooleanField(default=False, help_text='Service will be automatically added to all customers projects if it is available for all')),
                ('customer', models.ForeignKey(verbose_name='organization', to='structure.Customer')),
            ],
            options={
                'verbose_name': 'OpenStack provider',
                'verbose_name_plural': 'OpenStack providers',
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='OpenStackServiceProjectLink',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('project', models.ForeignKey(to='structure.Project')),
                ('service', models.ForeignKey(to='openstack.OpenStackService')),
            ],
            options={
                'abstract': False,
                'verbose_name': 'OpenStack provider project link',
                'verbose_name_plural': 'OpenStack provider project links',
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.CreateModel(
            name='SecurityGroup',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('state', django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')])),
                ('backend_id', models.CharField(max_length=128, blank=True)),
                ('service_project_link', models.ForeignKey(related_name='security_groups', to='openstack.OpenStackServiceProjectLink')),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='SecurityGroupRule',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('protocol', models.CharField(blank=True, max_length=4, choices=[('tcp', 'tcp'), ('udp', 'udp'), ('icmp', 'icmp')])),
                ('from_port', models.IntegerField(null=True, validators=[django.core.validators.MaxValueValidator(65535)])),
                ('to_port', models.IntegerField(null=True, validators=[django.core.validators.MaxValueValidator(65535)])),
                ('cidr', models.CharField(max_length=32, blank=True)),
                ('backend_id', models.CharField(max_length=128, blank=True)),
                ('security_group', models.ForeignKey(related_name='rules', to='openstack.SecurityGroup')),
            ],
        ),
        migrations.CreateModel(
            name='Tenant',
            fields=[
                ('id', models.AutoField(verbose_name='ID', serialize=False, auto_created=True, primary_key=True)),
                ('created', model_utils.fields.AutoCreatedField(default=django.utils.timezone.now, verbose_name='created', editable=False)),
                ('modified', model_utils.fields.AutoLastModifiedField(default=django.utils.timezone.now, verbose_name='modified', editable=False)),
                ('description', models.CharField(max_length=500, verbose_name='description', blank=True)),
                ('name', models.CharField(max_length=150, verbose_name='name', validators=[waldur_core.core.validators.validate_name])),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('runtime_state', models.CharField(max_length=150, verbose_name='runtime state', blank=True)),
                ('state', django_fsm.FSMIntegerField(default=5, choices=[(5, 'Creation Scheduled'), (6, 'Creating'), (1, 'Update Scheduled'), (2, 'Updating'), (7, 'Deletion Scheduled'), (8, 'Deleting'), (3, 'OK'), (4, 'Erred')])),
                ('backend_id', models.CharField(max_length=255, blank=True)),
                ('start_time', models.DateTimeField(null=True, blank=True)),
                ('internal_network_id', models.CharField(max_length=64, blank=True)),
                ('external_network_id', models.CharField(max_length=64, blank=True)),
                ('availability_zone', models.CharField(help_text='Optional availability group. Will be used for all instances provisioned in this tenant', max_length=100, blank=True)),
                ('user_username', models.CharField(max_length=50, blank=True)),
                ('user_password', models.CharField(max_length=50, blank=True)),
                ('service_project_link', models.ForeignKey(related_name='tenants', on_delete=django.db.models.deletion.PROTECT, to='openstack.OpenStackServiceProjectLink')),
                ('tags', taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags')),
            ],
            options={
                'abstract': False,
            },
            bases=(waldur_core.core.models.DescendantMixin, waldur_core.logging.loggers.LoggableMixin, models.Model),
        ),
        migrations.AddField(
            model_name='openstackservice',
            name='projects',
            field=models.ManyToManyField(related_name='openstack_services', through='openstack.OpenStackServiceProjectLink', to='structure.Project'),
        ),
        migrations.AddField(
            model_name='openstackservice',
            name='settings',
            field=models.ForeignKey(to='structure.ServiceSettings'),
        ),
        migrations.AddField(
            model_name='instancesecuritygroup',
            name='security_group',
            field=models.ForeignKey(related_name='instance_groups', to='openstack.SecurityGroup'),
        ),
        migrations.AddField(
            model_name='instance',
            name='service_project_link',
            field=models.ForeignKey(related_name='instances', on_delete=django.db.models.deletion.PROTECT, to='openstack.OpenStackServiceProjectLink'),
        ),
        migrations.AddField(
            model_name='instance',
            name='tags',
            field=taggit.managers.TaggableManager(to='taggit.Tag', through='taggit.TaggedItem', blank=True, help_text='A comma-separated list of tags.', verbose_name='Tags'),
        ),
        migrations.AddField(
            model_name='floatingip',
            name='service_project_link',
            field=models.ForeignKey(related_name='floating_ips', to='openstack.OpenStackServiceProjectLink'),
        ),
        migrations.AddField(
            model_name='backupschedule',
            name='instance',
            field=models.ForeignKey(related_name='backup_schedules', to='openstack.Instance'),
        ),
        migrations.AddField(
            model_name='backup',
            name='backup_schedule',
            field=models.ForeignKey(related_name='backups', on_delete=django.db.models.deletion.SET_NULL, blank=True, to='openstack.BackupSchedule', null=True),
        ),
        migrations.AddField(
            model_name='backup',
            name='instance',
            field=models.ForeignKey(related_name='backups', to='openstack.Instance'),
        ),
        migrations.AlterUniqueTogether(
            name='openstackserviceprojectlink',
            unique_together=set([('service', 'project')]),
        ),
        migrations.AlterUniqueTogether(
            name='openstackservice',
            unique_together=set([('customer', 'settings')]),
        ),
        migrations.AlterUniqueTogether(
            name='image',
            unique_together=set([('settings', 'backend_id')]),
        ),
        migrations.AlterUniqueTogether(
            name='flavor',
            unique_together=set([('settings', 'backend_id')]),
        ),
    ]
