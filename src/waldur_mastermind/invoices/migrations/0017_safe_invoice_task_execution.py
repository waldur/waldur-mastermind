from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0016_alter_invoiceitem_unit"),
        (
            "structure",
            "0056_customer_project_metadata_checklist",
        ),
    ]

    operations = []
