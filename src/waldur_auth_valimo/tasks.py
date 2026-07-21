import logging
from datetime import timedelta

from celery import shared_task
from django.utils import timezone

from waldur_core.core import tasks
from waldur_core.core.authentication import refresh_token
from waldur_core.core.models import User

from . import client, models

logger = logging.getLogger(__name__)


class AuthTask(tasks.StateTransitionTask):
    """Execute request for authentication"""

    def execute(self, auth_result):
        response = client.SignatureRequest.execute(
            transaction_id=auth_result.uuid.hex,
            phone=auth_result.phone,
            message=auth_result.message,
        )
        auth_result.backend_transaction_id = response.backend_transaction_id
        auth_result.save(update_fields=["backend_transaction_id"])


class PollTask(tasks.Task):
    max_retries = 25
    default_retry_delay = 12

    def execute(self, auth_result):
        response = client.StatusRequest.execute(
            transaction_id=auth_result.uuid.hex,
            backend_transaction_id=auth_result.backend_transaction_id,
        )
        if response.status == client.Statuses.OK:
            self._associate_with_user(auth_result, response.civil_number)
        elif response.status == client.Statuses.PROCESSING:
            self.retry()
        elif response.status == client.Statuses.ERRED:
            auth_result.set_canceled()
            auth_result.details = response.details
            auth_result.save(update_fields=["state", "details"])
            logger.info(
                "PKI login failed for auth result %s, details: %s.",
                auth_result.uuid.hex,
                auth_result.details,
            )
            logger.debug(
                "PKI login failure phone for auth result %s: %s",
                auth_result.uuid.hex,
                auth_result.phone,
            )

    def _associate_with_user(self, auth_result, civil_number):
        try:
            user = User.objects.get(civil_number=civil_number)
            refresh_token(user)
            auth_result.user = user
            auth_result.set_ok()
            user.last_login = timezone.now()
            user.save(update_fields=["last_login"])
            logger.info("PKI login was successfully done for user %s.", user.username)
        except User.DoesNotExist:
            auth_result.details = "User is not registered."
            auth_result.set_canceled()
            logger.info(
                "PKI login failed for auth result %s - user record does not exist in Waldur.",
                auth_result.uuid.hex,
            )
            logger.debug(
                "PKI login failure civil number for auth result %s: %s",
                auth_result.uuid.hex,
                civil_number,
            )
        auth_result.save()


@shared_task(name="waldur_auth_valimo.cleanup_auth_results")
def cleanup_auth_results():
    """Clean up Valimo authentication results older than 7 days."""
    models.AuthResult.objects.filter(
        modified__lte=timezone.now() - timedelta(days=7)
    ).delete()
