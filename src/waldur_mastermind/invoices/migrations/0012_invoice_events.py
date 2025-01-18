from django.core.exceptions import ObjectDoesNotExist
from django.db import migrations


def index_invoice_events(apps, schema_editor):
    Event = apps.get_model("logging", "Event")
    Feed = apps.get_model("logging", "Feed")
    ContentType = apps.get_model("contenttypes", "ContentType")
    Invoice = apps.get_model("invoices", "Invoice")
    ctype = ContentType.objects.get_for_model(Invoice)

    for event in Event.objects.filter(
        event_type__in=(
            "invoice_created",
            "invoice_paid",
            "invoice_canceled",
            "payment_created",
            "payment_removed",
        )
    ):
        year = event.context.get("year")
        month = event.context.get("month")
        if not year or month:
            continue
        try:
            invoice = Invoice.objects.get(
                customer__uuid=event.context["customer_uuid"],
                year=year,
                month=month,
            )
        except ObjectDoesNotExist:
            continue
        Feed.objects.create(content_type=ctype, object_id=invoice.id, event=event)


class Migration(migrations.Migration):
    dependencies = [
        ("invoices", "0011_projectcredit_minimal_consumption"),
        ("logging", "0015_event_index"),
    ]

    operations = [migrations.RunPython(index_invoice_events)]
