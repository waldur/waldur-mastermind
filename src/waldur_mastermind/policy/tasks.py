import hashlib
import json
import logging

from celery import shared_task
from django.core.cache import cache
from django.core.paginator import Paginator
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.utils.module_loading import import_string

from waldur_core.core import tasks as core_tasks
from waldur_core.core import utils as core_utils
from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType, ObservableObjectType
from waldur_core.logging.models import EventSubscriptionQueue
from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.policy import models

from . import utils

logger = logging.getLogger(__name__)


@shared_task(name="waldur_mastermind.policy.evaluate_policies_async")
def evaluate_policies_async(policy_class_path, filters):
    """Evaluate policies matching the given filters in a background task.

    Args:
        policy_class_path: Dotted path to the policy model class,
            e.g. "waldur_mastermind.policy.models.ProjectEstimatedCostPolicy".
        filters: A dict of keyword arguments passed to queryset.filter().
    """
    klass = import_string(policy_class_path)
    policies = klass.objects.filter(**filters).distinct()
    if policies.exists():
        logger.info(
            "Evaluating %d %s policies asynchronously (filters=%s)",
            policies.count(),
            klass.__name__,
            filters,
        )
        utils.evaluate_policies(policies)


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
        policy = models.SlurmPeriodicUsagePolicy.objects.get(uuid=policy_uuid)

        current_period = policy._get_current_period()

        # Lock the resource row for the whole read-modify-write so concurrent
        # evaluations of the same resource serialize. Without the lock a slow
        # high-usage evaluation could commit paused/downscaled flags after a
        # newer low-usage evaluation already cleared them (lost update), leaving
        # the resource stuck restricted while current usage is 0.
        with transaction.atomic():
            resource = marketplace_models.Resource.objects.select_for_update().get(
                uuid=resource_uuid
            )

            logger.info(
                f"Evaluating resource {resource.uuid} against policy {policy.uuid}"
            )

            # Recompute usage under the lock from freshly-read state so the
            # decision below reflects the latest ComponentUsage and flags.
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

            # Also pause if project is in grace period and offering supports pausing
            if (
                resource.project.is_in_grace_period
                and resource.offering.plugin_options.get("supports_pausing")
                and "pause" not in actions_needed
            ):
                actions_needed.append("pause")

            # Check for downscaling (independent of pausing)
            if (
                usage_percentage >= 100
                and "request_slurm_resource_downscaling" in policy.actions
            ):
                actions_needed.append("downscale")

            # Check for notification (independent of other actions, once per billing period)
            if (
                usage_percentage >= 80
                and "notify_organization_owners" in policy.actions
            ):
                already_notified = models.SlurmPolicyEvaluationLog.objects.filter(
                    policy=policy,
                    resource=resource,
                    billing_period=current_period,
                    actions_taken__contains="notify",
                ).exists()
                if not already_notified:
                    actions_needed.append("notify")

            # Apply or remove actions based on current usage
            # Handle pausing
            if "pause" in actions_needed:
                if not resource.paused:
                    resource.paused = True
                    resource.save()
                    logger.info(
                        f"Resource {resource.uuid} paused due to {usage_percentage:.1f}% usage"
                    )
            elif resource.paused and usage_percentage < grace_limit_percentage:
                # Don't unpause if project is in grace period and offering supports pausing
                in_lifecycle_grace = (
                    resource.project.is_in_grace_period
                    and resource.offering.plugin_options.get("supports_pausing")
                )
                if not in_lifecycle_grace:
                    resource.paused = False
                    resource.save()
                    logger.info(
                        f"Resource {resource.uuid} pause removed - usage {usage_percentage:.1f}% below grace limit"
                    )
                else:
                    logger.info(
                        f"Resource {resource.uuid} remains paused - project in grace period"
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

        if actions_needed or state_changed:
            event_logger.emit(
                "SLURM policy evaluation completed for resource %s: "
                "usage=%.1f%%, actions=%s"
                % (resource.uuid, usage_percentage, actions_needed),
                event_type=EventType.SLURM_POLICY_EVALUATION,
            )
        else:
            logger.debug(
                "SLURM policy evaluation no-op for resource %s: usage=%.1f%%",
                resource.uuid,
                usage_percentage,
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
            # Check if any other resources in offering still exceed thresholds.
            # Call the function directly instead of via Celery .apply().get()
            # to avoid RuntimeError('Never call result.get() within a task!').
            other_resources_triggered = _check_other_resources_triggered(
                str(policy.uuid), str(resource.uuid)
            )

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


def _check_other_resources_triggered(
    policy_uuid: str, exclude_resource_uuid: str
) -> bool:
    """
    Check if any other resources in the offering still trigger the policy.

    This is used to determine if a policy can be reset when one resource
    drops below thresholds.  Called as a plain function (not a Celery task)
    to avoid the "Never call result.get() within a task!" error.
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
def check_other_resources_triggered(
    policy_uuid: str, exclude_resource_uuid: str
) -> bool:
    """Celery task wrapper for _check_other_resources_triggered."""
    return _check_other_resources_triggered(policy_uuid, exclude_resource_uuid)


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


@shared_task(name="waldur_mastermind.policy.reset_slurm_policies_on_period_boundary")
def reset_slurm_policies_on_period_boundary():
    """Re-evaluate paused/downscaled SLURM resources whose current period has no usage.

    Runs daily (idempotent). For each SlurmPeriodicUsagePolicy, checks whether any
    active resources are still paused or downscaled while having 0% usage in the
    current period — which means the pause carried over from a previous period and
    should be cleared.

    This is safe to re-run: if the resource is already unpaused, re-evaluation
    with 0% usage is a no-op. If Celery beat was down or the queue was flushed,
    the next successful run catches up automatically.
    """
    policies = models.SlurmPeriodicUsagePolicy.objects.all()
    total_evaluated = 0

    for policy in policies:
        current_period = policy._get_current_period()
        if current_period == "total":
            continue

        # Find active resources that are still paused or downscaled
        resources = (
            marketplace_models.Resource.objects.filter(
                offering=policy.scope,
            )
            .exclude(
                state__in=(
                    marketplace_models.ResourceStates.TERMINATED,
                    marketplace_models.ResourceStates.TERMINATING,
                ),
            )
            .filter(
                Q(paused=True) | Q(downscaled=True),
            )
        )

        if not resources.exists():
            continue

        # Check if any of these resources have 0% usage in the current period.
        # If so, the pause is stale (from a previous period) and needs re-evaluation.
        policy_evaluated = 0
        for resource in resources:
            usage_pct = policy.get_resource_usage_percentage(resource, current_period)
            if usage_pct < 100:
                evaluate_resource_against_policy.delay(
                    str(resource.uuid), str(policy.uuid)
                )
                policy_evaluated += 1

        total_evaluated += policy_evaluated
        logger.info(
            "Period reset check: queued %d evaluations for policy %s (period=%s)",
            policy_evaluated,
            policy.uuid,
            current_period,
        )

    logger.info(
        "Period reset check complete: queued %d total evaluations", total_evaluated
    )
    return {"evaluated": total_evaluated}


@shared_task(
    name="waldur_mastermind.policy.sync_slurm_periodic_settings",
    base=core_tasks.BackgroundTask,
)
def sync_slurm_periodic_settings():
    """Send apply_periodic_settings to all active SLURM resources.

    Runs every 10 minutes to keep SLURM GrpTRESMins, fairshare, and QoS
    thresholds in sync with current policy settings. This is needed
    because:

    - The site agent sets initial limits without the grace buffer
    - Carryover and period boundaries change the effective allocation
    - Drift can occur after site-agent or SLURM restarts

    Uses a cache to skip resources whose settings haven't changed since
    the last sync, avoiding unnecessary STOMP messages.
    """
    total_sent = 0
    total_skipped = 0
    total_failed = 0

    for policy in models.SlurmPeriodicUsagePolicy.objects.all():
        # Skip offerings without a connected site-agent
        has_queue = EventSubscriptionQueue.objects.filter(
            offering_uuid=policy.scope.uuid,
            object_type=ObservableObjectType.RESOURCE_PERIODIC_LIMITS.value,
        ).exists()
        if not has_queue:
            continue

        resources = marketplace_models.Resource.objects.filter(
            offering=policy.scope,
            state=marketplace_models.ResourceStates.OK,
        )

        if not policy.apply_to_all and policy.organization_groups.exists():
            resources = resources.filter(
                project__customer__organization_groups__in=policy.organization_groups.all()
            )

        paginator = Paginator(resources.order_by("pk"), 200)
        for page_num in paginator.page_range:
            for resource in paginator.page(page_num).object_list:
                settings = policy.calculate_slurm_settings(resource)

                # Hash the settings to detect changes since last sync
                settings_hash = hashlib.md5(
                    json.dumps(settings, sort_keys=True, default=str).encode()
                ).hexdigest()
                cache_key = f"slurm_periodic_settings:{resource.uuid}"

                if cache.get(cache_key) == settings_hash:
                    total_skipped += 1
                    continue

                success = policy.apply_policy_actions(resource)
                if success:
                    # Cache for 24h — beat runs every 10min, so this just
                    # prevents re-sending identical settings until they change.
                    cache.set(cache_key, settings_hash, timeout=86400)
                    total_sent += 1
                else:
                    total_failed += 1

    logger.info(
        "SLURM periodic settings sync: %d sent, %d skipped (unchanged), %d failed",
        total_sent,
        total_skipped,
        total_failed,
    )
    return {"sent": total_sent, "skipped": total_skipped, "failed": total_failed}
