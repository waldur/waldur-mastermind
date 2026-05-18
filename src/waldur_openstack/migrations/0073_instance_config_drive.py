"""Per-instance config_drive override [WAL-9946].

Adds a nullable boolean Instance.config_drive that overrides the tenant-wide
service-settings option of the same name. Null means "inherit tenant default";
explicit True/False win over the tenant-wide value at Nova create time.
"""

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("openstack", "0072_rescue_support"),
    ]

    operations = [
        migrations.AddField(
            model_name="instance",
            name="config_drive",
            field=models.BooleanField(
                blank=True,
                default=None,
                help_text=(
                    "Force config drive on or off for this instance. "
                    "If null, the tenant-wide default from service settings is used."
                ),
                null=True,
            ),
        ),
    ]
