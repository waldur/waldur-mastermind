import logging

from celery import shared_task

from waldur_core.core import utils as core_utils
from waldur_core.core.log import event_logger
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure import models as structure_models
from waldur_mastermind.policy import models

from . import utils

logger = logging.getLogger(__name__)


def send_emails(emails, policy):
    scope_class = policy.scope.__class__.__name__

    if emails:
        context = {
            "scope_class": scope_class,
            "scope_name": policy.scope.name,
            "scope_url": policy.get_scope_homeport_url(),
            "limit": policy.limit_cost,
        }
        core_utils.broadcast_mail(
            "marketplace_policy",
            "notification_about_project_cost_exceeded_limit",
            context,
            emails,
        )

    event_logger.info(
        "Cost policy has been triggered and emails have been sent.",
        event_type="policy_notification",
        event_context={
            "policy_uuid": policy.uuid.hex,
            "scope": f"{scope_class} UUID: {policy.scope.uuid.hex}",
            "emails": str(emails),
        },
        group="policy_notification",
    )


@shared_task(name="waldur_mastermind.policy.notify_project_team")
def notify_project_team(serialized_policy):
    policy = core_utils.deserialize_instance(serialized_policy)
    if not isinstance(policy.scope, structure_models.Project):
        return
    emails = policy.scope.get_user_mails()
    send_emails(emails, policy)


@shared_task(name="waldur_mastermind.policy.notify_customer_team")
def notify_customer_owners(serialized_policy):
    policy = core_utils.deserialize_instance(serialized_policy)
    if not isinstance(policy.scope, structure_models.Customer):
        return
    emails = policy.scope.get_user_mails(RoleEnum.CUSTOMER_OWNER)
    send_emails(emails, policy)


@shared_task(name="waldur_mastermind.policy.check_polices")
def check_polices():
    """Evaluate all policies across all policy types in the system."""
    for klass in core_utils.get_all_subclasses(models.Policy):
        if klass._meta.abstract:
            continue

        utils.evaluate_policies(klass.objects.all())


@shared_task(name="waldur_mastermind.policy.notify_external_user")
def notify_external_user(serialized_policy):
    policy = core_utils.deserialize_instance(serialized_policy)
    emails = policy.options.get("notify_external_user", "").split(",")
    send_emails(emails, policy)
