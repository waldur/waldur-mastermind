from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0213_alter_resource_current_usages"),
    ]

    operations = [
        migrations.RemoveIndex(
            model_name="softwarepackage",
            name="marketplace_parent__4bb7b4_idx",
        ),
        migrations.AlterField(
            model_name="softwarepackage",
            name="parent_software",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=models.deletion.CASCADE,
                related_name="extensions_old",
                to="marketplace.softwarepackage",
            ),
        ),
        migrations.AddField(
            model_name="softwarepackage",
            name="parent_softwares",
            field=models.ManyToManyField(
                blank=True,
                help_text="Parent packages for extensions (e.g., Python package within Python)",
                related_name="extensions",
                to="marketplace.softwarepackage",
            ),
        ),
    ]
