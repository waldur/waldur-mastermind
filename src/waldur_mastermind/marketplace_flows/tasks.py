from celery import shared_task

from waldur_core.core.utils import broadcast_mail

from . import models


@shared_task
def send_mail_for_submitted_flow(flow_id):
    flow = models.FlowTracker.objects.get(id=flow_id)
    recipient_list = [
        user.email
        for user in flow.resource_create_request.offering.customer.get_owners()
    ]
    broadcast_mail(
        'marketplace_flows', 'flow_submitted', {'flow': flow}, recipient_list
    )


@shared_task
def send_mail_for_rejected_flow(flow_id):
    flow = models.FlowTracker.objects.get(id=flow_id)
    broadcast_mail(
        'marketplace_flows', 'flow_rejected', {'flow': flow}, [flow.requested_by.email]
    )
