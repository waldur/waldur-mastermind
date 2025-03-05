from django.db import migrations


def fill_invoice_item_compensation_details(apps, schema_editor):
    InvoiceItem = apps.get_model("invoices", "InvoiceItem")
    compensations = InvoiceItem.objects.exclude(credit=None)
    for compensation in compensations:
        invoice_item_name = compensation.name.split("Credit compensation. ")[1]
        invoice_item = InvoiceItem.objects.filter(name=invoice_item_name).first()
        if invoice_item:
            compensation.details = invoice_item.details
            compensation.save()


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0012_invoice_events"),
    ]

    operations = [migrations.RunPython(fill_invoice_item_compensation_details)]
