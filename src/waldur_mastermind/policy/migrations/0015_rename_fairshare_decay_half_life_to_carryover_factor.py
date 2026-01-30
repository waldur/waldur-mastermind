from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("policy", "0014_slurmpolicyevaluationlog_slurmcommandhistory"),
    ]

    operations = [
        migrations.RemoveField(
            model_name="slurmperiodicusagepolicy",
            name="fairshare_decay_half_life",
        ),
        migrations.AddField(
            model_name="slurmperiodicusagepolicy",
            name="carryover_factor",
            field=models.PositiveIntegerField(
                default=50,
                help_text="Maximum percentage of base allocation that can carry over from unused previous period (0-100)",
            ),
        ),
    ]
