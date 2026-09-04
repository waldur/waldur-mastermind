from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0281_rerun_data_migrations_skipped_by_squashes"),
    ]

    operations = [
        migrations.AddField(
            model_name="plan",
            name="billing_mode",
            field=models.CharField(
                choices=[
                    ("inherit", "Inherit from components"),
                    ("limit", "Limit-based"),
                    ("usage", "Usage-based"),
                ],
                default="inherit",
                help_text=(
                    "Overrides how the offering's builtin components are billed under "
                    "this plan. Custom components keep their own accounting type."
                ),
                max_length=10,
            ),
        ),
    ]
