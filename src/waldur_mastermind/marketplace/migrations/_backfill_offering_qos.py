"""Backfill logic for migration 0254, kept importable so it can be tested
directly against the live app registry (django_test_migrations is not
available), mirroring _enable_posix_account_backfill.
"""


def backfill_qos(apps, schema_editor):
    """Turn each partition's single ``qos`` string into an OfferingQoS + a
    default PartitionQoS allow-list entry, so existing SLURM offerings keep
    their behaviour under the new model."""
    OfferingPartition = apps.get_model("marketplace", "OfferingPartition")
    OfferingQoS = apps.get_model("marketplace", "SlurmOfferingQoS")
    PartitionQoS = apps.get_model("marketplace", "SlurmPartitionQoS")

    for partition in OfferingPartition.objects.exclude(qos="").iterator():
        qos, _ = OfferingQoS.objects.get_or_create(
            offering_id=partition.offering_id, name=partition.qos
        )
        PartitionQoS.objects.get_or_create(
            partition=partition, qos=qos, defaults={"is_default": True}
        )
