from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0010_error_traceback'),
    ]

    operations = [
        migrations.RemoveField(
            model_name='offeringplan',
            name='template',
        ),
        migrations.DeleteModel(
            name='Offering',
        ),
        migrations.DeleteModel(
            name='OfferingPlan',
        ),
        migrations.DeleteModel(
            name='OfferingTemplate',
        ),
    ]
