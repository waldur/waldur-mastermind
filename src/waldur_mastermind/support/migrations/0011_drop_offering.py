from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('support', '0010_error_traceback'),
        ('marketplace', '0039_clear_scope_for_support_offering'),
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
