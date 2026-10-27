import decimal
from collections.abc import Callable, Collection
from dataclasses import dataclass

from . import enums


@dataclass
class PolicyAction:
    action_type: enums.PolicyActionTypes
    method: Callable
    reset_method: Callable | None = None
    options_validator: Callable | None = None
    ignored_fields: Collection[str] | None = None


class PolicyGates:
    """Which check decided an estimated-cost policy's evaluation."""

    # Not triggered.
    COST_BELOW_LIMIT = "cost_below_limit"
    # Triggered.
    COST_OVER_LIMIT = "cost_over_limit"
    NO_CREDIT = "no_credit"
    CREDIT_IGNORED = "credit_ignored"
    # No longer produced: they came from the credit-balance gate that #784
    # removed. Kept in CHOICES so that the generated clients still accept them
    # until they stop handling them, and old events still carry them.
    HELD_BY_PROJECT_CREDIT = "held_by_project_credit"
    HELD_BY_CUSTOMER_CREDIT = "held_by_customer_credit"
    CREDIT_EXHAUSTED = "credit_exhausted"

    CHOICES = (
        (COST_BELOW_LIMIT, "Cost below limit"),
        (COST_OVER_LIMIT, "Cost over limit"),
        (NO_CREDIT, "No credit"),
        (CREDIT_IGNORED, "Credit ignored"),
        (HELD_BY_PROJECT_CREDIT, "Held by project credit (no longer produced)"),
        (HELD_BY_CUSTOMER_CREDIT, "Held by organization credit (no longer produced)"),
        (CREDIT_EXHAUSTED, "Credit exhausted (no longer produced)"),
    )


class EvaluationTriggers:
    """What started a policy evaluation, recorded as `trigger` on the events it
    emits."""

    POLICY_SAVED = "policy_saved"
    # Not an evaluation: a deleted policy's actions being reset.
    POLICY_DELETED = "policy_deleted"
    INVOICE_ITEM_SAVED = "invoice_item_saved"
    CREDIT_CHANGED = "credit_changed"
    USAGE_REPORTED = "usage_reported"
    PERIODIC_SWEEP = "periodic_sweep"
    # The sweep re-applying an already-fired policy's idempotent actions.
    RECONCILE = "reconcile"
    # Not an evaluation: the threshold actions of an already-fired policy, run
    # when an observed resource is saved.
    RESOURCE_SAVED = "resource_saved"


class CreditSources:
    """Whose live balance a policy reads: a project allocation or the
    organization credit, whichever is lower."""

    PROJECT = "project"
    CUSTOMER = "customer"


@dataclass(frozen=True)
class CostBreakdown:
    """What an estimated-cost policy's `current_cost` is made of:
    `cost_before_credit - credit_drawn - credit_pending`."""

    #: The period's invoiced cost before any credit.
    cost_before_credit: decimal.Decimal
    #: Credit compensation already written on the period's invoices.
    credit_drawn: decimal.Decimal
    #: Credit this month will still draw that is not written yet.
    credit_pending: decimal.Decimal
    #: The part of `current_cost` from months before the current one: cost
    #: after credit that credit added now cannot pay, since a top-up pays only
    #: the current month. 0 for a one-month period.
    earlier_months: decimal.Decimal = decimal.Decimal(0)


@dataclass(frozen=True)
class PolicyDecision:
    """What an estimated-cost policy evaluation decided, and on which figures.

    Recorded so that an action's event shows the inputs that actually fired or
    reset it, instead of balances re-read after the fact, which can contradict
    the decision.
    """

    triggered: bool
    gate: str
    #: The cost compared against `limit_cost`, net of `credit_deduction`.
    evaluated_cost: decimal.Decimal
    credit_deduction: decimal.Decimal
    limit_cost: int
    period: int
    use_credit: bool
    #: The credit balance compared against `limit_cost`. Always None since
    #: #784 removed that comparison; kept so the API field keeps its shape.
    #: The event context leaves it out, as it does every None.
    credit_value_compared: decimal.Decimal | None = None

    def as_event_context(self) -> dict:
        def amount(value):
            if value is None:
                return None
            return str(decimal.Decimal(value).quantize(decimal.Decimal("0.01")))

        return {
            "triggered": self.triggered,
            "gate": self.gate,
            "evaluated_cost": amount(self.evaluated_cost),
            "credit_deduction": amount(self.credit_deduction),
            "limit_cost": self.limit_cost,
            "period": self.period,
            "use_credit": self.use_credit,
            "credit_value_compared": amount(self.credit_value_compared),
        }
