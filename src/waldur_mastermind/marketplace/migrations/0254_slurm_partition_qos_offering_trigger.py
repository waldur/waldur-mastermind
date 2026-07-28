from django.db import migrations

# DB-level guard: a partition QoS link (SlurmPartitionQoS) must reference a QoS
# and a partition that belong to the *same* offering. This is a cross-table
# invariant, which a Postgres CHECK constraint cannot express (a CHECK may only
# reference columns of the row being checked), so it is enforced with a
# BEFORE INSERT OR UPDATE trigger. The serializer and the model ``clean()``
# guard the same rule at the application layer.
#
# The forward SQL is idempotent (CREATE OR REPLACE + DROP TRIGGER IF EXISTS)
# so it applies cleanly whether or not an earlier revision of migration 0253
# had already created the trigger on a given database.
FORWARD_SQL = """
CREATE OR REPLACE FUNCTION marketplace_slurm_partition_qos_offering_check()
RETURNS TRIGGER AS $$
BEGIN
    IF (
        SELECT offering_id FROM marketplace_offeringpartition
        WHERE id = NEW.partition_id
    ) IS DISTINCT FROM (
        SELECT offering_id FROM marketplace_slurmofferingqos
        WHERE id = NEW.qos_id
    ) THEN
        RAISE EXCEPTION
            'SlurmPartitionQoS: partition and qos must belong to the same offering';
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS marketplace_slurm_partition_qos_offering_check
    ON marketplace_slurmpartitionqos;

CREATE TRIGGER marketplace_slurm_partition_qos_offering_check
    BEFORE INSERT OR UPDATE ON marketplace_slurmpartitionqos
    FOR EACH ROW
    EXECUTE FUNCTION marketplace_slurm_partition_qos_offering_check();
"""

REVERSE_SQL = """
DROP TRIGGER IF EXISTS marketplace_slurm_partition_qos_offering_check
    ON marketplace_slurmpartitionqos;
DROP FUNCTION IF EXISTS marketplace_slurm_partition_qos_offering_check();
"""


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0253_slurmofferingqos_slurmpartitionqos_and_more"),
    ]

    operations = [
        migrations.RunSQL(sql=FORWARD_SQL, reverse_sql=REVERSE_SQL),
    ]
