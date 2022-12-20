from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0031_rename_invoice_item_model'),
    ]

    operations = [
        migrations.AddField(
            model_name='InvoiceItem',
            name='name',
            field=models.TextField(default=''),
        ),
    ]
