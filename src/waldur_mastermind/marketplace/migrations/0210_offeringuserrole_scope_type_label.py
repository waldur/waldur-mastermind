from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0209_offeringuserrole_scope_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="offeringuserrole",
            name="scope_type_label",
            field=models.CharField(
                blank=True,
                default="",
                help_text=(
                    "Human-readable label for scope_type shown to end users, "
                    "e.g. 'Rancher Project', 'Cluster Namespace'. "
                    "Falls back to capitalized scope_type if empty."
                ),
                max_length=150,
            ),
        ),
    ]
