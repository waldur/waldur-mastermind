"""Schema + data migration for the rescue feature [WAL-8603].

Adds two Glance-derived columns to Image (hw_rescue_device, hw_rescue_bus)
that identify "stable device rescue" images, and back-fills the runtime_state
column on Instance so any pre-existing "RESCUED" rows match Nova's actual
top-level status string ("RESCUE"). The data fix is expected to be a no-op
in practice — instance.runtime_state is populated verbatim from
backend_instance.status, and Nova has always emitted "RESCUE" — but it's
cheap insurance against any out-of-band writes that may have used the
formerly-declared "RESCUED" constant.
"""

from django.db import migrations, models


def fix_rescued_runtime_state(apps, schema_editor):
    Instance = apps.get_model("openstack", "Instance")
    Instance.objects.filter(runtime_state="RESCUED").update(runtime_state="RESCUE")


def reverse_fix_rescued_runtime_state(apps, schema_editor):
    Instance = apps.get_model("openstack", "Instance")
    Instance.objects.filter(runtime_state="RESCUE").update(runtime_state="RESCUED")


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0071_hypervisor_inventory"),
    ]

    operations = [
        migrations.AddField(
            model_name="image",
            name="hw_rescue_device",
            field=models.CharField(
                blank=True,
                help_text="Glance hw_rescue_device property (cdrom/disk/floppy).",
                max_length=20,
            ),
        ),
        migrations.AddField(
            model_name="image",
            name="hw_rescue_bus",
            field=models.CharField(
                blank=True,
                help_text="Glance hw_rescue_bus property (scsi/virtio/ide/usb).",
                max_length=20,
            ),
        ),
        migrations.RunPython(
            fix_rescued_runtime_state, reverse_fix_rescued_runtime_state
        ),
    ]
