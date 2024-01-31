from django.db import migrations


def fix_issue_order_item(apps, schema_editor):
    ContentType = apps.get_model("contenttypes", "ContentType")
    Order = apps.get_model("marketplace", "Order")
    Issue = apps.get_model("support", "Issue")
    # OrderItem has been renamed to Order
    order_type = ContentType.objects.get_for_model(Order)
    Issue.objects.filter(resource_content_type__model="orderitem").update(
        resource_content_type=order_type
    )


class Migration(migrations.Migration):
    dependencies = [
        ("support", "0004_priority_backend_name"),
        ("marketplace", "0108_rename_orderitem_order"),
        ("contenttypes", "0002_remove_content_type_name"),
    ]

    operations = [
        migrations.RunPython(fix_issue_order_item),
    ]
