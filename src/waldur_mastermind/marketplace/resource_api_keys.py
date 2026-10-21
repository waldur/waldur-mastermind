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
its limits again — when the period resets, or when a limit is raised. See
docs/resource-api-keys.md.
"""

import datetime
import logging

from django.db import transaction
from django.utils import timezone
from django_fsm import TransitionNotAllowed
from rest_framework.exceptions import ValidationError

from waldur_core.core.exceptions import IncorrectStateException
from waldur_mastermind.marketplace import (
    api_key_spending_limits,
    log,
    models,
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


def _lock(api_key: models.ResourceApiKey) -> models.ResourceApiKey:
    return models.ResourceApiKey.objects.select_for_update().get(pk=api_key.pk)


def request_key(resource: models.Resource, actor, **settings) -> models.ResourceApiKey:
    """Record a key request and ask the agent to create it."""
    require_resource_live(resource)
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
    **updates,
) -> models.ResourceApiKey:
    """Start a command on a key and publish it to the agent.

    ``action`` None retries the command that left the key Erred, carrying the
    settings the key has now. Locked, so two concurrent commands cannot both
    read the same settled state, and a retry reads the command it repeats under
    the same lock. ``updates`` are saved with the transition — the settings an
    update command carries. Without an actor the command is Waldur's own (a limit
    was reached, or the key is back under it).

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
    only when its failed command was an update: the edit is then the retry. An
    assignee change is Waldur's alone and applies in any state short of
    deletion. A limit raised above the usage of a key Waldur paused for it
    resumes the key.
    """
    require_resource_live(api_key.resource)
    with transaction.atomic():
        api_key = _lock(api_key)
        if api_key.state in (States.DELETING, States.DELETED):
            raise IncorrectStateException(
                f"An API key that is {api_key.state} cannot be edited."
            )
        changes = {
            field: value
            for field, value in changes.items()
            if getattr(api_key, field) != value
        }
        if not changes.keys() & AGENT_SETTINGS or api_key.state == States.PAUSED:
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
            return api_key
        return issue_command(api_key, Actions.UPDATE, actor, **changes)


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
    return api_key


def record_created_key(
    resource: models.Resource, client_id: str, key_ciphertext: str
) -> models.ResourceApiKey:
    """Store a key the agent created and applied on its own (at provisioning).

    Idempotent on (resource, client_id): a retried report re-stores the value.
    It may only land on a key that is being created or rotated, or has
    settled OK or Erred — a stale report must not resurrect a key since paused
    or deleted, nor settle another command.
    """
    require_resource_live(resource)
    with transaction.atomic():
        api_key, created = (
            models.ResourceApiKey.objects.select_for_update().get_or_create(
                resource=resource,
                client_id=client_id,
                defaults={
                    "key_ciphertext": key_ciphertext,
                    "issued_at": timezone.now(),
                    "state": States.OK,
                },
            )
        )
        if created:
            return api_key
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
    return api_key


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
        # Only a settled key of a live resource: a command in flight is left to
        # finish, and the next report checks again.
        if (
            reached
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
        elif not reached:
            api_key = resume_if_under_limit(api_key)
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
