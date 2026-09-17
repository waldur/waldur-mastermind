import logging
from contextlib import contextmanager

from django.utils import timezone

from waldur_core.core.utils import get_system_robot
from waldur_core.logging import middleware as logging_middleware

logger = logging.getLogger(__name__)


@contextmanager
def system_actor_event_context():
    """Attribute events emitted within the block to the system robot instead
    of the ambient request user.

    Policy actions can execute inside a user's HTTP request — when a policy is
    (re)saved the serializer evaluates it synchronously and runs the actions
    in-request. Without this override, ``compile_context`` stamps that user
    (and their IP / user-agent) onto every event emitted while the action
    runs — both the policy's own ``request_pausing`` event and the resource
    save's ``update_succeeded`` event — so the activity log reads as if the
    user paused the resource. The resource change is already recorded against
    the system robot in reversion; this aligns the event log with that.

    Only override — and only pay for the ``get_system_robot()`` lookup — when
    there is an ambient *request user* to misattribute to. On the async/beat
    path (Celery, no request context) there is nothing to override, so the
    system-robot query is skipped entirely. This matters at scale: thousands of
    usage reports evaluate policies on that path, and a per-evaluation
    get_or_create on the User table would be pure overhead there.
    """
    previous = logging_middleware.get_event_context()
    if not previous or "user_uuid" not in previous:
        # Background path (or an anonymous request): no request user to hide.
        yield
        return
    system_robot = get_system_robot()
    context = system_robot._get_log_context("user") if system_robot else {}
    logging_middleware.set_event_context(context)
    try:
        yield
    finally:
        logging_middleware.set_event_context(previous)


def evaluate_policies(policies, reconcile_already_fired=False):
    # Policy actions (and the resource saves they perform) emit events; attribute
    # them to the system robot, not to whoever's request happened to trigger the
    # evaluation. is_triggered() below emits nothing, so wrapping the whole loop
    # is safe.
    with system_actor_event_context():
        _evaluate_policies(policies, reconcile_already_fired=reconcile_already_fired)


def _evaluate_policies(policies, reconcile_already_fired=False):
    """Evaluate ``policies`` and run/reset their actions as needed.

    ``reconcile_already_fired`` controls what happens when a policy is still
    triggered but was already fired by an earlier evaluation (see
    ``_reconcile_idempotent_actions`` for why this matters): the frequent,
    per-event evaluation paths (invoice-item saves, credit changes — driven
    through ``evaluate_policies_async``) leave it ``False`` and keep the
    original "fire exactly once per edge" semantics those callers, and the
    tests covering them, depend on. Only the periodic ``check-polices`` sweep
    (`policy/tasks.py::check_polices`) passes ``True`` — reconciliation is a
    "did the fire actually stick" safety net, not something that should run
    on every single billing event for a policy that already fired correctly.
    """
    for policy in policies:
        if policy.is_triggered():
            # Atomic CAS: only fire if has_fired is still False in DB.
            # Prevents double-fire when concurrent workers evaluate the same policy.
            updated = (
                type(policy)
                .objects.filter(pk=policy.pk, has_fired=False)
                .update(has_fired=True, fired_datetime=timezone.now())
            )
            if updated == 0:
                # Already fired by another worker (or by an earlier
                # evaluation). The CAS above only ever runs actions on the
                # original False->True edge, so a policy that fires while its
                # action affects zero resources (e.g. a `supports_pausing`
                # offering flag was off, or a resource hadn't been created
                # yet) would otherwise stay silently ineffective forever:
                # `is_triggered()` keeps returning True, so this branch keeps
                # being reached, but the CAS never matches again.
                if reconcile_already_fired:
                    _reconcile_idempotent_actions(policy)
                continue

            policy.refresh_from_db()

            extra = ""
            if hasattr(policy, "limit_cost"):
                extra = f" limit_cost={policy.limit_cost}"
            logger.info(
                "A policy %s has fired.%s",
                policy.uuid.hex,
                extra,
            )

            for action in policy.get_immediate_actions():
                action.method(policy)
                logger.info(
                    "%s action of policy %s has been triggered.",
                    action.method.__name__,
                    policy.uuid.hex,
                )
        else:
            # Atomic CAS: only reset if has_fired is still True in DB.
            updated = (
                type(policy)
                .objects.filter(pk=policy.pk, has_fired=True)
                .update(has_fired=False, fired_datetime=timezone.now())
            )
            if updated == 0:
                continue  # Already reset by another worker

            policy.refresh_from_db()
            logger.info(
                "A policy %s has not fired.",
                policy.uuid.hex,
            )

            logger.info("Resetting immediate actions")
            for action in policy.get_immediate_actions():
                reset_method = action.reset_method
                if reset_method:
                    logger.info(
                        "Running immediate action reset method %s.",
                        reset_method.__name__,
                    )
                    reset_method(policy)

            logger.info("Resetting threshold actions")
            for action in policy.get_threshold_actions():
                reset_method = action.reset_method
                if reset_method:
                    logger.info(
                        "Running threshold action reset method %s.",
                        reset_method.__name__,
                    )
                    reset_method(policy)


def _reconcile_idempotent_actions(policy):
    """Re-apply an already-fired policy's idempotent resource-state actions.

    Only actions with a ``reset_method`` are re-run here — that is exactly
    the set of "toggle a resource field" actions (``request_pausing``,
    ``request_downscaling``, ``restrict_members``), whose target querysets in
    ``policy_actions._apply_generic_action`` filter on live state (e.g. an
    offering's ``supports_pausing`` flag) and always skip a resource already
    at the desired value. Re-running them is therefore a no-op for anything
    the original fire already handled, and the only way resources that
    became newly eligible since then — a flag turned on, a resource added to
    the policy's scope — ever get reconciled.

    One-shot actions (``notify_*``, ``terminate_resources``) have no
    ``reset_method`` and are deliberately skipped: they must fire exactly
    once per True->False->True cycle, not on every periodic re-evaluation.
    """
    for action in policy.get_immediate_actions():
        if action.reset_method is None:
            continue
        action.method(policy)
        logger.info(
            "%s action of policy %s has been reconciled.",
            action.method.__name__,
            policy.uuid.hex,
        )
