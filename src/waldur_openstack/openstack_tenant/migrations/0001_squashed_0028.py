import django.core.validators
import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models
import waldur_core.core.validators
import waldur_core.logging.loggers
import waldur_core.structure.models
import waldur_openstack.openstack_tenant.models


class Migration(migrations.Migration):
    initial = True

    dependencies = [
        ('structure', '0001_squashed_0036'),
    ]

    operations = [
        migrations.CreateModel(
            name='Backup',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'kept_until',
                    models.DateTimeField(
                        blank=True,
                        help_text='Guaranteed time of backup retention. If null - keep forever.',
                        null=True,
                    ),
                ),
                (
                    'metadata',
                    waldur_core.core.fields.JSONField(
                        blank=True,
                        help_text='Additional information about backup, can be used for backup restoration or deletion',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.project',
                    ),
                ),
                (
                    'service_settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='Flavor',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                (
                    'cores',
                    models.PositiveSmallIntegerField(
                        help_text='Number of cores in a VM'
                    ),
                ),
                ('ram', models.PositiveIntegerField(help_text='Memory size in MiB')),
                (
                    'disk',
                    models.PositiveIntegerField(help_text='Root disk size in MiB'),
                ),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'ordering': ['name'],
                'unique_together': {('settings', 'backend_id')},
            },
            bases=(
                waldur_core.logging.loggers.LoggableMixin,
                waldur_core.core.models.BackendModelMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='Image',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                (
                    'min_disk',
                    models.PositiveIntegerField(
                        default=0, help_text='Minimum disk size in MiB'
                    ),
                ),
                (
                    'min_ram',
                    models.PositiveIntegerField(
                        default=0, help_text='Minimum memory size in MiB'
                    ),
                ),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'abstract': False,
                'ordering': ['name'],
                'unique_together': {('settings', 'backend_id')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.CreateModel(
            name='Instance',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                ('latitude', models.FloatField(blank=True, null=True)),
                ('longitude', models.FloatField(blank=True, null=True)),
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                (
                    'cores',
                    models.PositiveSmallIntegerField(
                        default=0, help_text='Number of cores in a VM'
                    ),
                ),
                (
                    'ram',
                    models.PositiveIntegerField(
                        default=0, help_text='Memory size in MiB'
                    ),
                ),
                (
                    'disk',
                    models.PositiveIntegerField(
                        default=0, help_text='Disk size in MiB'
                    ),
                ),
                (
                    'min_ram',
                    models.PositiveIntegerField(
                        default=0, help_text='Minimum memory size in MiB'
                    ),
                ),
                (
                    'min_disk',
                    models.PositiveIntegerField(
                        default=0, help_text='Minimum disk size in MiB'
                    ),
                ),
                ('image_name', models.CharField(blank=True, max_length=150)),
                ('key_name', models.CharField(blank=True, max_length=50)),
                ('key_fingerprint', models.CharField(blank=True, max_length=47)),
                (
                    'user_data',
                    models.TextField(
                        blank=True,
                        help_text='Additional data that will be added to instance on provisioning',
                    ),
                ),
                ('start_time', models.DateTimeField(blank=True, null=True)),
                ('backend_id', models.CharField(blank=True, max_length=255, null=True)),
                ('flavor_name', models.CharField(blank=True, max_length=255)),
                (
                    'flavor_disk',
                    models.PositiveIntegerField(
                        default=0, help_text='Flavor disk size in MiB'
                    ),
                ),
                ('action', models.CharField(blank=True, max_length=50)),
                ('action_details', waldur_core.core.fields.JSONField(default=dict)),
                ('hypervisor_hostname', models.CharField(blank=True, max_length=255)),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.project',
                    ),
                ),
                (
                    'service_settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'ordering': ['name', 'created'],
            },
            bases=(
                waldur_openstack.openstack_tenant.models.TenantQuotaMixin,
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='InstanceAvailabilityZone',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('available', models.BooleanField(default=True)),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'unique_together': {('settings', 'name')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.CreateModel(
            name='Network',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                ('is_external', models.BooleanField(default=False)),
                ('type', models.CharField(blank=True, max_length=50)),
                ('segmentation_id', models.IntegerField(null=True)),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'unique_together': {('settings', 'backend_id')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.CreateModel(
            name='SecurityGroup',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'unique_together': {('settings', 'backend_id')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.CreateModel(
            name='Snapshot',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                ('size', models.PositiveIntegerField(help_text='Size in MiB')),
                ('backend_id', models.CharField(blank=True, max_length=255, null=True)),
                ('metadata', waldur_core.core.fields.JSONField(blank=True)),
                ('action', models.CharField(blank=True, max_length=50)),
                ('action_details', waldur_core.core.fields.JSONField(default=dict)),
                (
                    'kept_until',
                    models.DateTimeField(
                        blank=True,
                        help_text='Guaranteed time of snapshot retention. If null - keep forever.',
                        null=True,
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.project',
                    ),
                ),
                (
                    'service_settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            bases=(
                waldur_openstack.openstack_tenant.models.TenantQuotaMixin,
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='SubNet',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                ('cidr', models.CharField(blank=True, max_length=32)),
                (
                    'gateway_ip',
                    models.GenericIPAddressField(null=True, protocol='IPv4'),
                ),
                ('allocation_pools', waldur_core.core.fields.JSONField(default=dict)),
                ('ip_version', models.SmallIntegerField(default=4)),
                ('enable_dhcp', models.BooleanField(default=True)),
                (
                    'dns_nameservers',
                    waldur_core.core.fields.JSONField(
                        default=list,
                        help_text='List of DNS name servers associated with the subnet.',
                    ),
                ),
                (
                    'network',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='subnets',
                        to='openstack_tenant.network',
                    ),
                ),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
                (
                    'is_connected',
                    models.BooleanField(
                        default=True,
                        help_text='Is subnet connected to the default tenant router.',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Subnet',
                'verbose_name_plural': 'Subnets',
                'unique_together': {('settings', 'backend_id')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.CreateModel(
            name='VolumeAvailabilityZone',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('available', models.BooleanField(default=True)),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'unique_together': {('settings', 'name')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.CreateModel(
            name='VolumeType',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
                ('is_default', models.BooleanField(default=False)),
            ],
            options={
                'unique_together': {('settings', 'backend_id')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.CreateModel(
            name='Volume',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                ('size', models.PositiveIntegerField(help_text='Size in MiB')),
                ('backend_id', models.CharField(blank=True, max_length=255, null=True)),
                (
                    'device',
                    models.CharField(
                        blank=True,
                        help_text='Name of volume as instance device e.g. /dev/vdb.',
                        max_length=50,
                        validators=[
                            django.core.validators.RegexValidator(
                                '^/dev/[a-zA-Z0-9]+$',
                                message='Device should match pattern "/dev/alphanumeric+"',
                            )
                        ],
                    ),
                ),
                ('bootable', models.BooleanField(default=False)),
                ('metadata', waldur_core.core.fields.JSONField(blank=True)),
                ('image_name', models.CharField(blank=True, max_length=150)),
                ('image_metadata', waldur_core.core.fields.JSONField(blank=True)),
                ('action', models.CharField(blank=True, max_length=50)),
                ('action_details', waldur_core.core.fields.JSONField(default=dict)),
                (
                    'availability_zone',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='openstack_tenant.volumeavailabilityzone',
                    ),
                ),
                (
                    'image',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='openstack_tenant.image',
                    ),
                ),
                (
                    'instance',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='volumes',
                        to='openstack_tenant.instance',
                    ),
                ),
                (
                    'source_snapshot',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='volumes',
                        to='openstack_tenant.snapshot',
                    ),
                ),
                (
                    'type',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='openstack_tenant.volumetype',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.project',
                    ),
                ),
                (
                    'service_settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'unique_together': {('service_settings', 'backend_id')},
            },
            bases=(
                waldur_openstack.openstack_tenant.models.TenantQuotaMixin,
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='SnapshotSchedule',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'schedule',
                    waldur_core.core.fields.CronScheduleField(
                        max_length=15,
                        validators=[
                            waldur_core.core.validators.validate_cron_schedule,
                            waldur_core.core.validators.MinCronValueValidator(1),
                        ],
                    ),
                ),
                ('next_trigger_at', models.DateTimeField(null=True)),
                (
                    'timezone',
                    models.CharField(
                        default=django.utils.timezone.get_current_timezone_name,
                        max_length=50,
                    ),
                ),
                ('is_active', models.BooleanField(default=False)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'retention_time',
                    models.PositiveIntegerField(
                        help_text='Retention time in days, if 0 - resource will be kept forever'
                    ),
                ),
                ('maximal_number_of_resources', models.PositiveSmallIntegerField()),
                (
                    'call_count',
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text='How many times a resource schedule was called.',
                    ),
                ),
                (
                    'source_volume',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='snapshot_schedules',
                        to='openstack_tenant.volume',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.project',
                    ),
                ),
                (
                    'service_settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='SnapshotRestoration',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'snapshot',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='restorations',
                        to='openstack_tenant.snapshot',
                    ),
                ),
                (
                    'volume',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='restoration',
                        to='openstack_tenant.volume',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='snapshot',
            name='snapshot_schedule',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='snapshots',
                to='openstack_tenant.snapshotschedule',
            ),
        ),
        migrations.AddField(
            model_name='snapshot',
            name='source_volume',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='snapshots',
                to='openstack_tenant.volume',
            ),
        ),
        migrations.CreateModel(
            name='InternalIP',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                ('mac_address', models.CharField(blank=True, max_length=32)),
                ('backend_id', models.CharField(max_length=255, null=True)),
                (
                    'instance',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='internal_ips_set',
                        to='openstack_tenant.instance',
                    ),
                ),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
                (
                    'subnet',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='internal_ips',
                        to='openstack_tenant.subnet',
                    ),
                ),
                (
                    'allowed_address_pairs',
                    waldur_core.core.fields.JSONField(
                        default=list,
                        help_text='A server can send a packet with source address which matches one of the specified allowed address pairs.',
                    ),
                ),
                (
                    'fixed_ips',
                    waldur_core.core.fields.JSONField(
                        default=list,
                        help_text='A list of tuples (ip_address, subnet_id), where ip_address can be both IPv4 and IPv6 and subnet_id is a backend id of the subnet',
                    ),
                ),
                ('device_id', models.CharField(blank=True, max_length=255, null=True)),
                (
                    'device_owner',
                    models.CharField(blank=True, max_length=100, null=True),
                ),
            ],
            options={
                'unique_together': {('backend_id', 'settings')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.AddField(
            model_name='instance',
            name='availability_zone',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='openstack_tenant.instanceavailabilityzone',
            ),
        ),
        migrations.AddField(
            model_name='instance',
            name='security_groups',
            field=models.ManyToManyField(
                related_name='instances', to='openstack_tenant.SecurityGroup'
            ),
        ),
        migrations.AddField(
            model_name='instance',
            name='subnets',
            field=models.ManyToManyField(
                through='openstack_tenant.InternalIP', to='openstack_tenant.SubNet'
            ),
        ),
        migrations.CreateModel(
            name='BackupSchedule',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('error_message', models.TextField(blank=True)),
                (
                    'schedule',
                    waldur_core.core.fields.CronScheduleField(
                        max_length=15,
                        validators=[
                            waldur_core.core.validators.validate_cron_schedule,
                            waldur_core.core.validators.MinCronValueValidator(1),
                        ],
                    ),
                ),
                ('next_trigger_at', models.DateTimeField(null=True)),
                (
                    'timezone',
                    models.CharField(
                        default=django.utils.timezone.get_current_timezone_name,
                        max_length=50,
                    ),
                ),
                ('is_active', models.BooleanField(default=False)),
                (
                    'state',
                    django_fsm.FSMIntegerField(
                        choices=[
                            (5, 'Creation Scheduled'),
                            (6, 'Creating'),
                            (1, 'Update Scheduled'),
                            (2, 'Updating'),
                            (7, 'Deletion Scheduled'),
                            (8, 'Deleting'),
                            (3, 'OK'),
                            (4, 'Erred'),
                        ],
                        default=5,
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'retention_time',
                    models.PositiveIntegerField(
                        help_text='Retention time in days, if 0 - resource will be kept forever'
                    ),
                ),
                ('maximal_number_of_resources', models.PositiveSmallIntegerField()),
                (
                    'call_count',
                    models.PositiveSmallIntegerField(
                        default=0,
                        help_text='How many times a resource schedule was called.',
                    ),
                ),
                (
                    'instance',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='backup_schedules',
                        to='openstack_tenant.instance',
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.project',
                    ),
                ),
                (
                    'service_settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='BackupRestoration',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'created',
                    model_utils.fields.AutoCreatedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='created',
                    ),
                ),
                (
                    'modified',
                    model_utils.fields.AutoLastModifiedField(
                        default=django.utils.timezone.now,
                        editable=False,
                        verbose_name='modified',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'backup',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='restorations',
                        to='openstack_tenant.backup',
                    ),
                ),
                (
                    'flavor',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='+',
                        to='openstack_tenant.flavor',
                    ),
                ),
                (
                    'instance',
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='openstack_tenant.instance',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.AddField(
            model_name='backup',
            name='backup_schedule',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name='backups',
                to='openstack_tenant.backupschedule',
            ),
        ),
        migrations.AddField(
            model_name='backup',
            name='instance',
            field=models.ForeignKey(
                on_delete=django.db.models.deletion.PROTECT,
                related_name='backups',
                to='openstack_tenant.instance',
            ),
        ),
        migrations.AddField(
            model_name='backup',
            name='snapshots',
            field=models.ManyToManyField(
                related_name='backups', to='openstack_tenant.Snapshot'
            ),
        ),
        migrations.CreateModel(
            name='FloatingIP',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                (
                    'address',
                    models.GenericIPAddressField(
                        default=None, null=True, protocol='IPv4'
                    ),
                ),
                ('runtime_state', models.CharField(max_length=30)),
                (
                    'backend_network_id',
                    models.CharField(editable=False, max_length=255),
                ),
                (
                    'is_booked',
                    models.BooleanField(
                        default=False,
                        help_text='Marks if floating IP has been booked for provisioning.',
                    ),
                ),
                (
                    'internal_ip',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name='floating_ips',
                        to='openstack_tenant.internalip',
                    ),
                ),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'verbose_name': 'Floating IP',
                'verbose_name_plural': 'Floating IPs',
                'unique_together': {('settings', 'address')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.AlterUniqueTogether(
            name='instance',
            unique_together={('service_settings', 'backend_id')},
        ),
        migrations.AlterUniqueTogether(
            name='snapshot',
            unique_together={('service_settings', 'backend_id')},
        ),
        migrations.CreateModel(
            name='SecurityGroupRule',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'protocol',
                    models.CharField(
                        blank=True,
                        choices=[('tcp', 'tcp'), ('udp', 'udp'), ('icmp', 'icmp')],
                        max_length=40,
                    ),
                ),
                (
                    'from_port',
                    models.IntegerField(
                        null=True,
                        validators=[django.core.validators.MaxValueValidator(65535)],
                    ),
                ),
                (
                    'to_port',
                    models.IntegerField(
                        null=True,
                        validators=[django.core.validators.MaxValueValidator(65535)],
                    ),
                ),
                ('cidr', models.CharField(blank=True, max_length=255, null=True)),
                ('backend_id', models.CharField(blank=True, max_length=36)),
                (
                    'security_group',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='rules',
                        to='openstack_tenant.securitygroup',
                    ),
                ),
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                (
                    'direction',
                    models.CharField(
                        choices=[('ingress', 'ingress'), ('egress', 'egress')],
                        default='ingress',
                        max_length=8,
                    ),
                ),
                (
                    'ethertype',
                    models.CharField(
                        choices=[('IPv4', 'IPv4'), ('IPv6', 'IPv6')],
                        default='IPv4',
                        max_length=40,
                    ),
                ),
                (
                    'remote_group',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='openstack_tenant.securitygroup',
                    ),
                ),
            ],
            options={
                'abstract': False,
                'unique_together': {('security_group', 'backend_id')},
            },
        ),
        migrations.CreateModel(
            name='ServerGroup',
            fields=[
                (
                    'id',
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name='ID',
                    ),
                ),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(db_index=True, max_length=255)),
                (
                    'policy',
                    models.CharField(
                        blank=True, choices=[('affinity', 'Affinity')], max_length=40
                    ),
                ),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
                    ),
                ),
            ],
            options={
                'unique_together': {('settings', 'backend_id')},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
        migrations.AlterField(
            model_name='instance',
            name='security_groups',
            field=models.ManyToManyField(
                blank=True,
                related_name='instances',
                to='openstack_tenant.SecurityGroup',
            ),
        ),
        migrations.AddField(
            model_name='instance',
            name='server_group',
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                to='openstack_tenant.servergroup',
            ),
        ),
    ]
