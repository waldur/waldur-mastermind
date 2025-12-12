from django.db import migrations

from waldur_mastermind.marketplace.enums import OrderStates, OrderTypes, ResourceStates


def sync_resource_states(apps, schema_editor):
    """
    Synchronize resource states for existing orders to match the new synchronous state transition behavior.

    With the new implementation, resources immediately transition to UPDATING or TERMINATING state
    when UPDATE or TERMINATE orders are created. This migration updates existing resources with
    pending or executing orders to reflect this behavior.
    """
    Order = apps.get_model("marketplace", "Order")
    Resource = apps.get_model("marketplace", "Resource")

    # Find all non-terminal orders
    pending_executing_orders = Order.objects.filter(
        state__in=[
            OrderStates.PENDING_CONSUMER,
            OrderStates.PENDING_PROVIDER,
            OrderStates.EXECUTING,
        ]
    ).select_related("resource")

    resources_to_update = []

    for order in pending_executing_orders:
        if not order.resource:
            continue

        # Skip if resource is in ERRED state
        if order.resource.state == ResourceStates.ERRED:
            continue

        # Determine target state based on order type
        target_state = None
        if (
            order.type == OrderTypes.UPDATE
            and order.resource.state != ResourceStates.UPDATING
        ):
            target_state = ResourceStates.UPDATING
        elif (
            order.type == OrderTypes.TERMINATE
            and order.resource.state != ResourceStates.TERMINATING
        ):
            target_state = ResourceStates.TERMINATING

        if target_state:
            order.resource.state = target_state
            resources_to_update.append(order.resource)

    # Bulk update resources
    if resources_to_update:
        Resource.objects.bulk_update(resources_to_update, ["state"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0199_alter_offering_state"),
    ]

    operations = [
        migrations.RunPython(
            sync_resource_states, reverse_code=migrations.RunPython.noop
        ),
    ]
