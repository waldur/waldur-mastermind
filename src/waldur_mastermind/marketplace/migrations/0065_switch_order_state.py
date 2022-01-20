from django.db import migrations


class OrderStates:
    REQUESTED_FOR_APPROVAL = 1
    EXECUTING = 2
    DONE = 3
    TERMINATED = 4
    ERRED = 5
    REJECTED = 6


class OrderItemStates:
    PENDING = 1
    EXECUTING = 2
    DONE = 3
    ERRED = 4
    TERMINATED = 5
    TERMINATING = 6


def switch_order_state(apps, schema_editor):
    OrderItem = apps.get_model('marketplace', 'OrderItem')
    for item in OrderItem.objects.filter(
        state=OrderItemStates.DONE, order__state=OrderStates.REQUESTED_FOR_APPROVAL
    ):
        item.order.state = OrderStates.DONE
        item.order.save()


class Migration(migrations.Migration):

    dependencies = [
        ('marketplace', '0064_add_access_url_to_offering'),
    ]

    operations = [
        migrations.RunPython(switch_order_state),
    ]
