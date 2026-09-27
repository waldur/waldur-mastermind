"""Publish call and proposal state changes to unified-queue event consumers.

A proposal event belongs to the proposal, its call and the call's managing
organisation. So a consumer bound to a call receives the state changes of every
proposal submitted to it, and one bound to a single proposal receives only that
proposal's. A call event belongs to the call, its managing organisation and that
organisation's customer.

The organiser's customer is left out of the proposal chain on purpose. The API
admits customer-level roles to proposals only through PROPOSAL.LIST
(``Proposal.Permissions.list_permission``), and no customer role carries it --
an organisation owner reads proposals through a role on the call or the managing
organisation. Delivery re-checks only that a role exists somewhere in the chain,
not which permission it grants, so a customer key here would send proposal names
and states to owners, support and readers the API hides them from. Calls are
different: owners hold CALL.LIST, and active calls are public anyway.

The chains are built here rather than through ``permission_utils.scope_keys_for``:
``get_scope_ancestors`` walks project/offering/customer links and knows nothing
about the call hierarchy -- for a proposal it would yield the *applicant's*
project and customer, who cannot see the proposal through the API. They are
registered with ``event_dispatch.register_event_chain`` (see ``apps.py``), so the
registration guard and ``delivery_blocked_reason`` resolve bindings the same way.

Only state transitions are published. Creating a draft emits nothing, and
rounds have no state of their own (open/closed is derived from the clock), so
they emit nothing either.

Payloads carry identifiers, names and states only — never proposal content,
reviews or reviewer identities. A consumer that needs more reads it through the
API, where the usual permission checks apply.
"""

from django.db import transaction

from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.logging import event_dispatch
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import ObservableObjectType

from . import models

CALL_STATE_CHANGED = "call_state_changed"
PROPOSAL_STATE_CHANGED = "proposal_state_changed"


def call_chain(call: models.Call) -> list:
    return [call, call.manager, call.manager.customer]


def proposal_chain(proposal: models.Proposal) -> list:
    # No organiser customer: see the module docstring.
    call = proposal.round.call
    return [proposal, call, call.manager]


def _call_fields(call: models.Call) -> dict:
    return {
        "call_uuid": call.uuid.hex,
        "call_name": call.name,
        "call_slug": call.slug,
        "customer_uuid": call.manager.customer.uuid.hex,
    }


def _build(scope_keys, payload_builder, object_type, event_type) -> list[dict]:
    if get_skip_side_effects():
        return []
    return event_dispatch.build_messages(
        scope_keys,
        payload_builder,
        object_type,
        event_type=event_type,
        include_global=True,
    ).messages


def _send(messages: list[dict]):
    if not messages:
        return
    # After commit, so a rolled-back transition is never announced.
    transaction.on_commit(lambda: logging_tasks.publish_messages.delay(messages))


def _build_call_state_changed(call: models.Call, previous_state: str) -> list[dict]:
    return _build(
        event_dispatch.event_scope_keys(call),
        lambda: {
            **_call_fields(call),
            "state": call.state,
            "previous_state": previous_state,
        },
        ObservableObjectType.CALL,
        CALL_STATE_CHANGED,
    )


def _build_proposal_state_changed(
    proposal: models.Proposal, previous_state: str
) -> list[dict]:
    call = proposal.round.call
    return _build(
        event_dispatch.event_scope_keys(proposal),
        lambda: {
            "proposal_uuid": proposal.uuid.hex,
            "proposal_name": proposal.name,
            "proposal_slug": proposal.slug,
            "round_uuid": proposal.round.uuid.hex,
            **_call_fields(call),
            "state": proposal.state,
            "previous_state": previous_state,
        },
        ObservableObjectType.PROPOSAL,
        PROPOSAL_STATE_CHANGED,
    )


def publish_call_state_changed(call: models.Call, previous_state: str):
    _send(_build_call_state_changed(call, previous_state))


def publish_proposal_state_changed(proposal: models.Proposal, previous_state: str):
    _send(_build_proposal_state_changed(proposal, previous_state))


def publish_proposal_state_changes(transitions) -> None:
    """Announce many proposal transitions as ONE published batch.

    Matching still happens per proposal — a proposal-bound consumer must
    receive only its own — but the whole round's messages travel in a single
    Celery task instead of one per cancelled draft.

    ``transitions`` is an iterable of ``(proposal, previous_state)``.
    """
    messages: list[dict] = []
    for proposal, previous_state in transitions:
        messages.extend(_build_proposal_state_changed(proposal, previous_state))
    _send(messages)


def emit_call_state_changed(sender, instance: models.Call, created=False, **kwargs):
    if created or not instance.tracker.has_changed("state"):
        return
    publish_call_state_changed(instance, instance.tracker.previous("state"))


def emit_proposal_state_changed(
    sender, instance: models.Proposal, created=False, **kwargs
):
    if created or not instance.tracker.has_changed("state"):
        return
    publish_proposal_state_changed(instance, instance.tracker.previous("state"))
