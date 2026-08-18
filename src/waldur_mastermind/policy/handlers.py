import logging

from django.core.cache import cache
from django.db import transaction
from django.db.models import Q

from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.invoices.models import CustomerCredit, ProjectCredit

from . import models, tasks
from .models import ProjectEstimatedCostPolicy

logger = logging.getLogger(__name__)

# Debounce interval for cost policy evaluation (seconds).
# During month-boundary windows, hundreds of invoice items are created rapidly
# by the site agent. Each save triggers policy evaluation, but the compensation
# calculation depends on ALL items being present (shared credit is consumed
# sequentially). Debouncing ensures evaluation runs after the burst settles.
# Configurable via settings.WALDUR_COST_POLICY_DEBOUNCE_SECONDS.
COST_POLICY_DEBOUNCE_SECONDS = 120

# Fast debounce interval for policies that must act promptly after a single
# resource breaches its limit. Resource-scoped and credit-agnostic
# (``use_credit=False``) project policies do not depend on the month-boundary
# compensation burst settling — there is no shared credit to distribute — so
# they do not need the full window before pausing.
#
# This is deliberately a few seconds rather than ~0: the debounce also
# coalesces bursts, and many resources in one project reporting usage at the
# same time (e.g. a fleet on a 30-minute cadence) would otherwise schedule an
# evaluation roughly every second for the whole burst. A short floor collapses
# such a burst into a handful of evaluations per project while still pausing
# well within a "prompt" window.
# Configurable via settings.WALDUR_COST_POLICY_FAST_DEBOUNCE_SECONDS.
COST_POLICY_FAST_DEBOUNCE_SECONDS = 10


def _get_debounce_seconds():
    from django.conf import settings

    return getattr(
        settings, "WALDUR_COST_POLICY_DEBOUNCE_SECONDS", COST_POLICY_DEBOUNCE_SECONDS
    )


def _get_fast_debounce_seconds():
    from django.conf import settings

    return getattr(
        settings,
        "WALDUR_COST_POLICY_FAST_DEBOUNCE_SECONDS",
        COST_POLICY_FAST_DEBOUNCE_SECONDS,
    )


def _project_fast_debounce_seconds(project_id):
    """Return the fast countdown when the project has a policy that must fire
    promptly, otherwise ``None`` (caller falls back to the default window).

    A resource-scoped or ``use_credit=False`` policy targets a single resource
    and has no compensation to wait for, so it should pause the resource within
    seconds of a breach rather than after the full debounce window.
    """
    has_fast_policy = (
        ProjectEstimatedCostPolicy.objects.filter(scope_id=project_id)
        .filter(Q(resource__isnull=False) | Q(use_credit=False))
        .exists()
    )
    return _get_fast_debounce_seconds() if has_fast_policy else None


def _debounced_call(task, args, cache_key, debounce_seconds=None):
    """Schedule a debounced Celery task call.

    Uses cache.add() (atomic SETNX) to ensure only one task is scheduled
    per cache_key within the debounce window. Subsequent triggers for the
    same key are silently dropped — the scheduled task will pick up all
    changes when it runs.

    The actual broker publish is deferred to ``transaction.on_commit`` so
    it runs after the surrounding DB transaction commits. Two reasons:

    1. If the publish fails (broker stalled, connection half-open), the
       caller's state changes are already durable. The HTTP request can
       return success even if policy evaluation is briefly skipped — the
       next trigger will catch up.
    2. Outside any transaction (e.g. a one-off shell), ``on_commit``
       runs the callback immediately, so the helper is safe in both
       request and non-request contexts.

    Publish exceptions are swallowed and logged: policy evaluation is a
    side-effect, not a correctness requirement, so a transient broker
    blip must not propagate up to a 5xx on the parent operation. On such a
    failure the claim is released (``cache.delete``) so the *next* trigger
    reschedules immediately instead of being dropped against a stale key —
    otherwise a single failed publish opens a blind window for the whole
    debounce interval (longer, if no further trigger arrives, until the
    periodic safety-net sweep).

    ``debounce_seconds`` overrides the default window for a single call;
    callers pass the fast interval for policies that must act promptly.
    """
    if debounce_seconds is None:
        debounce_seconds = _get_debounce_seconds()
    if debounce_seconds <= 0:
        # Debounce disabled — dispatch immediately (used in tests).
        task.delay(*args)
        return

    def _publish() -> None:
        if not cache.add(cache_key, True, timeout=debounce_seconds):
            return  # Already scheduled for this cache_key
        logger.info(
            "Debounce: scheduling %s (key %s), countdown=%ds",
            task.name,
            cache_key,
            debounce_seconds,
        )
        try:
            task.apply_async(args=list(args), countdown=debounce_seconds)
        except Exception:  # noqa: BLE001
            # Release the claim so the next trigger retries rather than being
            # dropped against a key that guards a task which was never enqueued.
            cache.delete(cache_key)
            logger.exception(
                "Deferred policy publish failed for %s key=%s; "
                "released debounce key so the next trigger reschedules.",
                task.name,
                cache_key,
            )

    transaction.on_commit(_publish)


def _debounced_evaluate(policy_path, filters, cache_key, debounce_seconds=None):
    """Debounced dispatch of ``evaluate_policies_async`` — convenience wrapper."""
    _debounced_call(
        tasks.evaluate_policies_async,
        (policy_path, filters),
        cache_key,
        debounce_seconds=debounce_seconds,
    )


_CUSTOMER_POLICY_PATH = "waldur_mastermind.policy.models.CustomerEstimatedCostPolicy"
_PROJECT_POLICY_PATH = "waldur_mastermind.policy.models.ProjectEstimatedCostPolicy"
_OFFERING_USAGE_POLICY_PATH = "waldur_mastermind.policy.models.OfferingUsagePolicy"
_OFFERING_ESTIMATED_COST_POLICY_PATH = (
    "waldur_mastermind.policy.models.OfferingEstimatedCostPolicy"
)
_CUSTOMER_COMPONENT_USAGE_POLICY_PATH = (
    "waldur_mastermind.policy.models.CustomerComponentUsagePolicy"
)


def customer_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    """Evaluate customer cost policies when invoice items are updated."""
    invoice_item = instance
    customer_id = invoice_item.invoice.customer_id
    _debounced_evaluate(
        _CUSTOMER_POLICY_PATH,
        {"scope_id": customer_id},
        f"cost_policy_debounce:customer:{customer_id}",
    )


def project_estimated_cost_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    """Evaluate project cost policies when invoice items are updated."""
    invoice_item = instance
    project_id = invoice_item.project_id
    _debounced_evaluate(
        _PROJECT_POLICY_PATH,
        {"scope_id": project_id},
        f"cost_policy_debounce:project:{project_id}",
        debounce_seconds=_project_fast_debounce_seconds(project_id),
    )


def get_offering_trigger_handler(klass_path):
    def handler(sender, instance, created=False, **kwargs):
        resource = instance.resource
        if not resource:
            return
        offering_id = resource.offering_id
        # `is_triggered()` on each offering policy re-filters customers by its
        # own `organization_groups`, so a single `scope_id` filter is enough.
        _debounced_evaluate(
            klass_path,
            {"scope_id": offering_id},
            f"cost_policy_debounce:{klass_path}:offering:{offering_id}",
        )

    return handler


offering_usage_policy_trigger_handler = get_offering_trigger_handler(
    _OFFERING_USAGE_POLICY_PATH,
)


def slurm_periodic_usage_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    """
    Lightweight signal handler that queues background policy evaluation.

    This avoids blocking the ComponentUsage creation request with heavy
    policy evaluation logic by delegating to Celery background tasks.
    """
    component_usage = instance
    resource = component_usage.resource

    if not (resource and resource.offering):
        logger.warning(
            "ComponentUsage signal received without valid resource/offering context"
        )
        return

    if not models.SlurmPeriodicUsagePolicy.objects.filter(
        scope=resource.offering
    ).exists():
        return

    _debounced_call(
        tasks.evaluate_slurm_resource_policy,
        (str(resource.uuid),),
        f"slurm_policy_debounce:resource:{resource.uuid.hex}",
    )


offering_estimated_cost_policy_trigger_handler = get_offering_trigger_handler(
    _OFFERING_ESTIMATED_COST_POLICY_PATH,
)


def customer_component_usage_policy_trigger_handler(
    sender, instance, created=False, **kwargs
):
    """Evaluate customer component usage policies when component usage records change."""
    usage = instance

    if not usage:
        return

    customer_id = usage.resource.project.customer_id
    component_id = usage.component_id
    _debounced_evaluate(
        _CUSTOMER_COMPONENT_USAGE_POLICY_PATH,
        {
            "scope_id": customer_id,
            "component_limits_set__component_id": component_id,
        },
        f"cost_policy_debounce:{_CUSTOMER_COMPONENT_USAGE_POLICY_PATH}"
        f":customer:{customer_id}:component:{component_id}",
    )


def get_estimated_cost_policy_handler_for_observable_class(klass, observable_class):
    def handler(sender, instance, created=False, **kwargs):
        if not isinstance(instance, observable_class):
            return

        logger.info(
            "Estimated cost policy handler called for %s instance (created=%s).",
            sender.__name__,
            created,
        )

        observable_object = instance

        if getattr(observable_object, "is_mocked", False):
            logger.debug(
                "Skipping policy handler for mocked %s instance.",
                observable_object.__class__.__name__,
            )
            return

        policies = klass.objects.filter(
            scope=klass.get_scope_from_observable_object(observable_object)
        )

        logger.info(
            "Estimated cost policy handler triggered for %s (created=%s). Found %d policies.",
            observable_object.__class__.__name__,
            created,
            policies.count(),
        )

        for policy in policies:
            # Resource-scoped policies (project cost policies) only act on their
            # own resource; skip when the saved resource is a different one.
            policy_resource_id = getattr(policy, "resource_id", None)
            if policy_resource_id and policy_resource_id != observable_object.pk:
                continue

            if policy.get_threshold_actions() and policy.has_fired:
                threshold_actions = policy.get_threshold_actions()
                logger.info(
                    "Processing policy %s (UUID: %s). Found %d threshold actions.",
                    policy.scope.name,
                    policy.uuid.hex,
                    len(threshold_actions),
                )
                for action in threshold_actions:
                    if action.ignored_fields and hasattr(observable_object, "tracker"):
                        if not set(observable_object.tracker.changed()) - set(
                            action.ignored_fields
                        ):
                            continue

                    action.method(policy, created)
                    logger.info(
                        "%s action has been triggered for %s. Policy UUID: %s",
                        action.method.__name__,
                        policy.scope.name,
                        policy.uuid.hex,
                    )

    return handler


def customer_credit_changed_handler(
    sender, instance: CustomerCredit, created=False, **kwargs
):
    """Handle customer credit value changes and evaluate related policies."""
    customer_credit = instance

    if not customer_credit.tracker.has_changed("value"):
        return

    logger.info(
        "%s has changed; customer and project policy evaluation will be "
        "scheduled once this transaction commits (never, if it's rolled back)",
        customer_credit,
    )
    customer_id = customer_credit.customer_id
    _debounced_evaluate(
        _CUSTOMER_POLICY_PATH,
        {"scope_id": customer_id},
        f"cost_policy_debounce:customer:{customer_id}",
    )
    _debounced_evaluate(
        _PROJECT_POLICY_PATH,
        {"scope__customer_id": customer_id},
        f"cost_policy_debounce:projects_of_customer:{customer_id}",
    )


def project_credit_changed_handler(
    sender, instance: ProjectCredit, created=False, **kwargs
):
    project_credit = instance

    if not project_credit.tracker.has_changed("value"):
        return

    logger.info(
        "%s has changed; project policy evaluation will be scheduled once "
        "this transaction commits (never, if it's rolled back)",
        project_credit,
    )
    project_id = project_credit.project_id
    _debounced_evaluate(
        _PROJECT_POLICY_PATH,
        {"scope_id": project_id},
        f"cost_policy_debounce:project:{project_id}",
        debounce_seconds=_project_fast_debounce_seconds(project_id),
    )


def customer_credit_offerings_list_changed_handler(
    sender, instance, action, reverse, model, pk_set, **kwargs
):
    if action not in ("post_add", "post_remove", "post_clear"):
        return
    if pk_set is None:
        customer_ids = [instance.customer_id]
    else:
        customer_ids = list(
            invoices_models.CustomerCredit.objects.filter(
                offerings__pk__in=list(pk_set),
            ).values_list("customer_id", flat=True)
        )
    # Per-customer dispatch with a cache key shared by
    # ``customer_estimated_cost_policy_trigger_handler`` so credit edits and
    # invoice-item saves dedupe with each other inside the debounce window.
    for cid in customer_ids:
        _debounced_evaluate(
            _CUSTOMER_POLICY_PATH,
            {"scope_id": cid},
            f"cost_policy_debounce:customer:{cid}",
        )


def run_reset_actions_upon_cost_policy_deletion(
    sender, instance: ProjectEstimatedCostPolicy, **kwargs
) -> None:
    """
    Execute reset actions when a cost policy is deleted.

    Args:
        sender: The model class that sent the signal
        instance: The policy instance being deleted
        kwargs: Additional keyword arguments
    """
    policy: models.ProjectEstimatedCostPolicy = instance

    try:
        actions = policy.get_immediate_actions()
        for action in actions:
            reset_method = action.reset_method
            if reset_method:
                logger.info(
                    "Running immediate action reset method %s for policy %s (UUID: %s)",
                    reset_method.__name__,
                    policy.scope.name if policy.scope else "unknown",
                    policy.uuid.hex,
                )
                try:
                    reset_method(policy)
                except Exception as e:
                    logger.exception(
                        "Failed to execute reset method %s: %s",
                        reset_method.__name__,
                        str(e),
                    )
    except Exception as e:
        logger.exception(
            "Failed to run reset actions for policy %s: %s",
            getattr(policy, "uuid", "unknown"),
            str(e),
        )


_BLOCKING_ACTION = "block_creation_of_new_resources"


def _period_months(policy):
    periods = invoices_models.PeriodMixin.Periods
    return {periods.MONTH_1: 1, periods.MONTH_3: 3, periods.MONTH_12: 12}.get(
        policy.period, 0
    )


def _period_query(policy):
    """Build an invoice-month Q filter for the policy's period."""
    import datetime

    from dateutil.relativedelta import relativedelta
    from django.db.models import Q

    from waldur_core.core.utils import month_start

    current_month_start = month_start(datetime.date.today())
    query = Q()
    for n in range(_period_months(policy)):
        previous_month = current_month_start - relativedelta(months=n)
        query |= Q(
            invoice__month=previous_month.month,
            invoice__year=previous_month.year,
        )
    return query


def _existing_total(items_qs, policy):
    """Sum invoice item totals matching the policy period, mirroring
    ``EstimatedCostPolicyMixin._is_triggered``."""
    from waldur_core.structure.models import Customer

    active_customers = Customer.objects.filter(blocked=False, archived=False)
    items = (
        items_qs.filter(invoice__customer__in=active_customers)
        .exclude(invoice__state=invoices_models.Invoice.States.CANCELED)
        .filter(_period_query(policy))
    )
    return sum(item.total for item in items)


def _check_project_policies(resource, cost):
    """Mirror ProjectEstimatedCostPolicy.is_triggered with `cost` added."""
    from waldur_mastermind.invoices import compensations as invoices_compensation
    from waldur_mastermind.marketplace.exceptions import PolicyException

    project = resource.project
    # Resource-scoped policies govern an existing resource and never block the
    # creation of other resources, so they are excluded from the creation gate.
    policies = models.ProjectEstimatedCostPolicy.objects.filter(
        scope=project, actions__contains=_BLOCKING_ACTION, resource__isnull=True
    )
    for policy in policies:
        items_qs = invoices_models.InvoiceItem.objects.filter(project=project)
        existing = _existing_total(items_qs, policy)
        if policy.use_credit:
            compensation = invoices_compensation.MonthlyCompensation(
                project.customer
            ).get_project_compensation(project)
        else:
            compensation = 0
        projected = existing + cost
        if projected - compensation <= policy.limit_cost:
            continue
        # Above the limit: credit may still cover the overage (unless the policy
        # is configured to ignore credit).
        if policy.use_credit:
            project_credit = invoices_models.ProjectCredit.objects.filter(
                project=project
            ).first()
            if project_credit:
                if project_credit.value > policy.limit_cost:
                    continue
            else:
                customer_credit = invoices_models.CustomerCredit.objects.filter(
                    customer=project.customer
                ).first()
                if customer_credit and customer_credit.value > policy.limit_cost:
                    continue
        raise PolicyException(
            f"Creation of new resources in this project is prohibited by {policy}. "
            f"Projected cost ({projected - compensation}) would exceed "
            f"limit ({policy.limit_cost})."
        )


def _check_customer_policies(resource, cost):
    """Mirror CustomerEstimatedCostPolicy.is_triggered with `cost` added."""
    from waldur_mastermind.invoices import compensations as invoices_compensation
    from waldur_mastermind.marketplace.exceptions import PolicyException

    customer = resource.project.customer
    policies = models.CustomerEstimatedCostPolicy.objects.filter(
        scope=customer, actions__contains=_BLOCKING_ACTION
    )
    for policy in policies:
        items_qs = invoices_models.InvoiceItem.objects.filter(
            invoice__customer=customer
        )
        existing = _existing_total(items_qs, policy)
        compensation = invoices_compensation.MonthlyCompensation(
            customer
        ).total_compensation
        projected = existing + cost
        if projected - compensation <= policy.limit_cost:
            continue
        customer_credit = invoices_models.CustomerCredit.objects.filter(
            customer=customer
        ).first()
        if customer_credit and customer_credit.value > policy.limit_cost:
            continue
        raise PolicyException(
            f"Creation of new resources in this organization is prohibited by {policy}. "
            f"Projected cost ({projected - compensation}) would exceed "
            f"limit ({policy.limit_cost})."
        )


def _check_offering_policies(resource, cost):
    """Mirror OfferingEstimatedCostPolicy.is_triggered with `cost` added.
    Offering policies don't apply compensation or credit shortcuts."""
    from waldur_mastermind.marketplace.exceptions import PolicyException

    offering = resource.offering
    customer = resource.project.customer
    policies = models.OfferingEstimatedCostPolicy.objects.filter(
        scope=offering, actions__contains=_BLOCKING_ACTION
    )
    for policy in policies:
        # Skip policies that don't apply to this customer.
        if (
            not policy.apply_to_all
            and not customer.organization_groups.filter(
                pk__in=policy.organization_groups.values_list("pk", flat=True)
            ).exists()
        ):
            continue
        if policy.apply_to_all:
            items_qs = invoices_models.InvoiceItem.objects.filter(
                resource__offering=offering,
                invoice__customer__blocked=False,
                invoice__customer__archived=False,
            )
        else:
            items_qs = invoices_models.InvoiceItem.objects.filter(
                resource__offering=offering,
                invoice__customer__organization_groups__in=(
                    policy.organization_groups.all()
                ),
            )
        existing = _existing_total(items_qs, policy)
        projected = existing + cost
        if projected > policy.limit_cost:
            raise PolicyException(
                f"Creation of new resources is prohibited by {policy}. "
                f"Projected cost ({projected}) would exceed "
                f"limit ({policy.limit_cost})."
            )


def validate_resource_creation_against_cost_policies(sender, resource, cost, **kwargs):
    """
    Proactively validate that creating a resource won't violate any cost policy
    (project-, customer-, or offering-scoped) carrying the
    ``block_creation_of_new_resources`` action.

    Called by the marketplace module via the ``resource_creation_validation``
    signal. Raising ``PolicyException`` (a DRF ``ValidationError``) aborts the
    enclosing transaction and surfaces a 400 to the API client.
    """
    if not hasattr(resource, "project") or not resource.project:
        return
    _check_project_policies(resource, cost)
    _check_customer_policies(resource, cost)
    if getattr(resource, "offering", None):
        _check_offering_policies(resource, cost)
