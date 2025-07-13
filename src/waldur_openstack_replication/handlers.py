from waldur_core.core.enums import CoreStates
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.models import Order
from waldur_openstack_replication.models import Migration


def handle_migration_post_save(sender, instance: Migration, created, **kwargs):
    """Handle migration post-save events."""
    migration = instance
    if created:
        return
    if not migration.tracker.has_changed("state"):
        return
    if migration.state not in (CoreStates.OK, CoreStates.ERRED):
        return
    Order.objects.create(
        created=migration.created,
        created_by=migration.created_by,
        resource=migration.dst_resource,
        offering=migration.dst_resource.offering,
        project=migration.dst_resource.project,
        limits=migration.dst_resource.limits,
        state=migration.state == CoreStates.OK
        and OrderStates.DONE
        or OrderStates.ERRED,
        consumer_reviewed_by=migration.created_by,
        provider_reviewed_by=migration.created_by,
        consumer_reviewed_at=migration.created,
        provider_reviewed_at=migration.created,
        error_message=migration.error_message,
        error_traceback=migration.error_traceback,
    )
