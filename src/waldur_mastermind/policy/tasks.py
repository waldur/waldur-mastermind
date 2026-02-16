import logging

from celery import shared_task
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.policy import models

from . import utils

logger = logging.getLogger(__name__)


@shared_task
def evaluate_slurm_resource_policy(
    resource_uuid: str, component_usage_uuid: str = None
):
    """
    Background task to evaluate SlurmPeriodicUsagePolicy for a specific resource.

    This task is queued when ComponentUsage is created/updated to avoid blocking
    the web request with heavy policy evaluation logic.

    Args:
        resource_uuid: UUID of the resource to evaluate
        component_usage_uuid: UUID of the ComponentUsage that triggered evaluation (optional)
    """
    try:
        # Get the resource that triggered the evaluation
        resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)

        logger.info(
            f"Evaluating SLURM policies for resource {resource.uuid} ({resource.name})"
        )

        # Get applicable SLURM policies for this resource's offering
        policies = models.SlurmPeriodicUsagePolicy.objects.filter(
            scope=resource.offering,
        )

        if resource.project.customer.organization_groups.exists():
            # Filter by organization groups if not apply_to_all
            policies = policies.filter(
                Q(apply_to_all=True)
                | Q(
                    organization_groups__in=resource.project.customer.organization_groups.all()
                )
            )
        else:
            policies = policies.filter(apply_to_all=True)

        # Evaluate each policy for this specific resource
        for policy in policies:
            evaluate_resource_against_policy.delay(str(resource.uuid), str(policy.uuid))

        logger.info(
            f"Queued {policies.count()} policy evaluations for resource {resource.uuid}"
        )

    except marketplace_models.Resource.DoesNotExist:
        logger.error(f"Resource {resource_uuid} not found for policy evaluation")
    except Exception as e:
        logger.error(
            f"Error evaluating SLURM policies for resource {resource_uuid}: {e}"
        )


@shared_task
def evaluate_resource_against_policy(resource_uuid: str, policy_uuid: str):
    """
    Evaluate a specific resource against a specific SlurmPeriodicUsagePolicy.

    This performs the actual policy logic evaluation and applies actions
    as needed for individual resource management.
    """
    try:
        resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        policy = models.SlurmPeriodicUsagePolicy.objects.get(uuid=policy_uuid)

        logger.info(f"Evaluating resource {resource.uuid} against policy {policy.uuid}")

        # Calculate usage percentage for this specific resource
        current_period = policy._get_current_period()
        usage_percentage = policy.get_resource_usage_percentage(
            resource, current_period
        )

        logger.info(f"Resource {resource.uuid} usage: {usage_percentage:.1f}%")

        # Capture state before evaluation for logging
        previous_state = {
            "paused": bool(resource.paused),
            "downscaled": bool(resource.downscaled),
        }

        # Determine required actions based on usage percentage
        actions_needed = []

        grace_limit_percentage = (1 + policy.grace_ratio) * 100

        # Check for pausing (highest threshold)
        if (
            usage_percentage >= grace_limit_percentage
            and "request_slurm_resource_pausing" in policy.actions
        ):
            actions_needed.append("pause")

        # Check for downscaling (independent of pausing)
        if (
            usage_percentage >= 100
            and "request_slurm_resource_downscaling" in policy.actions
        ):
            actions_needed.append("downscale")

        # Check for notification (independent of other actions, once per billing period)
        if usage_percentage >= 80 and "notify_organization_owners" in policy.actions:
            already_notified = models.SlurmPolicyEvaluationLog.objects.filter(
                policy=policy,
                resource=resource,
                billing_period=current_period,
                actions_taken__contains="notify",
            ).exists()
            if not already_notified:
                actions_needed.append("notify")

        # Apply or remove actions based on current usage
        with transaction.atomic():
            # Handle pausing
            if "pause" in actions_needed:
                if not resource.paused:
                    resource.paused = True
                    resource.save()
                    logger.info(
                        f"Resource {resource.uuid} paused due to {usage_percentage:.1f}% usage"
                    )
            elif resource.paused and usage_percentage < grace_limit_percentage:
                resource.paused = False
                resource.save()
                logger.info(
                    f"Resource {resource.uuid} pause removed - usage {usage_percentage:.1f}% below grace limit"
                )

            # Handle downscaling
            if "downscale" in actions_needed:
                if not resource.downscaled:
                    resource.downscaled = True
                    resource.save()
                    logger.info(
                        f"Resource {resource.uuid} downscaled due to {usage_percentage:.1f}% usage"
                    )
            elif resource.downscaled and usage_percentage < 100:
                resource.downscaled = False
                resource.save()
                logger.info(
                    f"Resource {resource.uuid} downscaling removed - usage {usage_percentage:.1f}% below threshold"
                )

            # Handle notifications (always send, don't persist state)
            if "notify" in actions_needed:
                notify_about_resource_usage.delay(
                    str(resource.uuid), str(policy.uuid), usage_percentage
                )

        # Capture state after evaluation
        resource.refresh_from_db()
        new_state = {
            "paused": bool(resource.paused),
            "downscaled": bool(resource.downscaled),
        }

        # Send STOMP message to site agent when resource state changed
        # so that SLURM QoS is updated (e.g. normal -> slowdown -> blocked)
        stomp_sent = False
        state_changed = previous_state != new_state
        if state_changed:
            stomp_sent = policy.apply_policy_actions(resource)

        models.SlurmPolicyEvaluationLog.objects.create(
            policy=policy,
            resource=resource,
            billing_period=current_period,
            usage_percentage=usage_percentage,
            grace_limit_percentage=grace_limit_percentage,
            actions_taken=actions_needed,
            previous_state=previous_state,
            new_state=new_state,
            stomp_message_sent=stomp_sent,
        )

        # Emit structured event
        event_logger.emit(
            "SLURM policy evaluation completed for resource %s: "
            "usage=%.1f%%, actions=%s"
            % (resource.uuid, usage_percentage, actions_needed),
            event_type=EventType.SLURM_POLICY_EVALUATION,
        )

        # Update policy state if any resource actions were applied
        if actions_needed and not policy.has_fired:
            policy.has_fired = True
            policy.fired_datetime = timezone.now()
            policy.save()
            logger.info(
                f"Policy {policy.uuid} marked as fired due to resource {resource.uuid}"
            )

        # Check if policy should be reset (no resources exceed thresholds)
        elif not actions_needed and policy.has_fired:
            # Check if any other resources in offering still exceed thresholds
            other_resources_triggered = check_other_resources_triggered.apply(
                args=[policy.uuid, resource.uuid]
            ).get()

            if not other_resources_triggered:
                policy.has_fired = False
                policy.fired_datetime = timezone.now()
                policy.save()
                logger.info(
                    f"Policy {policy.uuid} reset - no resources exceed thresholds"
                )

    except (
        marketplace_models.Resource.DoesNotExist,
        models.SlurmPeriodicUsagePolicy.DoesNotExist,
    ) as e:
        logger.error(f"Policy evaluation failed - missing object: {e}")
    except Exception as e:
        logger.error(f"Error in resource policy evaluation: {e}")


@shared_task
def check_other_resources_triggered(
    policy_uuid: str, exclude_resource_uuid: str
) -> bool:
    """
    Check if any other resources in the offering still trigger the policy.

    This is used to determine if a policy can be reset when one resource
    drops below thresholds.
    """
    try:
        policy = models.SlurmPeriodicUsagePolicy.objects.get(uuid=policy_uuid)

        # Get all other resources in the offering
        other_resources = (
            marketplace_models.Resource.objects.filter(offering=policy.scope)
            .exclude(uuid=exclude_resource_uuid)
            .exclude(
                state__in=(
                    marketplace_models.ResourceStates.TERMINATED,
                    marketplace_models.ResourceStates.TERMINATING,
                )
            )
        )

        # Check if any other resource exceeds thresholds
        for resource in other_resources:
            usage_percentage = policy.get_resource_usage_percentage(resource)
            if usage_percentage >= 80:  # Minimum threshold for any action
                logger.debug(
                    f"Other resource {resource.uuid} still triggers policy: {usage_percentage:.1f}%"
                )
                return True

        logger.debug(f"No other resources trigger policy {policy.uuid}")
        return False

    except Exception as e:
        logger.error(f"Error checking other resources for policy {policy_uuid}: {e}")
        return True  # Conservative - assume triggered to avoid premature reset


@shared_task
def notify_about_resource_usage(
    resource_uuid: str, policy_uuid: str, usage_percentage: float
):
    """Send notifications about resource usage threshold breaches."""
    try:
        resource = marketplace_models.Resource.objects.get(uuid=resource_uuid)
        policy = models.SlurmPeriodicUsagePolicy.objects.get(uuid=policy_uuid)

        # Send notification to organization owners
        customer = resource.project.customer
        owners = customer.get_user_mails(RoleEnum.CUSTOMER_OWNER)

        if owners:
            context = {
                "resource_name": resource.name,
                "resource_uuid": resource.uuid,
                "usage_percentage": usage_percentage,
                "allocation_limit": policy.component_limits_set.first().limit
                if policy.component_limits_set.exists()
                else "unknown",
                "customer_name": customer.name,
                "project_name": resource.project.name,
            }

            core_utils.broadcast_mail(
                "policy_notifications",
                "slurm_resource_usage_threshold_exceeded",
                context,
                owners,
            )

            logger.info(
                f"Usage notification sent for resource {resource.uuid} ({usage_percentage:.1f}% usage)"
            )

    except Exception as e:
        logger.error(f"Error sending usage notification: {e}")


def send_emails(emails, policy: "models.ProjectEstimatedCostPolicy"):
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

    event_logger.emit(
        "Cost policy has been triggered and emails have been sent.",
        event_type=EventType.POLICY_NOTIFICATION,
        event_context={
            "policy_uuid": policy.uuid.hex,
            "scope": f"{scope_class} UUID: {policy.scope.uuid.hex}",
            "emails": str(emails),
        },
        scopes=[],
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


@shared_task(name="waldur_mastermind.policy.cleanup_slurm_evaluation_logs")
def cleanup_slurm_evaluation_logs():
    """Delete SLURM policy evaluation log entries older than the configured retention period."""
    from datetime import timedelta

    from constance import config

    retention_days = config.SLURM_POLICY_EVALUATION_LOG_RETENTION_DAYS
    cutoff_date = timezone.now() - timedelta(days=retention_days)

    deleted_count, _ = models.SlurmPolicyEvaluationLog.objects.filter(
        evaluated_at__lt=cutoff_date,
    ).delete()

    logger.info(
        "Cleaned up %d SLURM evaluation log entries older than %d days",
        deleted_count,
        retention_days,
    )

    return {"deleted_count": deleted_count, "retention_days": retention_days}
