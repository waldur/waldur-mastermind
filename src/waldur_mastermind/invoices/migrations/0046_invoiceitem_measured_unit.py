from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0045_invoiceitem_resource_fix'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoiceitem',
            name='measured_unit',
            field=models.CharField(
                blank=True,
                help_text='Unit of measurement, for example, GB.',
                max_length=30,
            ),
        ),
    ]
