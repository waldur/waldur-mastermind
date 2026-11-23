"""The rules that decide whether an order is reviewed, as data.

The consumer and provider review steps are each decided by an ordered list of
rules: the first rule that holds for an order decides. ``decide`` walks a list
over the facts of a real order -- this is what the engine routes orders by --
and ``simulate`` walks the same list over facts that may be unknown, which is
how the order-flow description draws the branches for an orderer who has not
placed an order yet. Both read the same list, so the description cannot say
something the engine does not do.

A rule reads facts by name from a facts object. A fact is ``True``, ``False``
or, only when simulating, ``None`` for "depends on the order". Facts are read
lazily and in rule order, so ``decide`` asks no more than the first rule that
holds needs. A rule that combines facts uses ``and``, never a helper taking
evaluated arguments: a fact about the user must not be read for an offering
type the rule does not concern -- it costs queries, and the user may be gone.
"""

import dataclasses
from collections.abc import Callable
from typing import Any

from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    REMOTE_OFFERING,
    SCRIPT_OFFERING,
    SITE_AGENT_OFFERING,
)

Tri = bool | None


@dataclasses.dataclass(frozen=True)
class Source:
    """Where a rule's input lives, for the description to point at.

    ``kind`` is one of ``plugin_option``, ``offering``, ``constance``,
    ``project``, ``orderer``, ``order``, ``role`` or ``engine``. Only
    ``plugin_option`` and ``offering`` are the provider's to change.
    """

    kind: str
    field: str

    @property
    def editable(self) -> bool:
        return self.kind in ("plugin_option", "offering")


@dataclasses.dataclass(frozen=True)
class Rule:
    key: str
    # Plain-language statement of the rule's condition and outcome.
    text: str
    applies: Callable[[Any], Tri]
    # Whether the review is skipped when the rule applies.
    skips: Callable[[Any], Tri]
    sources: tuple[Source, ...] = ()


def _po(field):
    return Source("plugin_option", field)


CONSUMER_RULES: tuple[Rule, ...] = (
    Rule(
        "purchase_order_hold",
        "The offering requires a purchase order: the order waits for consumer "
        "review until one is attached. Terminations are exempt.",
        applies=lambda f: f.held_for_purchase_order,
        skips=lambda f: False,
        sources=(_po("require_purchase_order_upload"), Source("order", "attachment")),
    ),
    Rule(
        "placed_with_review",
        "An order placed automatically carries the review that authorized it "
        "(the call manager who accepted a proposal).",
        applies=lambda f: f.placed_automatically_with_review,
        skips=lambda f: True,
        sources=(Source("order", "placed_automatically"),),
    ),
    Rule(
        "staff",
        "Orders placed by staff skip consumer review.",
        applies=lambda f: f.creator_is_staff,
        skips=lambda f: True,
        sources=(Source("orderer", "is_staff"),),
    ),
    Rule(
        "autoapprove_disabled",
        "Auto-approval is disabled: every order is reviewed by the consumer. "
        "Terminations are exempt.",
        applies=lambda f: f.autoapprove_disabled,
        skips=lambda f: False,
        sources=(_po("disable_autoapprove"),),
    ),
    Rule(
        "private_offering",
        "On a private offering, whoever may approve private orders in the "
        "project approves their own order.",
        applies=lambda f: f.offering_is_private,
        skips=lambda f: f.creator_can_approve_private_order,
        sources=(
            Source("offering", "shared"),
            Source("offering", "billable"),
            Source("role", "APPROVE_PRIVATE_ORDER"),
        ),
    ),
    Rule(
        "service_provider_project",
        "Orders from the provider's own organization skip consumer review.",
        applies=lambda f: f.service_provider_project_autoapprove,
        skips=lambda f: True,
        sources=(
            _po("auto_approve_in_service_provider_projects"),
            Source("project", "customer"),
        ),
    ),
    Rule(
        "provider_owner_terminates",
        "An owner of the provider's organization terminates without consumer review.",
        applies=lambda f: f.termination_by_provider_owner,
        skips=lambda f: True,
        sources=(Source("orderer", "provider_owner"),),
    ),
    Rule(
        "auto_approve_role",
        "The orderer holds a role whose orders are approved automatically.",
        applies=lambda f: f.creator_holds_auto_approve_role,
        skips=lambda f: True,
        sources=(_po("auto_approve_for_roles"),),
    ),
    Rule(
        "approver_orders",
        "Whoever may approve orders in the project approves their own order; "
        "anyone else's order waits for them.",
        applies=lambda f: True,
        skips=lambda f: f.creator_can_approve_order,
        sources=(Source("role", "APPROVE_ORDER"),),
    ),
)


PROVIDER_RULES: tuple[Rule, ...] = (
    Rule(
        "site_agent",
        "Site agent offerings: every order is reviewed on the provider side, "
        "usually by the agent itself.",
        applies=lambda f: f.offering_type == SITE_AGENT_OFFERING,
        skips=lambda f: False,
        sources=(Source("offering", "type"),),
    ),
    Rule(
        "basic",
        "Basic offerings: every order is reviewed by the provider, and the "
        "approval is what fulfills it.",
        applies=lambda f: f.offering_type == BASIC_OFFERING,
        skips=lambda f: False,
        sources=(Source("offering", "type"),),
    ),
    Rule(
        "remote_auto_approve",
        "Remote offerings with remote auto-approval skip provider review.",
        applies=lambda f: f.offering_type == REMOTE_OFFERING
        and f.auto_approve_remote_orders,
        skips=lambda f: True,
        sources=(Source("offering", "type"), _po("auto_approve_remote_orders")),
    ),
    Rule(
        "remote_provider_owner",
        "Remote offerings: an order approved by an owner of the provider's "
        "organization (or staff) skips provider review.",
        applies=lambda f: f.offering_type == REMOTE_OFFERING
        and f.approver_owns_provider,
        skips=lambda f: True,
        sources=(Source("offering", "type"), Source("orderer", "provider_owner")),
    ),
    Rule(
        "remote_service_manager",
        "Remote offerings: an order approved by a service manager of the "
        "provider who is also a user of the offering skips provider review.",
        applies=lambda f: f.offering_type == REMOTE_OFFERING
        and f.approver_manages_offering,
        skips=lambda f: True,
        sources=(
            Source("offering", "type"),
            Source("orderer", "provider_service_manager"),
        ),
    ),
    Rule(
        "remote",
        "Remote offerings: any other order is reviewed by the provider.",
        applies=lambda f: f.offering_type == REMOTE_OFFERING,
        skips=lambda f: False,
        sources=(Source("offering", "type"),),
    ),
    Rule(
        "script",
        "Script offerings skip provider review unless script auto-approval is "
        "turned off.",
        applies=lambda f: f.offering_type == SCRIPT_OFFERING,
        skips=lambda f: f.auto_approve_marketplace_script,
        sources=(Source("offering", "type"), _po("auto_approve_marketplace_script")),
    ),
    Rule(
        "automatic",
        "Orders of this offering type are not reviewed by the provider.",
        applies=lambda f: True,
        skips=lambda f: True,
        sources=(Source("offering", "type"),),
    ),
)


def decide(rules: tuple[Rule, ...], facts) -> tuple[bool, Rule]:
    """Whether the review is skipped for a real order, and the deciding rule."""
    for rule in rules:
        if rule.applies(facts):
            return bool(rule.skips(facts)), rule
    raise AssertionError("The last rule of a list always applies.")


@dataclasses.dataclass(frozen=True)
class RuleOutcome:
    rule: Rule
    # True, False or None ("depends on the order").
    holds: Tri
    # When the rule holds: whether the review is skipped (None: depends).
    skips: Tri
    # Whether the walk reached this rule at all.
    reached: bool


@dataclasses.dataclass(frozen=True)
class Simulation:
    # "skipped", "required" or "depends".
    verdict: str
    outcomes: tuple[RuleOutcome, ...]

    @property
    def deciding(self) -> tuple[RuleOutcome, ...]:
        """The rules that hold or may hold, in order."""
        return tuple(o for o in self.outcomes if o.reached and o.holds is not False)


def simulate(rules: tuple[Rule, ...], facts) -> Simulation:
    """Walk ``rules`` over facts that may be unknown.

    A rule that certainly holds ends the walk, as in ``decide``. A rule that
    may hold is a branch: the walk records what it would decide and goes on
    with the case where it does not hold.
    """
    outcomes = []
    verdicts = set()
    done = False
    for rule in rules:
        if done:
            outcomes.append(RuleOutcome(rule, None, None, reached=False))
            continue
        holds = rule.applies(facts)
        if holds is False:
            outcomes.append(RuleOutcome(rule, False, None, reached=True))
            continue
        skips = rule.skips(facts)
        outcomes.append(RuleOutcome(rule, holds, skips, reached=True))
        verdicts.add(skips)
        if holds:
            done = True
    if verdicts == {True}:
        verdict = "skipped"
    elif verdicts == {False}:
        verdict = "required"
    else:
        verdict = "depends"
    return Simulation(verdict, tuple(outcomes))
