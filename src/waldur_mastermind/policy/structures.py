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
