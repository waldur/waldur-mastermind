import logging

from celery import shared_task
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.permissions import _get_customer, _get_project
from waldur_mastermind.policy import log, models

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

    log.event_logger.policy_notification.info(
        "Cost policy has been triggered and emails have been sent.",
        event_type="policy_notification",
        event_context={
            "policy_uuid": policy.uuid.hex,
            "scope": f"{scope_class} UUID: {policy.scope.uuid.hex}",
            "emails": str(emails),
        },
    )


@shared_task(name="waldur_mastermind.policy.notify_project_team")
def notify_project_team(serialized_policy):
    policy = core_utils.deserialize_instance(serialized_policy)
    project = _get_project(policy.scope)
    emails = project.get_user_mails()
    send_emails(emails, policy)


@shared_task(name="waldur_mastermind.policy.notify_customer_team")
def notify_customer_owners(serialized_policy):
    policy = core_utils.deserialize_instance(serialized_policy)
    customer = _get_customer(policy.scope)
    emails = customer.get_user_mails(RoleEnum.CUSTOMER_OWNER)
    send_emails(emails, policy)


@shared_task(name="waldur_mastermind.policy.check_polices")
def check_polices():
    for klass in core_utils.get_all_subclasses(models.Policy):
        if klass._meta.abstract:
            continue

        for policy in klass.objects.all():
            if policy.is_triggered():
                if policy.has_fired:
                    continue
                else:
                    policy.has_fired = True
                    policy.fired_datetime = timezone.now()
                    policy.save()
                    logger.info(
                        "A policy %s has fired.",
                        policy.uuid.hex,
                    )

                    for action in policy.get_one_time_actions():
                        action(policy)
                        logger.info(
                            "%s action of policy %s has been triggered.",
                            action.__name__,
                            policy.uuid.hex,
                        )
            else:
                if not policy.has_fired:
                    continue
                else:
                    policy.has_fired = False
                    policy.fired_datetime = timezone.now()
                    policy.save()
                    logger.info(
                        "A policy %s has not fired.",
                        policy.uuid.hex,
                    )

                    for action in policy.get_not_one_time_actions():
                        reset_action = getattr(action, "reset", None)
                        if reset_action:
                            action.reset(policy)

            return policy
