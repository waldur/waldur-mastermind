import django.db.models.deletion
from django.db import migrations, models

import waldur_core.core.fields


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0070_router_external_gateway"),
    ]

    operations = [
        migrations.CreateModel(
            name="HypervisorInventory",
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
                ("uuid", waldur_core.core.fields.UUIDField()),
                (
                    "resource_class",
                    models.CharField(
                        help_text=(
                            "Placement resource class, e.g. VCPU, MEMORY_MB, "
                            "DISK_GB, VGPU, PCI_DEVICE, NUMA_CORE, CUSTOM_*."
                        ),
                        max_length=255,
                    ),
                ),
                ("total", models.PositiveBigIntegerField(default=0)),
                ("reserved", models.PositiveBigIntegerField(default=0)),
                ("allocation_ratio", models.FloatField(default=1.0)),
                ("used", models.PositiveBigIntegerField(default=0)),
                (
                    "hypervisor",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="inventories",
                        to="openstack.hypervisor",
                    ),
                ),
            ],
            options={
                "ordering": ("hypervisor", "resource_class"),
                "unique_together": {("hypervisor", "resource_class")},
            },
        ),
    ]
