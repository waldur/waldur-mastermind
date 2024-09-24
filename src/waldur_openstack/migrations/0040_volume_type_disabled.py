from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0039_remove_old_instance"),
    ]

    operations = [
        migrations.AddField(
            model_name="volumetype",
            name="disabled",
            field=models.BooleanField(default=False),
        ),
    ]
