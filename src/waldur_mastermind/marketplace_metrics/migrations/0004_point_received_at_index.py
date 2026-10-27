import django.contrib.postgres.indexes
from django.contrib.postgres.operations import AddIndexConcurrently
from django.db import migrations


class Migration(migrations.Migration):
    """Built without blocking ingest and reads: the point table is large."""

    atomic = False

    dependencies = [
        ("marketplace_metrics", "0003_point_received_at_and_rollup_state"),
    ]

    operations = [
        AddIndexConcurrently(
            model_name="metricpoint",
            index=django.contrib.postgres.indexes.BrinIndex(
                autosummarize=True,
                fields=["received_at"],
                name="metric_point_received_brin",
            ),
        ),
    ]
