from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [('marketplace', '0059_offering_image')]

    operations = [
        migrations.AlterUniqueTogether(
            name='offeringuser',
            unique_together={('offering', 'user')},
        ),
    ]
