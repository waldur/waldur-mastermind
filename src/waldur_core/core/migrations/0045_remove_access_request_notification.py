from django.db import migrations

from waldur_core.core.migrations._remove_access_request_notification import (
    remove_access_request_notification,
)


class Migration(migrations.Migration):
    dependencies = [
        ("core", "0044_alter_dailytablesizehistory_options"),
    ]

    operations = [
        migrations.RunPython(
            remove_access_request_notification, migrations.RunPython.noop
        ),
    ]
