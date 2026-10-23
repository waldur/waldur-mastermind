"""Commands on a resource's API keys and the site agent's acknowledgements.

Waldur never generates key material. Every change to a key is a command: Waldur
moves the key into a transitional state, records the command as its
``pending_action`` and publishes it; the site agent carries it out at the backend
and acknowledges through a provider endpoint, which settles the key. While a
command is pending, the key refuses another one. A command that failed leaves
the key Erred with the command still recorded; the key then accepts only that
command again (a retry) or a delete, so a retry can never silently stand in for
a different command.

Per-key limits are per calendar period (see ``api_key_spending_limits``) and
enforced here, not at the gateway: a usage report that reaches a limit issues a
``pause`` for that key alone, and Waldur resumes the key once its usage is under
its limits again — when the period resets, or when a limit is raised.

Commands are issued by whoever manages the resource's users. A key's assignee,
the one person trusted with its value, may also replace that value: rotate the
key, or retry a rotation of it that failed.

A key's models are narrowed by its resource's selection and by what the offering
still lists (``model_lists``), so a change to either is an ``update`` for every
key whose effective list it changes. See docs/resource-api-keys.md.
"""

import datetime
import logging

from django.db import transaction
from django.utils import timezone
from django_fsm import TransitionNotAllowed
from rest_framework.exceptions import PermissionDenied, ValidationError

from waldur_core.core.exceptions import IncorrectStateException
from waldur_core.structure.managers import (
    get_connected_customers,
    get_connected_projects,
)
from waldur_mastermind.marketplace import (
    api_key_spending_limits,
    log,
    model_lists,
    models,
    signals,
    spending_limits,
    utils,
)
from waldur_mastermind.marketplace.enums import (
    ResourceApiKeyActions,
    ResourceApiKeyStates,
    ResourceStates,
)

logger = logging.getLogger(__name__)

Actions = ResourceApiKeyActions
States = ResourceApiKeyStates

# The transition that starts each command on an existing key; it also records
# the command as pending_action.
_START = {
    Actions.CREATE: lambda key: key.set_creating(),
    Actions.ROTATE: lambda key: key.set_updating(Actions.ROTATE),
    Actions.UPDATE: lambda key: key.set_updating(Actions.UPDATE),
    Actions.PAUSE: lambda key: key.set_pausing(),
    Actions.RESUME: lambda key: key.set_resuming(),
    Actions.DELETE: lambda key: key.set_deleting(),
}

# Settings the agent applies at the backend. The assignee is Waldur's alone.
AGENT_SETTINGS = frozenset({"spending_limits", "allowed_models"})

# Commands every key-carrying backend accepts, governed or not. Rotation
# predates governance; a resume must stay possible for a key that was paused
# before the provider switched governance off, or the key is stranded.
UNGOVERNED = frozenset({Actions.ROTATE, Actions.RESUME})


def is_resource_live(resource: models.Resource) -> bool:
    return resource.state not in (
        ResourceStates.TERMINATING,
        ResourceStates.TERMINATED,
    )


def require_resource_live(resource: models.Resource) -> None:
    """Reject key commands on a resource that is not live.

    A command emitted for a terminating resource races the termination cleanup
    (which deletes the key rows) at the agent.
    """
    if not is_resource_live(resource):
        raise IncorrectStateException(
            f"The resource is {resource.get_state_display()}; its API keys "
            f"can no longer be managed."
        )


def require_managed(resource: models.Resource) -> None:
    """Reject per-key governance on an offering whose backend cannot do it."""
    if not models.ResourceApiKey.is_managed_for(resource):
        raise ValidationError(
            "The offering of this resource does not support managing API keys."
        )


def is_acting_assignee(api_key: models.ResourceApiKey, user) -> bool:
    """Whether ``user`` is the key's assignee and may still act as one.

    Held to what reveal holds the assignee to: still connected to the
    resource's project or organization, and not a minimal-visibility viewer.
    """
    if not api_key.user_id or api_key.user_id != user.id:
        return False
    resource = api_key.resource
    return (
        resource.project_id in get_connected_projects(user)
        or resource.project.customer_id in get_connected_customers(user)
    ) and not utils.is_resource_project_only_viewer(user, resource)


def _lock(api_key: models.ResourceApiKey) -> models.ResourceApiKey:
    return models.ResourceApiKey.objects.select_for_update().get(pk=api_key.pk)


def _lock_resource(resource: models.Resource) -> models.Resource:
    """Lock the resource; taken before any of its keys.

    It serializes assignments on one resource, and holds the resource's model
    selection until the key is saved: a change to the selection writes the
    resource row and then looks for its keys, so a key created under this lock
    is either seen by it or created after it, with the new selection.
    """
    return models.Resource.objects.select_for_update().get(pk=resource.pk)


def require_free_assignee(
    resource: models.Resource, user, api_key: models.ResourceApiKey | None = None
) -> None:
    """Refuse to assign someone a second key on one resource.

    A person holds at most one key per resource: the backend ties their own
    use of the resource, such as chat, to the key assigned to them, and two
    keys would leave it two answers. A key holds its assignee until it is
    Deleted; one still being deleted may yet fail and stay. Keys on other
    resources do not count. Called with the resource locked.
    """
    if user is None:
        return
    held = resource.api_keys.filter(user=user).exclude(state=States.DELETED)
    if api_key is not None:
        held = held.exclude(pk=api_key.pk)
    other = held.first()
    if other is not None:
        name = other.client_id or "a requested key"
        raise ValidationError(
            {
                "user": f"{user.full_name or user.username} already holds an API "
                f"key on this resource ({name}). A person holds at most one key "
                f"per resource: unassign or delete that key first."
            }
        )


def _recheck_limits(requested, resource, current: dict | None):
    """The requested limits validated again on the locked resource and key.

    The request was validated unlocked: a concurrent edit may since have
    changed the key's limits, which the request may send back as held, or
    lowered the resource's, which cap them.
    """
    field, _value = requested
    try:
        return api_key_spending_limits.clean_requested_limits(
            requested, resource, current
        )
    except ValidationError as error:
        raise ValidationError({field: error.detail})


def request_key(resource: models.Resource, actor, **settings) -> models.ResourceApiKey:
    """Record a key request and ask the agent to create it."""
    requested = settings.pop(api_key_spending_limits.REQUESTED, None)
    require_resource_live(resource)
    with transaction.atomic():
        resource = _lock_resource(resource)
        require_resource_live(resource)
        require_free_assignee(resource, settings.get("user"))
        if requested is not None:
            settings["spending_limits"] = _recheck_limits(requested, resource, None)
        api_key = models.ResourceApiKey.objects.create(
            resource=resource,
            state=States.CREATING,
            pending_action=Actions.CREATE,
            **settings,
        )
        utils.publish_api_key_event(api_key, Actions.CREATE)
    log.log_resource_api_key_command(api_key, Actions.CREATE, actor)
    return api_key


def failed_command(api_key: models.ResourceApiKey) -> str:
    """The command that left an Erred key Erred.

    A key that erred before commands were recorded could only have been
    rotating then.
    """
    return api_key.pending_action or Actions.ROTATE


def issue_command(
    api_key: models.ResourceApiKey,
    action: str | None,
    actor=None,
    reason="",
    assignee_only=False,
    **updates,
) -> models.ResourceApiKey:
    """Start a command on a key and publish it to the agent.

    ``action`` None retries the command that left the key Erred, carrying the
    settings the key has now. Locked, so two concurrent commands cannot both
    read the same settled state, and a retry reads the command it repeats under
    the same lock. ``updates`` are saved with the transition — the settings an
    update command carries. Without an actor the command is Waldur's own (a limit
    was reached, or the key is back under it).

    ``assignee_only`` says the actor was admitted for being the key's assignee
    and nothing more. Such an actor may only rotate the key, never in place of
    another command that failed, and not a settled key that is over a limit
    the offering enforces. All of it is settled under the lock: a retry names
    its command there, and the key may have been reassigned since the actor
    was admitted.

    Deleting a requested key that has no client_id yet publishes nothing: the
    backend holds nothing to revoke, so the key is Deleted at once, even while
    its creation is still pending.
    """
    require_resource_live(api_key.resource)
    with transaction.atomic():
        api_key = _lock(api_key)
        cancel = action == Actions.DELETE and not api_key.client_id
        if action is None:
            if api_key.state != States.ERRED:
                raise IncorrectStateException(
                    f"Only a failed command can be retried; the API key is "
                    f"{api_key.state}."
                )
            action = failed_command(api_key)
        elif api_key.state == States.ERRED:
            # Another command would replace the failed one without it ever
            # having been carried out, and the key would look settled. Once
            # governance is off, a governed command can no longer be retried,
            # so an ungoverned one may take its place rather than strand the key.
            failed = failed_command(api_key)
            retriable = failed in UNGOVERNED or api_key.is_managed
            if retriable and action not in (failed, Actions.DELETE):
                raise IncorrectStateException(
                    f"The API key's {failed} failed; retry it or delete the key."
                )
        elif api_key.pending_action and not cancel:
            raise IncorrectStateException(
                f"The API key is busy: its {api_key.pending_action} has not "
                f"been acknowledged yet."
            )
        if assignee_only:
            if api_key.user_id != actor.id:
                raise PermissionDenied()
            # Once governance is off, a rotation may stand in for a governed
            # command that failed. That is a manager's call: it would bring
            # back a key whose pause or deletion failed.
            stands_in = (
                api_key.state == States.ERRED
                and failed_command(api_key) != Actions.ROTATE
            )
            if action != Actions.ROTATE or stands_in:
                raise PermissionDenied(
                    "The assignee of an API key can only rotate it, or retry a "
                    "rotation of it that failed."
                )
            # A usage report pauses only a settled key, so a rotation in flight
            # holds the limit pause off. Whoever the limit is set for must not
            # be able to keep one in flight. Nothing is held off where limits
            # are not enforced, or for an Erred key, which is never paused for
            # its limit: retrying its rotation is what settles it.
            if (
                api_key.state == States.OK
                and api_key.is_managed
                and api_key_spending_limits.reached_limits(api_key)
            ):
                raise IncorrectStateException(
                    "The API key has reached its limit; its assignee cannot "
                    "rotate it until it is under the limit again."
                )
        if action not in UNGOVERNED:
            require_managed(api_key.resource)
        # A requested key whose creation failed never reached the backend under
        # a client_id; there is nothing to rotate, pause or update, only to
        # request again or drop.
        if not api_key.client_id and action not in (Actions.CREATE, Actions.DELETE):
            raise IncorrectStateException(
                "The API key was never created; it can only be requested again "
                "or deleted."
            )
        try:
            if cancel:
                api_key.cancel_request()
            else:
                _START[action](api_key)
        except TransitionNotAllowed:
            raise IncorrectStateException(
                f"Cannot {action} an API key that is {api_key.state}."
            )
        for field, value in updates.items():
            setattr(api_key, field, value)
        if action in Actions.CARRY_SETTINGS and action != Actions.ROTATE:
            # The command carries the effective list as it is now. A rotation
            # carries it too, but an agent older than that ignores it, so a key
            # marked is still sent an update once the rotation settles.
            api_key.models_outdated = False
        api_key.save()
    if cancel:
        log.log_resource_api_key_command(api_key, action, actor, applied=True)
        return api_key
    utils.publish_api_key_event(api_key, action)
    log.log_resource_api_key_command(api_key, action, actor, reason)
    return api_key


def retry(api_key: models.ResourceApiKey, actor) -> models.ResourceApiKey:
    """Re-issue the command that left the key Erred."""
    return issue_command(api_key, None, actor)


def update_settings(
    api_key: models.ResourceApiKey, actor, changes: dict
) -> models.ResourceApiKey:
    """Change a key's assignee, limits or model allowlist.

    The agent only hears about limits and models, and not while the key is paused:
    the resume command carries them. An Erred key takes new limits and models
    only when its failed command was an update: the edit is then the retry. A
    limit raised above the usage of a key Waldur paused for it resumes the key.

    An assignee may be given only to someone who holds no other key on the
    resource. Where the offering manages keys, a key that holds a secret
    changes hands only by a rotation: the previous assignee, or any project
    member while the key was unassigned, may have revealed the old secret, and
    the backend ties chat and spending to whoever holds the key now. The
    rotation carries whatever limits and models the same edit sets. A paused
    key, or one with another command in flight or failed, has to be resumed,
    settled or retried first. A key that holds no secret yet, and any key of
    an offering without key management, just takes the new assignee. Once
    committed the change is announced with ``resource_api_key_assignee_changed``.
    """
    changes = dict(changes)
    requested = changes.pop(api_key_spending_limits.REQUESTED, None)
    require_resource_live(api_key.resource)
    with transaction.atomic():
        # The resource before the key, as everywhere: it serializes the
        # assignments, and holds the limits that cap the key's.
        resource = None
        if changes.get("user") is not None or requested is not None:
            resource = _lock_resource(api_key.resource)
        api_key = _lock(api_key)
        if api_key.state in (States.DELETING, States.DELETED):
            raise IncorrectStateException(
                f"An API key that is {api_key.state} cannot be edited."
            )
        if requested is not None:
            changes["spending_limits"] = _recheck_limits(
                requested, resource, api_key.spending_limits
            )
        changes = {
            field: value
            for field, value in changes.items()
            if getattr(api_key, field) != value
        }
        rotate = "user" in changes and _changes_hands_by_rotation(api_key)
        if "user" in changes:
            require_free_assignee(api_key.resource, changes["user"], api_key)
        if rotate:
            _require_rotatable_for_assignee(api_key)
            api_key = issue_command(
                api_key,
                Actions.ROTATE,
                actor,
                reason="Its assignee changed.",
                **changes,
            )
        elif not changes.keys() & AGENT_SETTINGS or api_key.state == States.PAUSED:
            if changes:
                # No command, so no new state: ``modified`` is left alone, as
                # the agent's sweep reads it as the time the key entered its
                # state.
                models.ResourceApiKey.objects.filter(pk=api_key.pk).update(**changes)
                for field, value in changes.items():
                    setattr(api_key, field, value)
                # The assignee decides who may reveal a live secret: audited
                # like the commands are.
                log.log_resource_api_key_command(
                    api_key, Actions.UPDATE, actor, applied=True
                )
                if changes.keys() & AGENT_SETTINGS:
                    api_key = resume_if_under_limit(api_key)
        else:
            api_key = issue_command(api_key, Actions.UPDATE, actor, **changes)
        if "user" in changes:
            _announce_assignee_change(api_key)
        return api_key


def _changes_hands_by_rotation(api_key: models.ResourceApiKey) -> bool:
    """Whether a new assignee for the key takes a rotation.

    Only a key whose backend holds a secret has one to replace, and only an
    offering that manages keys ties its use to the assignee. Without key
    management an assignee change stays a record in Waldur, as unassigning
    there always was.
    """
    return bool(api_key.client_id) and api_key.is_managed


def _require_rotatable_for_assignee(api_key: models.ResourceApiKey) -> None:
    """Refuse an assignee change the key cannot rotate for now.

    Called with the key locked. A rotation would bring a paused key back, and
    would replace a command in flight or one that failed without it ever being
    carried out; a failed rotation is simply retried with the new assignee.
    """
    if api_key.state == States.PAUSED:
        message = (
            "The API key is paused. Its assignee changes by rotating it, so "
            "resume the key first."
        )
    elif api_key.state == States.ERRED:
        failed = failed_command(api_key)
        if failed == Actions.ROTATE:
            return
        message = (
            f"The API key's {failed} failed. Its assignee changes by rotating "
            f"it, so retry the {failed} or delete the key first."
        )
    elif api_key.pending_action:
        message = (
            f"The API key is busy: its {api_key.pending_action} has not been "
            f"acknowledged yet. Its assignee changes by rotating it, so wait "
            f"for the {api_key.pending_action} to finish first."
        )
    else:
        return
    raise ValidationError({"user": message})


def _announce_assignee_change(api_key: models.ResourceApiKey) -> None:
    """Announce a key's new assignee once the change commits.

    A backend may tie its own use of the resource, such as chat, to the key's
    assignee. It learns of a new one only in a membership pass, and no command
    on the key starts one. Called only once the edit has been applied, so an
    edit refused on the way announces nothing.
    """
    transaction.on_commit(lambda: _send_assignee_change(api_key))


def _send_assignee_change(api_key: models.ResourceApiKey) -> None:
    # The edit has committed: a receiver failing, such as on a broker outage,
    # must not turn it into an error.
    for receiver, result in signals.resource_api_key_assignee_changed.send_robust(
        sender=models.ResourceApiKey, api_key=api_key
    ):
        if isinstance(result, Exception):
            logger.error(
                "Could not announce the new assignee of API key %s to %s",
                api_key.uuid,
                getattr(receiver, "__qualname__", receiver),
                exc_info=result,
            )


def acknowledge(
    api_key: models.ResourceApiKey,
    transition: str,
    awaited: tuple | None,
    **updates,
) -> models.ResourceApiKey:
    """Settle a key on the agent's report.

    ``awaited`` lists the pending actions this report may settle, so a report
    for one command cannot settle another: a late rotation report must not
    un-pause a key. A blank pending action stands for a key with no recorded
    command; None accepts any (a failure report). Locked, so a duplicate report
    sees the settled state and is refused.
    """
    with transaction.atomic():
        api_key = _lock(api_key)
        if awaited is not None and api_key.pending_action not in awaited:
            raise IncorrectStateException(
                f"The API key is not awaiting this report: "
                f"{api_key.pending_action or 'no command'} is pending and it is "
                f"{api_key.state}."
            )
        try:
            getattr(api_key, transition)()
        except TransitionNotAllowed:
            raise IncorrectStateException(
                f"The report does not apply to an API key that is {api_key.state}."
            )
        for field, value in updates.items():
            setattr(api_key, field, value)
        api_key.save()
        api_key = _pause_if_over_limit(api_key)
    return send_outdated_models(api_key)


def record_created_key(
    resource: models.Resource, client_id: str, key_ciphertext: str
) -> models.ResourceApiKey:
    """Store a key the agent created and applied on its own (at provisioning).

    Idempotent on (resource, client_id): a retried report re-stores the value.
    It may only land on a key that is being created or rotated, or has
    settled OK or Erred — a stale report must not resurrect a key since paused
    or deleted, nor settle another command.

    Nobody requested the key, so nothing restricts it at the backend: it is
    stored as calling every model, and when its resource allows fewer, it is
    sent an ``update`` with those at once.
    """
    require_resource_live(resource)
    with transaction.atomic():
        resource = _lock_resource(resource)
        api_key, created = (
            models.ResourceApiKey.objects.select_for_update().get_or_create(
                resource=resource,
                client_id=client_id,
                defaults={
                    "key_ciphertext": key_ciphertext,
                    "issued_at": timezone.now(),
                    "state": States.OK,
                    "allowed_models": [models.ResourceApiKey.ALL_MODELS],
                },
            )
        )
        if created:
            if model_lists.effective_models(api_key) != model_lists.EVERY_MODEL:
                api_key.models_outdated = True
                api_key.save(update_fields=["models_outdated"])
            return send_outdated_models(api_key)
        if api_key.state not in (
            States.CREATING,
            States.OK,
            States.UPDATING,
            States.ERRED,
        ) or api_key.pending_action not in ("", Actions.CREATE, Actions.ROTATE):
            raise IncorrectStateException(
                f"API key {client_id} is {api_key.state} and cannot be re-reported."
            )
        api_key.key_ciphertext = key_ciphertext
        api_key.issued_at = timezone.now()
        api_key.state = States.OK
        api_key.pending_action = ""
        api_key.error_message = ""
        api_key.save()
        api_key = _pause_if_over_limit(api_key)
    return send_outdated_models(api_key)


def _record_month(api_key, usages: dict, period: datetime.date, daily_usages):
    """Merge a month's usage so far into the key, refusing what days already cover."""
    if api_key.usage_period and period < api_key.usage_period:
        raise IncorrectStateException(
            f"The API key already holds its usage for "
            f"{api_key.usage_period:%Y-%m}; {period:%Y-%m} can no longer be "
            f"reported."
        )
    # The month figure of a component with daily amounts is their sum, and the
    # next daily report would overwrite a month total sent beside them.
    with_days = api_key_spending_limits.types_with_days(api_key, period) | {
        item["type"] for item in daily_usages if item["date"].replace(day=1) == period
    }
    covered = sorted(with_days & set(usages))
    if covered:
        raise ValidationError(
            {
                "usages": f"{', '.join(covered)} has daily amounts in "
                f"{period:%Y-%m}; report its usage per day."
            }
        )
    if period == api_key.usage_period:
        usages = {**(api_key.current_usages or {}), **usages}
    api_key.current_usages = usages
    api_key.usage_period = period


def _record_days(api_key, daily_usages):
    """Store a key's usage per day, and derive its month figure from the days.

    An agent that switches to daily reporting mid-month must report every day
    of the month so far, as for a resource's daily usage: the days replace the
    month total from then on. The month figure never drops within the month
    all the same, so a month total reported before the first daily amounts
    stays as a floor and a limit it reached does not reopen before the days
    catch up. The figure is not billed, so a day corrected downwards counting
    until the month turns errs on the side of the limit.
    """
    api_key_spending_limits.record_days(api_key, daily_usages)
    months = {item["date"].replace(day=1) for item in daily_usages}
    latest = max(months)
    if not api_key.usage_period or latest > api_key.usage_period:
        api_key.current_usages = {}
        api_key.usage_period = latest
    # Days of a month before the one the key holds still count for the limits
    # of the periods they fall in; only the latest month's figure is kept.
    if api_key.usage_period in months:
        current = api_key.current_usages or {}
        api_key.current_usages = {
            **current,
            **{
                component_type: max(current.get(component_type, 0), total)
                for component_type, total in api_key_spending_limits.month_from_days(
                    api_key, api_key.usage_period
                ).items()
            },
        }


def _move_resume_time(api_key: models.ResourceApiKey, reached: list[dict]) -> None:
    """Keep ``limit_resumes_at`` at the reset of the limits that still hold the key.

    Saved without touching ``modified``, which the agent's sweep reads as the
    time the key entered its current state.
    """
    resumes_at = spending_limits.resumes_at(reached)
    if api_key.limit_resumes_at != resumes_at:
        api_key.limit_resumes_at = resumes_at
        models.ResourceApiKey.objects.filter(pk=api_key.pk).update(
            limit_resumes_at=resumes_at
        )


def record_usage(
    api_key: models.ResourceApiKey,
    usages: dict | None,
    period: datetime.date,
    daily_usages=(),
) -> models.ResourceApiKey:
    """Store a key's usage, and pause or resume it against its limits.

    ``usages``, unless None, is the usage so far in the month ``period``: a
    report for the month the key already holds merges per component, and one for
    a later month replaces it. Only the latest month is kept, so a report for an earlier one
    is refused. ``daily_usages`` are amounts per UTC date; a date reported again
    replaces its amount, and the month figure of a component with daily amounts
    is their sum.

    Accepted in any state, a deleted key's included: its usage up to the
    deletion still counts. The report leaves ``modified`` alone, which the
    agent's sweep reads as the time the key entered its current state.
    """
    with transaction.atomic():
        api_key = _lock(api_key)
        if usages is not None:
            _record_month(api_key, usages, period, daily_usages)
        if daily_usages:
            _record_days(api_key, daily_usages)
        models.ResourceApiKey.objects.filter(pk=api_key.pk).update(
            current_usages=api_key.current_usages, usage_period=api_key.usage_period
        )
        reached = api_key_spending_limits.reached_limits(api_key)
        if reached:
            api_key = _pause_if_over_limit(api_key, reached)
        else:
            api_key = resume_if_under_limit(api_key)
    return api_key


def _pause_if_over_limit(
    api_key: models.ResourceApiKey, reached: list[dict] | None = None
) -> models.ResourceApiKey:
    """Pause a settled key whose usage has reached one of its limits.

    Called with the key locked, and with the limits it has reached when the
    caller already has them. Only a settled key of a live resource: a command
    in flight is left to finish, and whatever settles it checks again — the
    next usage report, or the agent's acknowledgement, so a key that reached
    its limit while a rotation was in flight cannot settle unpaused.
    """
    if reached is None:
        reached = api_key_spending_limits.reached_limits(api_key)
    if (
        reached
        and api_key.is_managed
        and api_key.state == States.OK
        and not api_key.pending_action
        and is_resource_live(api_key.resource)
    ):
        api_key = issue_command(
            api_key,
            Actions.PAUSE,
            reason=f"Its usage reached the {spending_limits.describe(reached)}.",
            paused_by_limit=True,
            limit_resumes_at=spending_limits.resumes_at(reached),
        )
    elif reached and api_key.paused_by_limit:
        # A longer limit reached since the pause holds the key past the
        # reset of the one that paused it.
        _move_resume_time(api_key, reached)
    return api_key


def resume_if_under_limit(api_key: models.ResourceApiKey) -> models.ResourceApiKey:
    """Resume a key Waldur paused for its limit once its usage is under it again.

    That happens when the period of every reached limit resets, or when a limit
    is raised or removed. A key paused by a person is left alone, and so is one
    with a command in flight: the pause has to have settled first. A key still
    over a limit gets the time that limit resets as its ``limit_resumes_at``.
    """
    with transaction.atomic():
        api_key = _lock(api_key)
        if (
            api_key.state == States.PAUSED
            and api_key.paused_by_limit
            and not api_key.pending_action
            and is_resource_live(api_key.resource)
        ):
            reached = api_key_spending_limits.reached_limits(api_key)
            if reached:
                _move_resume_time(api_key, reached)
            else:
                api_key = issue_command(
                    api_key,
                    Actions.RESUME,
                    reason="Its usage is under its limit again.",
                )
    return api_key


def resume_keys_under_limit(reset_only=False) -> None:
    """Resume every key paused for its limit whose usage is under it again.

    A usage report resumes such a key on its own, but when a period resets no
    report may come for a while: this catches the reset. ``reset_only`` checks
    only the keys whose ``limit_resumes_at`` has passed.
    """
    keys = models.ResourceApiKey.objects.filter(
        state=States.PAUSED, paused_by_limit=True, pending_action=""
    )
    if reset_only:
        keys = keys.filter(limit_resumes_at__lte=timezone.now())
    for api_key in keys:
        # One key failing to resume must not keep the others paused.
        try:
            resume_if_under_limit(api_key)
        except Exception:
            logger.exception(
                "Could not resume API key %s under its limit", api_key.uuid
            )


def retry_failed_limit_pauses() -> None:
    """Issue again every limit pause the agent failed to carry out.

    Such a key is Erred with the pause as its failed command, and still live at
    the backend: neither a usage report nor the resume tasks touch an Erred
    key, so without this it keeps being served until someone presses Retry.
    A key that is no longer over its limit is left for a person to settle.
    """
    keys = models.ResourceApiKey.objects.filter(
        state=States.ERRED,
        pending_action=Actions.PAUSE,
        paused_by_limit=True,
        resource__offering__plugin_options__enable_api_key_provisioning=True,
    ).exclude(
        resource__state__in=(ResourceStates.TERMINATING, ResourceStates.TERMINATED)
    )
    for api_key in keys:
        # One key failing must not keep the others live.
        try:
            _retry_limit_pause(api_key)
        except Exception:
            logger.exception(
                "Could not retry the limit pause of API key %s", api_key.uuid
            )


def _retry_limit_pause(api_key: models.ResourceApiKey) -> None:
    with transaction.atomic():
        api_key = _lock(api_key)
        if not (
            api_key.state == States.ERRED
            and api_key.pending_action == Actions.PAUSE
            and api_key.paused_by_limit
            and api_key.is_managed
            and is_resource_live(api_key.resource)
        ):
            return
        reached = api_key_spending_limits.reached_limits(api_key)
        if not reached:
            return
        issue_command(
            api_key,
            None,
            reason=f"Its pause failed, and its usage is still over the "
            f"{spending_limits.describe(reached)}.",
            limit_resumes_at=spending_limits.resumes_at(reached),
        )


def resource_models_changed(
    resource: models.Resource, before: list[str], before_choices: list[str] | None
) -> None:
    """Send the keys of a resource their new effective model lists.

    ``before`` is what the resource allowed its keys until now, and
    ``before_choices`` what its offering listed. A key whose effective list
    moves gets an ``update`` command. One that cannot take a command now — one
    is in flight, or its last one failed — is marked ``models_outdated`` and
    gets it once it settles, unless a command carrying its settings goes first.
    A paused key's resume carries the list, and a deleted key needs none. Keys
    of an offering without key management take no governed command, so they
    are left alone.
    """
    if not models.ResourceApiKey.is_managed_for(resource) or not is_resource_live(
        resource
    ):
        return
    after = model_lists.resource_models(resource)
    after_choices = model_lists.offered_models(resource.offering)
    if model_lists.same(before, after) and model_lists.same_choices(
        before_choices, after_choices
    ):
        return
    # A paused key is looked at too: _update_models locks it, so a resume
    # racing this change either waits for the new selection or is marked.
    for api_key in resource.api_keys.exclude(
        state__in=(States.DELETING, States.DELETED)
    ):
        if not model_lists.same(
            model_lists.key_models(before, api_key.allowed_models, before_choices),
            model_lists.key_models(after, api_key.allowed_models, after_choices),
        ):
            _update_models(api_key)


def _update_models(api_key: models.ResourceApiKey) -> None:
    with transaction.atomic():
        api_key = _lock(api_key)
        if (
            api_key.state == States.OK
            and not api_key.pending_action
            and api_key.client_id
        ):
            issue_command(
                api_key,
                Actions.UPDATE,
                reason="The resource's models changed.",
            )
        elif api_key.state not in (States.PAUSED, States.DELETING, States.DELETED):
            models.ResourceApiKey.objects.filter(pk=api_key.pk).update(
                models_outdated=True
            )


def offering_models_changed(
    offering: models.Offering,
    plugin_options: dict | None,
    resource_options: dict | None,
) -> None:
    """Send an offering's keys their models after its settings changed them.

    ``plugin_options`` and ``resource_options`` are the offering's settings
    before the change: key management turned on, the models option becoming or
    stopping being a multiple choice, or models added to or removed from its
    choices. Each resource is locked while its keys are compared, so a key
    reported or a selection saved meanwhile reads the offering as saved.

    Without key management nothing narrowed a key's list, so a key is taken to
    hold its own list as stored, a model the offering stopped listing included.
    """
    before_choices = (
        model_lists.offered(resource_options)
        if (plugin_options or {}).get("enable_api_key_provisioning")
        else None
    )
    resource_ids = (
        models.Resource.objects.filter(offering=offering)
        .exclude(state__in=(ResourceStates.TERMINATING, ResourceStates.TERMINATED))
        .values_list("pk", flat=True)
    )
    for resource_id in resource_ids:
        with transaction.atomic():
            resource = models.Resource.objects.select_for_update().get(pk=resource_id)
            # The offering as it is being saved, without a query per resource.
            resource.offering = offering
            before = model_lists.allowed(
                plugin_options, resource_options, resource.options
            )
            resource_models_changed(resource, before, before_choices)


def send_outdated_keys_models() -> None:
    """Send every settled key marked ``models_outdated`` its models.

    A key is marked when it could not take an update, and is sent one as it
    settles; this catches the ones nothing settles, such as keys an upgrade
    found applied wider than their resource allows. Keys on an offering
    without key management stay marked, but are left alone until it is
    turned on again.
    """
    for api_key in models.ResourceApiKey.objects.filter(
        models_outdated=True,
        state=States.OK,
        pending_action="",
        resource__offering__plugin_options__enable_api_key_provisioning=True,
    ):
        # One key failing must not keep the others outdated.
        try:
            send_outdated_models(api_key)
        except Exception:
            logger.exception("Could not send API key %s its models", api_key.uuid)


def send_outdated_models(api_key: models.ResourceApiKey) -> models.ResourceApiKey:
    """Send a key that has just settled the models its resource changed meanwhile."""
    if not api_key.models_outdated:
        return api_key
    with transaction.atomic():
        api_key = _lock(api_key)
        if (
            api_key.models_outdated
            and api_key.state == States.OK
            and not api_key.pending_action
            and api_key.client_id
            and is_resource_live(api_key.resource)
            and api_key.is_managed
        ):
            api_key = issue_command(
                api_key,
                Actions.UPDATE,
                reason="The resource's models changed.",
            )
    return api_key
