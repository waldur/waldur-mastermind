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
    HELD_BY_PROJECT_CREDIT = "held_by_project_credit"
    HELD_BY_CUSTOMER_CREDIT = "held_by_customer_credit"
    # Triggered.
    CREDIT_EXHAUSTED = "credit_exhausted"
    NO_CREDIT = "no_credit"
    CREDIT_IGNORED = "credit_ignored"

    CHOICES = (
        (COST_BELOW_LIMIT, "Cost below limit"),
        (HELD_BY_PROJECT_CREDIT, "Held by project credit"),
        (HELD_BY_CUSTOMER_CREDIT, "Held by organization credit"),
        (CREDIT_EXHAUSTED, "Credit exhausted"),
        (NO_CREDIT, "No credit"),
        (CREDIT_IGNORED, "Credit ignored"),
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
    """The balance Gate 2 compared: a project allocation or the organization
    credit, whichever is lower."""

    PROJECT = "project"
    CUSTOMER = "customer"


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
    #: The live credit balance Gate 2 compared; None when Gate 2 did not run.
    credit_value_compared: decimal.Decimal | None = None
    #: Whose balance `credit_value_compared` is (a `CreditSources` value), so
    #: the API can withhold an organization balance from project roles. Not
    #: part of the event context.
    credit_source: str | None = None

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
