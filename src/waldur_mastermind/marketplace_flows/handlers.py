from django.db import transaction

from waldur_mastermind.marketplace.tasks import process_order_on_commit

from . import models, tasks


def process_flow_state_change(sender, instance, created=False, **kwargs):
    if created:
        return

    flow: models.FlowTracker = instance

    if not flow.tracker.has_changed("state"):
        return

    if flow.state != models.FlowTracker.States.PENDING:
        transaction.on_commit(lambda: tasks.send_mail_for_submitted_flow.delay(flow.id))
    elif flow.state == models.FlowTracker.States.APPROVED:
        flow.order.set_state_executing()
        flow.order.save()
        process_order_on_commit(flow.order, flow.requested_by)
    elif flow.state == models.FlowTracker.States.REJECTED:
        transaction.on_commit(lambda: tasks.send_mail_for_rejected_flow.delay(flow.id))


def approve_reject_offering_state_request_when_related_issue_is_resolved(
    sender, instance, created=False, **kwargs
):
    if created:
        return

    issue = instance

    if not issue.tracker.has_changed("status"):
        return

    try:
        offering_request = models.OfferingStateRequest.objects.get(issue_id=issue.id)

        user = issue.assignee.user if issue.assignee else None

        if issue.resolved is None:
            return
        elif issue.resolved:
            offering_request.approve(user)
        else:
            offering_request.reject(user)

    except models.OfferingStateRequest.DoesNotExist:
        pass
