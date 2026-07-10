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


def evaluate_policies(policies):
    # Policy actions (and the resource saves they perform) emit events; attribute
    # them to the system robot, not to whoever's request happened to trigger the
    # evaluation. is_triggered() below emits nothing, so wrapping the whole loop
    # is safe.
    with system_actor_event_context():
        _evaluate_policies(policies)


def _evaluate_policies(policies):
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
                continue  # Already fired by another worker

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
