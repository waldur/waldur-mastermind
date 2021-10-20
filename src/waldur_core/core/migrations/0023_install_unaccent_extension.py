# Warning! This generation shouldn't be dropped after migration scripts are squashed

from django.contrib.postgres.operations import UnaccentExtension
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0022_long_email'),
    ]

    operations = [UnaccentExtension()]
