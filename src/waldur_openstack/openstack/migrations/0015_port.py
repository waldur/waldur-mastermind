# Generated by Django 2.2.13 on 2020-11-17 17:30

import django.db.models.deletion
import django.utils.timezone
import django_fsm
import model_utils.fields
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models
import waldur_core.core.shims
import waldur_core.core.validators
import waldur_core.structure.models


class Migration(migrations.Migration):

    dependencies = [
        ('taggit', '0003_taggeditem_add_unique_index'),
        ('openstack', '0014_securitygrouprule_ethertype'),
    ]

    operations = [
        migrations.CreateModel(
            name='Port',
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
                ('error_traceback', models.TextField(blank=True)),
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
                ('mac_address', models.CharField(blank=True, max_length=32)),
                (
                    'ip4_address',
                    models.GenericIPAddressField(
                        blank=True, null=True, protocol='IPv4'
                    ),
                ),
                (
                    'ip6_address',
                    models.GenericIPAddressField(
                        blank=True, null=True, protocol='IPv6'
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
                    'network',
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ports',
                        to='openstack.Network',
                    ),
                ),
                (
                    'tags',
                    waldur_core.core.shims.TaggableManager(
                        blank=True,
                        help_text='A comma-separated list of tags.',
                        related_name='port_port_openstack',
                        through='taggit.TaggedItem',
                        to='taggit.Tag',
                        verbose_name='Tags',
                    ),
                ),
                (
                    'tenant',
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name='ports',
                        to='openstack.Tenant',
                    ),
                ),
            ],
            options={'abstract': False,},
            bases=(
                waldur_core.core.models.DescendantMixin,
                waldur_core.core.models.BackendModelMixin,
                waldur_core.structure.models.StructureLoggableMixin,
                models.Model,
            ),
        ),
    ]