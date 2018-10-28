from __future__ import unicode_literals

import logging

from celery import shared_task
from django.conf import settings

from waldur_core.core import utils as core_utils
from waldur_core.core.utils import broadcast_mail
from waldur_core.structure import models as structure_models
from waldur_mastermind.support.tasks import _send_issue_notification

from . import models

logger = logging.getLogger(__name__)


def get_request_customer_link(request, customer):
    return settings.WALDUR_EXPERTS['REQUEST_CUSTOMER_LINK_TEMPLATE'].format(
        request_uuid=request.uuid.hex,
        customer_uuid=customer.uuid.hex
    )


def get_request_project_link(request):
    return settings.WALDUR_EXPERTS['REQUEST_PROJECT_LINK_TEMPLATE'].format(
        request_uuid=request.uuid.hex,
        project_uuid=request.project.uuid.hex
    )


@shared_task(name='waldur_mastermind.experts.send_new_request')
def send_new_request(request_uuid):
    """
    Send email notification about new expert request.
    """

    request = models.ExpertRequest.objects.get(uuid=request_uuid)

    enabled_providers = models.ExpertProvider.objects.filter(enable_notifications=True)
    customers = list(enabled_providers.values_list('customer', flat=True))
    customers = structure_models.Customer.objects.filter(pk__in=customers)
    for customer in customers:
        users = customer.get_owners()
        extra_context = {
            'request_link': get_request_customer_link(request, customer)
        }
        send_request_mail('new_request', request, users, extra_context)


@shared_task(name='waldur_mastermind.experts.send_new_bid')
def send_new_bid(bid_uuid):
    """
    Send email notification about new bid.
    """

    bid = models.ExpertBid.objects.get(uuid=bid_uuid)
    users = bid.request.customer.get_owners()
    extra_context = {
        'bid': bid,
        'request_link': get_request_project_link(bid.request)
    }
    send_request_mail('new_bid', bid.request, users, extra_context)


@shared_task(name='waldur_mastermind.experts.send_new_contract')
def send_new_contract(request_uuid):
    """
    Send email notification about accepted expert request.
    """

    request = models.ExpertRequest.objects.get(uuid=request_uuid)
    users = request.customer.get_owners()
    extra_context = {
        'request_link': get_request_project_link(request)
    }
    send_request_mail('contract', request, users, extra_context)


def send_request_mail(event_type, request, users, extra_context=None):
    """
    Shorthand to send email notification about expert request event.
    """

    recipient_list = list(users.exclude(email='').values_list('email', flat=True))

    context = dict(
        request=request,
        customer_name=request.project.customer.name,
        project_name=request.project.name,
        currency_name=settings.WALDUR_CORE['CURRENCY_NAME'],
        site_name=settings.WALDUR_CORE['SITE_NAME'],
    )
    if extra_context:
        context.update(extra_context)

    logger.debug('About to send expert request {request_name} to {recipient_list}.'.format(
        request_name=request.name,
        recipient_list=', '.join(recipient_list)
    ))

    broadcast_mail('experts', event_type, context, recipient_list)


@shared_task
def create_pdf_contract(contract_id):
    contract = models.ExpertContract.objects.get(pk=contract_id)
    contract.create_file()


@shared_task(name='waldur_mastermind.experts.send_expert_comment_added_notification')
def send_expert_comment_added_notification(serialized_comment):
    # Send Expert notifications
    comment = core_utils.deserialize_instance(serialized_comment)
    expert_request = comment.issue.expertrequest_set.first()
    expert_contract = getattr(expert_request, 'contract', None)

    if expert_contract:
        experts = expert_contract.team.get_users(structure_models.ProjectRole.ADMINISTRATOR)

        if comment.author.user not in experts:
            for expert in experts:
                _send_issue_notification(comment.issue, 'comment_added', receiver=expert)
