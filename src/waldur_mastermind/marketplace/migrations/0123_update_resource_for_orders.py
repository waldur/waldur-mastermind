from django.db import migrations


class ResourceStates:
    CREATING = 1
    OK = 2
    ERRED = 3
    UPDATING = 4
    TERMINATING = 5
    TERMINATED = 6


class OrderStates:
    PENDING_CONSUMER = 1
    PENDING_PROVIDER = 7
    EXECUTING = 2
    DONE = 3
    ERRED = 4
    CANCELED = 5
    REJECTED = 6


class OrderTypes:
    CREATE = 1
    UPDATE = 2
    TERMINATE = 3


def update_resource_for_orders(apps, schema_editor):
    Resource = apps.get_model("marketplace", "Resource")
    Order = apps.get_model("marketplace", "Order")

    creating_resources = Resource.objects.filter(state=ResourceStates.CREATING)

    for resource in creating_resources:
        creation_order = Order.objects.filter(
            resource=resource, type=OrderTypes.CREATE
        ).first()
        if creation_order.state != OrderStates.ERRED:
            continue

        if resource.backend_id in [None, ""]:
            resource.state = ResourceStates.TERMINATED
        else:
            resource.state = ResourceStates.ERRED

        resource.save(update_fields=["state"])


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0122_add_missing_plan_periods"),
    ]

    operations = [
        migrations.RunPython(update_resource_for_orders),
    ]
