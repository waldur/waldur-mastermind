from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ('invoices', '0042_update_invoice_items_resource_name'),
    ]

    operations = [
        # Raw SQL is used instead of Django migration operations
        # because packages application has been removed
        migrations.RunSQL(
            'ALTER TABLE invoices_servicedowntime DROP COLUMN IF EXISTS package_id'
        ),
    ]
