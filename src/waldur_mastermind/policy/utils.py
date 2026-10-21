import logging
import threading
import uuid
from contextlib import contextmanager

from django.utils import timezone

from waldur_core.core.utils import ROBOT_USERNAMES, get_system_robot
from waldur_core.logging import middleware as logging_middleware

from .structures import EvaluationTriggers

logger = logging.getLogger(__name__)

# How many `system_actor_event_context` blocks are open in this thread. The
# event context alone cannot tell: Celery copies it into every task published
# during an evaluation, and on down the chain, so a trigger in the context does
# not mean an evaluation is running here and now.
_evaluation_state = threading.local()


def in_evaluation() -> bool:
    """Whether this thread is inside a policy evaluation (or reset) right now."""
    return getattr(_evaluation_state, "depth", 0) > 0


#: The event-context fields that say what started an evaluation and who.
EVALUATION_FIELDS = (
    "trigger",
    "trigger_object_uuid",
    "initiated_by_uuid",
    "initiated_by_username",
    "initiated_as_username",
)


def uuid_hex(obj) -> str | None:
    """`obj.uuid` as 32 hex characters, or None when there is no object.

    Right after `objects.create(uuid="…")`, as the structure import does, the
    attribute is still the string it was given, not a `UUID`; `.uuid.hex`
    raised there, failing the save the handler ran for."""
    value = getattr(obj, "uuid", None)
    if not value:
        return None
    try:
        return uuid.UUID(str(value)).hex
    except ValueError:
        return str(value)


def _is_person(context) -> bool:
    """Whether the context's user is a person rather than absent or a robot
    account (`ROBOT_USERNAMES`)."""
    return bool(context.get("user_uuid")) and (
        context.get("user_username") not in ROBOT_USERNAMES
    )


def current_initiator(context=None) -> dict | None:
    """The person whose action is running, as `{"uuid", "username"}` plus
    `"as_username"` under impersonation, or None.

    A person acting now wins. Under impersonation that is the impersonator, as
    for revisions (`core.handlers`), with the account acted as kept beside it.
    Otherwise the event user is a robot or absent: inside
    `system_actor_event_context` it is already the system robot, so the
    initiator recorded there is returned, and a nested block or a task
    dispatched from within one keeps the person who started it. A robot is
    never an initiator: background jobs that run as one (monthly invoicing,
    say) are started by nobody.
    """
    if context is None:
        context = logging_middleware.get_event_context() or {}
    if _is_person(context):
        if context.get("user_impersonator_uuid"):
            return {
                "uuid": context["user_impersonator_uuid"],
                "username": context.get("user_impersonator_username"),
                "as_username": context.get("user_username"),
            }
        return {"uuid": context["user_uuid"], "username": context.get("user_username")}
    if context.get("initiated_by_uuid"):
        return {
            "uuid": context["initiated_by_uuid"],
            "username": context.get("initiated_by_username"),
            "as_username": context.get("initiated_as_username"),
        }
    return None


def _with_evaluation_fields(previous, trigger=None, trigger_object_uuid=None):
    """`previous` with the evaluation fields brought up to date.

    The fields already in the context carry over, so a Celery task keeps what
    its dispatching request recorded. A new `trigger` replaces the old one and
    its object together: an object left over from an earlier trigger would
    point at something unrelated.
    """
    context = {
        key: value for key, value in previous.items() if key not in EVALUATION_FIELDS
    }
    fields = {key: previous[key] for key in EVALUATION_FIELDS if previous.get(key)}
    if trigger:
        fields.pop("trigger_object_uuid", None)
        fields["trigger"] = trigger
    if trigger_object_uuid:
        fields["trigger_object_uuid"] = trigger_object_uuid
    initiator = current_initiator(previous)
    fields.pop("initiated_by_uuid", None)
    fields.pop("initiated_by_username", None)
    fields.pop("initiated_as_username", None)
    if initiator:
        fields["initiated_by_uuid"] = initiator["uuid"]
        if initiator.get("username"):
            fields["initiated_by_username"] = initiator["username"]
        if initiator.get("as_username"):
            fields["initiated_as_username"] = initiator["as_username"]
    context.update(fields)
    return context


@contextmanager
def _event_context(context):
    previous = logging_middleware.get_event_context()
    logging_middleware.set_event_context(context)
    try:
        yield
    finally:
        logging_middleware.set_event_context(previous)


@contextmanager
def evaluation_event_context(
    trigger=None, trigger_object_uuid=None, keep_existing=False
):
    """Record what started the work in the block on every event it emits,
    keeping whoever the events are attributed to. Celery tasks published
    within the block carry the fields too, through the event context
    `celeryconf.pass_event_context` attaches to every task.

    With `keep_existing`, work done inside an evaluation running in this
    thread stays attributed to what started that evaluation. A trigger that
    merely arrived with the event context (a task published during some
    earlier evaluation) does not count: it is replaced."""
    previous = logging_middleware.get_event_context() or {}
    if keep_existing and in_evaluation() and previous.get("trigger"):
        yield
        return
    with _event_context(
        _with_evaluation_fields(previous, trigger, trigger_object_uuid)
    ):
        yield


@contextmanager
def system_actor_event_context(trigger=None, trigger_object_uuid=None):
    """Attribute events emitted within the block to the system robot instead
    of the ambient request user, and record why they happened.

    Every event emitted within the block also carries the `EVALUATION_FIELDS`:
    `trigger` (an `EvaluationTriggers` value), `trigger_object_uuid` when there
    is one, and `initiated_by_uuid` / `initiated_by_username` for the person
    being replaced (the impersonator under impersonation, with
    `initiated_as_username` for the account acted as). In a Celery task the request's context, fields included,
    was carried over by `celeryconf`, so the person is still known there.

    Policy actions can execute inside a user's HTTP request — when a policy is
    (re)saved the serializer evaluates it synchronously and runs the actions
    in-request. Without this override, ``compile_context`` stamps that user
    (and their IP / user-agent) onto every event emitted while the action
    runs — both the policy's own ``request_pausing`` event and the resource
    save's ``update_succeeded`` event — so the activity log reads as if the
    user paused the resource. The resource change is already recorded against
    the system robot in reversion; this aligns the event log with that.

    Only override — and only pay for the ``get_system_robot()`` lookup — when
    there is a person to misattribute to. On the async/beat path with no
    request context, and when the events are already the robot's (a nested
    block, or a job that runs as the robot), only the fields are added. This
    matters at scale: thousands of usage reports evaluate policies on that
    path, and a per-evaluation get_or_create on the User table would be pure
    overhead there.
    """
    previous = logging_middleware.get_event_context() or {}
    context = _with_evaluation_fields(previous, trigger, trigger_object_uuid)
    if _is_person(previous):
        system_robot = get_system_robot()
        robot = system_robot._get_log_context("user") if system_robot else {}
        context = {
            **robot,
            **{key: context[key] for key in EVALUATION_FIELDS if key in context},
        }
    _evaluation_state.depth = getattr(_evaluation_state, "depth", 0) + 1
    try:
        with _event_context(context):
            yield
    finally:
        _evaluation_state.depth -= 1


def evaluate_policies(
    policies,
    reconcile_already_fired=False,
    trigger=None,
    trigger_object_uuid=None,
):
    # Policy actions (and the resource saves they perform) emit events; attribute
    # them to the system robot, not to whoever's request happened to trigger the
    # evaluation, and record what started it. is_triggered() below emits
    # nothing, so wrapping the whole loop is safe.
    with system_actor_event_context(trigger, trigger_object_uuid):
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
                    with evaluation_event_context(EvaluationTriggers.RECONCILE):
                        _reconcile_idempotent_actions(policy)
                continue

            policy.refresh_from_db()

            logger.info(
                "A policy %s has fired.%s",
                policy.uuid.hex,
                _decision_log_suffix(policy),
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
                "A policy %s has not fired.%s",
                policy.uuid.hex,
                _decision_log_suffix(policy),
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


def _decision_log_suffix(policy) -> str:
    """The gate and figures behind an estimated-cost policy's evaluation, for
    the fired / not fired log lines; `refresh_from_db` keeps the `decision`
    attribute `evaluate` set. Other policies log the limit when they have one.
    The trigger and initiator of the evaluation follow, when known."""
    decision = getattr(policy, "decision", None)
    if decision is not None:
        suffix = "".join(
            f" {key}={value}" for key, value in decision.as_event_context().items()
        )
    elif hasattr(policy, "limit_cost"):
        suffix = f" limit_cost={policy.limit_cost}"
    else:
        suffix = ""
    context = logging_middleware.get_event_context() or {}
    for key in EVALUATION_FIELDS:
        if context.get(key):
            suffix += f" {key}={context[key]}"
    return suffix


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
