import django.contrib.postgres.fields
import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.conf import settings
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models
import waldur_core.core.validators
import waldur_core.structure.models
import waldur_rancher.models


class Migration(migrations.Migration):
    replaces = [
        ('waldur_rancher', '0001_initial'),
        ('waldur_rancher', '0002_cluster_backend_id_not_null'),
        ('waldur_rancher', '0003_node_roles'),
        ('waldur_rancher', '0004_node_backend_id'),
        ('waldur_rancher', '0005_node_name_is_unique'),
        ('waldur_rancher', '0006_node_initial_data'),
        ('waldur_rancher', '0007_cluster_tenant_settings'),
        ('waldur_rancher', '0008_node_annotations'),
        ('waldur_rancher', '0009_runtime_state'),
        ('waldur_rancher', '0010_rancher_user'),
        ('waldur_rancher', '0011_catalog'),
        ('waldur_rancher', '0012_cluster_initial_data'),
        ('waldur_rancher', '0013_remove_cluster_initial_data'),
        ('waldur_rancher', '0014_drop_constraints'),
        ('waldur_rancher', '0015_project'),
        ('waldur_rancher', '0016_namespace'),
        ('waldur_rancher', '0017_add_settings_and_template'),
        ('waldur_rancher', '0017_pull_settings'),
        ('waldur_rancher', '0018_template_icon'),
        ('waldur_rancher', '0019_settings_non_null'),
        ('waldur_rancher', '0020_extend_icon_url_size'),
        ('waldur_rancher', '0021_rancher_user_uuid'),
        ('waldur_rancher', '0022_rancheruserprojectlink'),
        ('waldur_rancher', '0023_workload'),
        ('waldur_rancher', '0024_hpa'),
        ('waldur_rancher', '0025_hpa_state'),
        ('waldur_rancher', '0026_hpa_description'),
        ('waldur_rancher', '0027_cluster_template'),
        ('waldur_rancher', '0028_verbose_name_for_template'),
        ('waldur_rancher', '0029_application'),
        ('waldur_rancher', '0030_ingress'),
        ('waldur_rancher', '0031_extend_description_limits'),
        ('waldur_rancher', '0032_service'),
        ('waldur_rancher', '0033_error_traceback'),
        ('waldur_rancher', '0034_delete_catalogs_without_scope'),
        ('waldur_rancher', '0035_drop_spl'),
        ('waldur_rancher', '0036_cluster_management_security_group'),
        ('waldur_rancher', '0037_json_field'),
    ]

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('structure', '0001_squashed_0036'),
        ('contenttypes', '0002_remove_content_type_name'),
    ]

    operations = [
        migrations.CreateModel(
            name='Cluster',
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
                (
                    'backend_id',
                    models.CharField(blank=True, max_length=255),
                ),
                (
                    'node_command',
                    models.CharField(
                        blank=True,
                        help_text='Rancher generated node installation command base.',
                        max_length=1024,
                    ),
                ),
                (
                    'tenant_settings',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        to='structure.servicesettings',
                    ),
                ),
                ('runtime_state', models.CharField(blank=True, max_length=255)),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='structure.servicesettings',
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
                (
                    'management_security_group',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        to='openstack.securitygroup',
                    ),
                ),
            ],
            options={
                'unique_together': set(),
            },
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
        migrations.CreateModel(
            name='RancherUser',
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
                ('uuid', waldur_core.core.fields.UUIDField()),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('is_active', models.BooleanField(default=True)),
                (
                    'settings',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to='structure.servicesettings',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                'unique_together': {('user', 'settings')},
                'ordering': ('user__username',),
            },
        ),
        migrations.CreateModel(
            name='RancherUserClusterLink',
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
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'role',
                    waldur_rancher.models.ClusterRole(
                        choices=[
                            ('owner', 'Cluster owner'),
                            ('member', 'Cluster member'),
                        ],
                        db_index=True,
                        max_length=30,
                    ),
                ),
                (
                    'cluster',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.cluster',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.rancheruser',
                    ),
                ),
            ],
            options={
                'unique_together': {('user', 'cluster', 'role')},
            },
        ),
        migrations.AddField(
            model_name='rancheruser',
            name='clusters',
            field=models.ManyToManyField(
                through='waldur_rancher.RancherUserClusterLink',
                to='waldur_rancher.Cluster',
            ),
        ),
        migrations.CreateModel(
            name='Project',
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
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'cluster',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.cluster',
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
            },
        ),
        migrations.CreateModel(
            name='Catalog',
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
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                ('object_id', models.PositiveIntegerField(null=True)),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('catalog_url', models.URLField()),
                ('branch', models.CharField(max_length=255)),
                ('commit', models.CharField(blank=True, max_length=40)),
                ('username', models.CharField(blank=True, max_length=255)),
                ('password', models.CharField(blank=True, max_length=255)),
                (
                    'content_type',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='contenttypes.contenttype',
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
            },
        ),
        migrations.CreateModel(
            name='Namespace',
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
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'project',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='namespaces',
                        to='waldur_rancher.project',
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
            },
        ),
        migrations.CreateModel(
            name='Template',
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
                (
                    'icon_url',
                    models.URLField(
                        blank=True, max_length=500, verbose_name='icon url'
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('project_url', models.URLField(blank=True, max_length=500)),
                ('default_version', models.CharField(max_length=255)),
                (
                    'versions',
                    django.contrib.postgres.fields.ArrayField(
                        base_field=models.CharField(max_length=255), size=None
                    ),
                ),
                (
                    'catalog',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.catalog',
                    ),
                ),
                (
                    'cluster',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.cluster',
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.project',
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
                    'icon',
                    models.FileField(blank=True, null=True, upload_to='rancher_icons'),
                ),
            ],
            options={
                'abstract': False,
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='RancherUserProjectLink',
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
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('role', models.CharField(max_length=255)),
                (
                    'project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.project',
                    ),
                ),
                (
                    'user',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.rancheruser',
                    ),
                ),
            ],
            options={
                'unique_together': {('user', 'project', 'role')},
            },
        ),
        migrations.CreateModel(
            name='Workload',
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
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('scale', models.PositiveSmallIntegerField()),
                (
                    'cluster',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.cluster',
                    ),
                ),
                (
                    'namespace',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.namespace',
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.project',
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
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='ClusterTemplate',
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
            ],
            options={
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='ClusterTemplateNode',
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
                ('controlplane_role', models.BooleanField(default=False)),
                ('etcd_role', models.BooleanField(default=False)),
                ('worker_role', models.BooleanField(default=False)),
                (
                    'min_vcpu',
                    models.PositiveSmallIntegerField(verbose_name='Min vCPU (cores)'),
                ),
                ('min_ram', models.PositiveIntegerField(verbose_name='Min RAM (GB)')),
                (
                    'system_volume_size',
                    models.PositiveIntegerField(verbose_name='System volume size (GB)'),
                ),
                ('preferred_volume_type', models.CharField(blank=True, max_length=150)),
                (
                    'template',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='nodes',
                        to='waldur_rancher.clustertemplate',
                    ),
                ),
            ],
            options={
                'abstract': False,
            },
        ),
        migrations.CreateModel(
            name='Application',
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
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('version', models.CharField(max_length=100)),
                ('answers', models.JSONField(blank=True, default=dict)),
                (
                    'cluster',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.cluster',
                    ),
                ),
                (
                    'namespace',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.namespace',
                    ),
                ),
                (
                    'rancher_project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.project',
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
                    'template',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.template',
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
            name='HPA',
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
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
                    ),
                ),
                ('uuid', waldur_core.core.fields.UUIDField()),
                (
                    'runtime_state',
                    models.CharField(
                        blank=True, max_length=150, verbose_name='runtime state'
                    ),
                ),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('current_replicas', models.PositiveSmallIntegerField(default=0)),
                ('desired_replicas', models.PositiveSmallIntegerField(default=0)),
                ('min_replicas', models.PositiveSmallIntegerField(default=0)),
                ('max_replicas', models.PositiveSmallIntegerField(default=0)),
                ('metrics', models.JSONField()),
                (
                    'cluster',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.cluster',
                    ),
                ),
                (
                    'namespace',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.namespace',
                    ),
                ),
                (
                    'project',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.project',
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
                    'workload',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='waldur_rancher.workload',
                    ),
                ),
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
                (
                    'description',
                    models.CharField(
                        blank=True, max_length=2000, verbose_name='description'
                    ),
                ),
                ('error_traceback', models.TextField(blank=True)),
            ],
            options={
                'ordering': ('name',),
            },
        ),
        migrations.CreateModel(
            name='Ingress',
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
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('rules', models.JSONField(blank=True, default=list)),
                (
                    'cluster',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.cluster',
                    ),
                ),
                (
                    'namespace',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.namespace',
                    ),
                ),
                (
                    'rancher_project',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.project',
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
            name='Node',
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
                ('object_id', models.PositiveIntegerField(null=True)),
                (
                    'cluster',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.cluster',
                    ),
                ),
                (
                    'content_type',
                    models.ForeignKey(
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='+',
                        to='contenttypes.contenttype',
                    ),
                ),
                ('controlplane_role', models.BooleanField(default=False)),
                ('etcd_role', models.BooleanField(default=False)),
                ('worker_role', models.BooleanField(default=False)),
                ('backend_id', models.CharField(blank=True, max_length=255)),
                ('error_message', models.TextField(blank=True)),
                (
                    'name',
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name='name',
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
                    'initial_data',
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text='Initial data for instance creating.',
                    ),
                ),
                ('annotations', models.JSONField(blank=True, default=dict)),
                ('cpu_allocated', models.FloatField(blank=True, null=True)),
                ('cpu_total', models.IntegerField(blank=True, null=True)),
                ('docker_version', models.CharField(blank=True, max_length=255)),
                ('k8s_version', models.CharField(blank=True, max_length=255)),
                ('labels', models.JSONField(blank=True, default=dict)),
                ('pods_allocated', models.IntegerField(blank=True, null=True)),
                ('pods_total', models.IntegerField(blank=True, null=True)),
                (
                    'ram_allocated',
                    models.IntegerField(
                        blank=True, help_text='Allocated RAM in Mi.', null=True
                    ),
                ),
                (
                    'ram_total',
                    models.IntegerField(
                        blank=True, help_text='Total RAM in Mi.', null=True
                    ),
                ),
                ('runtime_state', models.CharField(blank=True, max_length=255)),
                ('error_traceback', models.TextField(blank=True)),
            ],
            options={
                'ordering': ('name',),
                'unique_together': {('content_type', 'object_id'), ('cluster', 'name')},
            },
        ),
        migrations.CreateModel(
            name='Service',
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
                ('backend_id', models.CharField(blank=True, max_length=255)),
                (
                    'cluster_ip',
                    models.GenericIPAddressField(
                        blank=True, null=True, protocol='IPv4'
                    ),
                ),
                ('selector', models.JSONField(blank=True, null=True)),
                (
                    'namespace',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        to='waldur_rancher.namespace',
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
                    'target_workloads',
                    models.ManyToManyField(to='waldur_rancher.Workload'),
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
    ]
