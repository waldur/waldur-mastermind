import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields
import waldur_core.core.models
import waldur_core.core.validators


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0068_remove_loadbalancer_vip_subnet_id_and_more"),
        ("structure", "0073_alter_projectdigestconfiguration_uuid"),
    ]

    operations = [
        migrations.CreateModel(
            name="Hypervisor",
            fields=[
                (
                    "id",
                    models.AutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "name",
                    models.CharField(
                        max_length=150,
                        validators=[waldur_core.core.validators.validate_name],
                        verbose_name="name",
                    ),
                ),
                ("uuid", waldur_core.core.fields.UUIDField()),
                ("backend_id", models.CharField(db_index=True, max_length=255)),
                (
                    "hypervisor_type",
                    models.CharField(
                        blank=True,
                        help_text="Hypervisor type, e.g. KVM, QEMU, VMware",
                        max_length=50,
                    ),
                ),
                (
                    "vcpus",
                    models.PositiveIntegerField(default=0, help_text="Total vCPUs"),
                ),
                (
                    "vcpus_used",
                    models.PositiveIntegerField(default=0, help_text="Used vCPUs"),
                ),
                (
                    "memory_mb",
                    models.PositiveIntegerField(
                        default=0, help_text="Total RAM in MiB"
                    ),
                ),
                (
                    "memory_mb_used",
                    models.PositiveIntegerField(default=0, help_text="Used RAM in MiB"),
                ),
                (
                    "local_gb",
                    models.PositiveIntegerField(
                        default=0, help_text="Total disk in GiB"
                    ),
                ),
                (
                    "local_gb_used",
                    models.PositiveIntegerField(
                        default=0, help_text="Used disk in GiB"
                    ),
                ),
                (
                    "running_vms",
                    models.PositiveIntegerField(
                        default=0, help_text="Number of running VMs"
                    ),
                ),
                (
                    "state",
                    models.CharField(
                        blank=True,
                        help_text="Hypervisor state, e.g. up or down",
                        max_length=50,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        blank=True,
                        help_text="Hypervisor status, e.g. enabled or disabled",
                        max_length=50,
                    ),
                ),
                (
                    "settings",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="+",
                        to="structure.servicesettings",
                    ),
                ),
            ],
            options={
                "abstract": False,
                "unique_together": {("settings", "backend_id")},
            },
            bases=(waldur_core.core.models.BackendModelMixin, models.Model),
        ),
    ]
