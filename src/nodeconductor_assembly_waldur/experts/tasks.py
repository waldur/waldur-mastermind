from __future__ import unicode_literals

import logging

from celery import shared_task
from django.conf import settings
from django.core.mail import send_mail
from django.template.loader import render_to_string

from . import models

logger = logging.getLogger(__name__)


@shared_task(name='nodeconductor_assembly_waldur.experts.send_contract')
def send_contract(request_uuid, email):
    request = models.ExpertRequest.objects.get(uuid=request_uuid)

    context = dict(
        request=request,
        customer_name=request.project.customer.name,
        project_name=request.project.name,
    )

    subject = render_to_string('experts/contract_subject.txt', context)
    text_message = render_to_string('experts/contract_message.txt', context)
    html_message = render_to_string('experts/contract_message.html', context)

    logger.debug('About to send expert contract {request_name} to {email}.'.format(
        request_name=request.name, email=email, **context))
    send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, [email], html_message=html_message)
