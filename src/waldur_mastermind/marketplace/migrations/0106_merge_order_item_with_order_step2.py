from django.db import migrations


def fill_data(apps, schema_editor):
    OrderItem = apps.get_model('marketplace', 'OrderItem')
    for order_item in OrderItem.objects.all():
        order = order_item.order
        # because remote plugin assumes so
        if OrderItem.objects.filter(order=order).count() == 1:
            order_item.uuid = order.uuid
        order_item.project = order.project
        order_item.created_by = order.created_by
        order_item.approved_by = order.approved_by
        order_item.approved_at = order.approved_at
        # TERMINATED (4) -> CANCELED (5)
        if order.state == 4:
            order_item.state = 4
        # REJECTED (new state)
        if order.state == 6:
            order_item.state = 6
        order_item.save(
            update_fields=[
                'uuid',
                'project',
                'created_by',
                'approved_by',
                'approved_at',
                'state',
            ]
        )


class Migration(migrations.Migration):
    dependencies = [
        ('marketplace', '0105_merge_order_item_with_order_step1'),
    ]

    operations = [
        migrations.RunPython(fill_data, elidable=True),
    ]
