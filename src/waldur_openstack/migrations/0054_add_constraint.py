from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0053_remove_duplicates"),
    ]

    operations = [
        migrations.AlterUniqueTogether(
            name="port",
            unique_together={("tenant", "backend_id")},
        ),
        migrations.AlterUniqueTogether(
            name="router",
            unique_together={("tenant", "backend_id")},
        ),
    ]
