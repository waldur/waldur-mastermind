"""
See also: https://stackoverflow.com/questions/4448340/postgresql-duplicate-key-violates-unique-constraint
"""

from django.db import migrations

OPENSTACK_TABLES = (
    "openstack_backup",
    "openstack_backuprestoration",
    "openstack_backupschedule",
    "openstack_flavor",
    "openstack_image",
    "openstack_instance",
    "openstack_instanceavailabilityzone",
    "openstack_securitygroup",
    "openstack_servergroup",
    "openstack_snapshot",
    "openstack_snapshotrestoration",
    "openstack_snapshotschedule",
    "openstack_volume",
    "openstack_volumeavailabilityzone",
    "openstack_volumetype",
)


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0040_volume_type_disabled"),
    ]

    operations = [
        migrations.RunSQL(
            sql=[
                f"SELECT setval(pg_get_serial_sequence('{table}', 'id'), (SELECT MAX(id) FROM {table}) + 1);"
                for table in OPENSTACK_TABLES
            ]
        ),
    ]
