from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0055_invoice_backend_id'),
    ]

    operations = [
        migrations.AlterField(
            model_name='invoiceitem',
            name='quantity',
            field=models.DecimalField(decimal_places=7, default=0, max_digits=22),
        ),
    ]
