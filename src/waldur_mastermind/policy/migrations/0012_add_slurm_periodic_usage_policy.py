# Generated manually for SLURM periodic usage policy

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("policy", "0011_customercomponentusagepolicy_and_more"),
        (
            "marketplace",
            "0195_offeringtermsofservice_grace_period_days",
        ),  # Latest marketplace migration
    ]

    operations = [
        migrations.CreateModel(
            name="SlurmPeriodicUsagePolicy",
            fields=[
                (
                    "offeringusagepolicy_ptr",
                    models.OneToOneField(
                        auto_created=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        parent_link=True,
                        primary_key=True,
                        serialize=False,
                        to="policy.offeringusagepolicy",
                    ),
                ),
                (
                    "limit_type",
                    models.CharField(
                        choices=[
                            ("GrpTRESMins", "Group TRES Minutes"),
                            ("MaxTRESMins", "Max TRES Minutes"),
                            ("GrpTRES", "Group TRES (concurrent)"),
                        ],
                        default="GrpTRESMins",
                        help_text="SLURM limit type to apply",
                        max_length=20,
                    ),
                ),
                (
                    "tres_billing_enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Use TRES billing units instead of raw TRES values",
                    ),
                ),
                (
                    "tres_billing_weights",
                    models.JSONField(
                        blank=True,
                        default=dict,
                        help_text='TRES billing weights (e.g., {"CPU": 0.015625, "Mem": 0.001953125, "GRES/gpu": 0.25})',
                    ),
                ),
                (
                    "fairshare_decay_half_life",
                    models.PositiveIntegerField(
                        default=15,
                        help_text="Fairshare decay half-life in days (matches SLURM PriorityDecayHalfLife)",
                    ),
                ),
                (
                    "grace_ratio",
                    models.FloatField(
                        default=0.2,
                        help_text="Grace period ratio (0.2 = 20% overconsumption allowed)",
                    ),
                ),
                (
                    "carryover_enabled",
                    models.BooleanField(
                        default=True,
                        help_text="Enable unused allocation carryover to next period",
                    ),
                ),
                (
                    "raw_usage_reset",
                    models.BooleanField(
                        default=True,
                        help_text="Reset raw usage at period transitions (PriorityUsageResetPeriod=None)",
                    ),
                ),
                (
                    "qos_strategy",
                    models.CharField(
                        choices=[
                            ("threshold", "Threshold-based (single threshold)"),
                            ("progressive", "Progressive (multiple thresholds)"),
                        ],
                        default="threshold",
                        help_text="QoS management strategy",
                        max_length=20,
                    ),
                ),
            ],
            options={
                "verbose_name": "SLURM Periodic Usage Policy",
                "verbose_name_plural": "SLURM Periodic Usage Policies",
            },
            bases=("policy.offeringusagepolicy",),
        ),
    ]
