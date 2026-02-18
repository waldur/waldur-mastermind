from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0208_order_rejection_comments"),
    ]

    operations = [
        migrations.AddField(
            model_name="offeringuserrole",
            name="scope_type",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Level this role applies at, e.g. 'cluster', 'project'. "
                    "Empty means offering-wide."
                ),
                max_length=50,
            ),
        ),
    ]
