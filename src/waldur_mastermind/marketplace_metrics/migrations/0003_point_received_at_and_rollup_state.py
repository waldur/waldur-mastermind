import datetime

import django.contrib.postgres.indexes
import django.db.models.functions.datetime
import django.utils.timezone
from django.db import migrations, models

HELP_TEXT = (
    "When the point was stored, or last corrected: the roll-up recomputes "
    "the buckets of points received since its previous run."
)


class Migration(migrations.Migration):
    """received_at and the roll-up state; the index on it comes next.

    Every step here is a catalogue change on PostgreSQL 11+, so the lock on
    the point table is brief. Existing points are received at the epoch: the
    first run, finding no previous one, recomputes the whole window anyway,
    and the second then starts from it rather than from every point.
    """

    dependencies = [
        ("marketplace_metrics", "0002_rollups_and_goals"),
    ]

    operations = [
        migrations.CreateModel(
            name="RollupState",
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
                ("rolled_up_until", models.DateTimeField(blank=True, null=True)),
            ],
        ),
        migrations.AddField(
            model_name="metricpoint",
            name="received_at",
            field=models.DateTimeField(
                db_default=datetime.datetime(1970, 1, 1, tzinfo=datetime.UTC),
                help_text=HELP_TEXT,
            ),
        ),
        migrations.AlterField(
            model_name="metricpoint",
            name="received_at",
            field=models.DateTimeField(
                db_default=django.db.models.functions.datetime.Now(),
                default=django.utils.timezone.now,
                help_text=HELP_TEXT,
            ),
        ),
        # A storage parameter of the existing index: no rebuild.
        migrations.SeparateDatabaseAndState(
            database_operations=[
                migrations.RunSQL(
                    "ALTER INDEX metric_point_ts_brin SET (autosummarize = on)",
                    "ALTER INDEX metric_point_ts_brin RESET (autosummarize)",
                ),
            ],
            state_operations=[
                migrations.RemoveIndex(
                    model_name="metricpoint", name="metric_point_ts_brin"
                ),
                migrations.AddIndex(
                    model_name="metricpoint",
                    index=django.contrib.postgres.indexes.BrinIndex(
                        autosummarize=True,
                        fields=["timestamp"],
                        name="metric_point_ts_brin",
                    ),
                ),
            ],
        ),
    ]
