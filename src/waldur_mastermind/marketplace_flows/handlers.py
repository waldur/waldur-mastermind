from django.db import transaction

from waldur_core.core import utils as core_utils
from waldur_mastermind.marketplace.tasks import process_order

from . import models, tasks


def process_flow_state_change(sender, instance, created=False, **kwargs):
    if created:
        return

    flow = instance

    if not flow.tracker.has_changed('state'):
        return

    if flow.state != models.FlowTracker.States.PENDING:
        transaction.on_commit(lambda: tasks.send_mail_for_submitted_flow.delay(flow.id))
    elif flow.state == models.FlowTracker.States.APPROVED:
        serialized_order = core_utils.serialize_instance(flow.order_item.order)
        serialized_user = core_utils.serialize_instance(flow.requested_by)
        transaction.on_commit(
            lambda: process_order.delay(serialized_order, serialized_user)
        )
    elif flow.state == models.FlowTracker.States.REJECTED:
        transaction.on_commit(lambda: tasks.send_mail_for_rejected_flow.delay(flow.id))
