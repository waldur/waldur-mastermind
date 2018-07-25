import logging

from celery import shared_task
from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.tokens import default_token_generator
from django.core.mail import send_mail
from django.template.loader import render_to_string


logger = logging.getLogger(__name__)


@shared_task(name='waldur_auth_social.send_activation_email')
def send_activation_email(user_uuid):
    user = get_user_model().objects.get(uuid=user_uuid, is_active=False)
    token = default_token_generator.make_token(user)
    url_template = settings.WALDUR_AUTH_SOCIAL['USER_ACTIVATION_URL_TEMPLATE']

    url = url_template.format(token=token, user_uuid=user_uuid)
    context = {'activation_url': url}

    subject = render_to_string('waldur_auth_social/activation_email_subject.txt', context).strip()
    text_message = render_to_string('waldur_auth_social/activation_email_message.txt', context)
    html_message = render_to_string('waldur_auth_social/activation_email_message.html', context)

    logger.debug('About to send an activation email to %s' % user.email)
    send_mail(subject, text_message, settings.DEFAULT_FROM_EMAIL, [user.email], html_message=html_message)
