from django.db import migrations
from django.utils.timezone import now

# 0266 archived the SlurmInvoices.SlurmPackage offerings, which stops new orders.
# It does not resolve the resources already provisioned against them: those keep
# their current state with no delete processor behind them, so a terminate order
# is accepted and then fails in process_order() with "Skipping order processing
# because processor is not found.", leaving the resource ERRED. They can never
# reach TERMINATED through the normal flow.
#
# This migration retires them directly. Data migrations run against historical
# models, so none of the post_save handlers that normally accompany termination
# fire -- the work they would have done is replicated explicitly below.
#
# The plan-period close is the part that matters beyond bookkeeping: an open
# ResourcePlanPeriod keeps a fixed-price resource accruing invoice charges for a
# backend that no longer exists.

SLURM_OFFERING = "SlurmInvoices.SlurmPackage"

RESOURCE_TERMINATED = 6

ORDER_TERMINATE = 3
ORDER_DONE = 3
ORDER_ERRED = 4

# pending-consumer, executing, pending-provider, pending-project, pending-start-date
ORDER_NON_TERMINAL = [1, 2, 7, 8, 9]


def terminate_slurm_resources(apps, schema_editor):
    Resource = apps.get_model("marketplace", "Resource")
    ResourcePlanPeriod = apps.get_model("marketplace", "ResourcePlanPeriod")
    Order = apps.get_model("marketplace", "Order")

    resource_ids = list(
        Resource.objects.filter(offering__type=SLURM_OFFERING)
        .exclude(state=RESOURCE_TERMINATED)
        .values_list("id", flat=True)
    )
    if not resource_ids:
        return

    # Stop billing accrual first: close_resource_plan_period_when_resource_is_terminated
    # would normally do this off the state change.
    closed = ResourcePlanPeriod.objects.filter(
        resource_id__in=resource_ids, end__isnull=True
    ).update(end=now())

    # A terminate order is now satisfied -- the resource is being retired here.
    terminate_orders = Order.objects.filter(
        resource_id__in=resource_ids,
        type=ORDER_TERMINATE,
        state__in=ORDER_NON_TERMINAL,
    ).update(state=ORDER_DONE)

    # Anything else still in flight can never complete: the plugin is gone.
    other_orders = Order.objects.filter(
        resource_id__in=resource_ids, state__in=ORDER_NON_TERMINAL
    ).update(
        state=ORDER_ERRED,
        error_message=f"The {SLURM_OFFERING} plugin has been removed, "
        "so this order can no longer be processed.",
    )

    Resource.objects.filter(id__in=resource_ids).update(state=RESOURCE_TERMINATED)

    print(
        f"Terminated {len(resource_ids)} {SLURM_OFFERING} resource(s): "
        f"closed {closed} open plan period(s), "
        f"completed {terminate_orders} terminate order(s), "
        f"erred {other_orders} other in-flight order(s)."
    )


class Migration(migrations.Migration):
    dependencies = [
        ("marketplace", "0266_archive_slurm_offerings"),
    ]

    operations = [
        # Reverse is a noop: the prior per-resource states are not recorded, and
        # restoring them would only recreate resources nothing can manage.
        migrations.RunPython(terminate_slurm_resources, migrations.RunPython.noop),
    ]
