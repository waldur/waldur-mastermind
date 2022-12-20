import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('invoices', '0043_drop_package_column'),
    ]

    operations = [
        migrations.AddField(
            model_name='invoiceitem',
            name='resource',
            field=models.ForeignKey(
                null=True,
                on_delete=django.db.models.deletion.PROTECT,
                related_name='invoice_items',
                to='marketplace.Resource',
            ),
        ),
        migrations.RemoveField(
            model_name='invoiceitem',
            name='content_type',
        ),
        migrations.RemoveField(
            model_name='invoiceitem',
            name='object_id',
        ),
    ]
