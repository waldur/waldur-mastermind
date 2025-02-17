from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0045_floatingip_external_address"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="snapshot",
            name="snapshot_schedule",
        ),
        migrations.RemoveField(
            model_name="backup",
            name="backup_schedule",
        ),
        migrations.DeleteModel(
            name="SnapshotSchedule",
        ),
        migrations.DeleteModel(
            name="BackupSchedule",
        ),
    ]
