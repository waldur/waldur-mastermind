"""Price a change to a customer's resources the way the invoice will bill it.

A cost estimate built from plan prices (``Plan.get_estimate``) misses what the
invoice applies: the plan unit (a per-day plan is billed per day of the month),
volume discounts aggregated over the whole customer, promotion campaigns, and
the whole-GB rounding of OpenStack limits. Re-implementing those rules beside
the billing code would drift from it.

So an estimate runs the billing code itself, inside a transaction it rolls
back. It runs once for the current state and once for the changed state; the
difference is the cost of the change. Each run:

- places the orders the change needs today, as the real orders would: an added
  resource and a plan switch are billed into the current month's invoice, so
  their one-off fees (one-time components, one-time limits, the plan-switch
  fee) are priced with the campaigns and discounts of the current month;
- closes the current month as the monthly task will, when the customer has
  credit, and bills and closes each month up to the reference one, so that
  the credit left for the reference month is known;
- creates the customer's invoice for a reference month and bills every
  billable resource into it, with the change applied in memory, then applies
  the volume discounts and reads the items.

The main figure is that of a whole month in the steady state. A cost billed
once a quarter or once a year (a quarterly or annual limit, the fixed fee of a
plan priced per quarter) is billed for its whole period and spread over its
months. Beside it, ``invoice_total`` is what the reference month's own
invoice bills: such a cost in full in the month it falls due, nothing of it
in the other months. What the orders of the change bill into the current
month (the rest of the month of an added resource, a year billed ahead, the
one-off fees) is ``current_month_total``. Usage-based components are not
estimated, since there is no usage to bill; they are listed with the reason.

The figures (``monthly_total``, ``invoice_total``, ``yearly_total``,
``one_off_total``, ``current_month_total``) are gross: before credit and
excluding VAT. Next to them a simulation reports what the credit would pay of
the reference month (``credit_compensation``) and what it would draw
(``credit_drawn``, more under a minimal consumption), computed by the
compensation the month close runs, the month's total net of it and the VAT of
the invoice.

Signal handlers that would act on the simulated items (cost-policy
evaluation, price-estimate updates, carried-over usage) check
``invoices.simulation.is_active()`` and stand aside while a simulation runs.
"""

from __future__ import annotations

import dataclasses
import datetime
import traceback
from calendar import monthrange
from collections import defaultdict
from decimal import Decimal
from functools import partial

from django.conf import settings
from django.db import IntegrityError, transaction
from django.db.models import F, Prefetch, Q
from django.utils import timezone

from waldur_core.core import utils as core_utils
from waldur_core.core.middleware import skip_side_effects
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.common.utils import quantize_price
from waldur_mastermind.invoices import compensations, simulation, vat
from waldur_mastermind.invoices import models as invoice_models
from waldur_mastermind.invoices import tasks as invoice_tasks
from waldur_mastermind.marketplace import billing_discount, billing_mode
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.billing import MarketplaceBillingService
from waldur_mastermind.marketplace.billing_limit import LimitPeriodProcessor
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    LimitPeriods,
    OfferingStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)

RESIZE = "resize"
SWITCH = "switch"
ADD = "add"
REMOVE = "remove"
ACTIONS = (RESIZE, SWITCH, ADD, REMOVE)

ONE_OFF_TYPES = (BillingTypes.ONE_TIME, BillingTypes.ON_PLAN_SWITCH)
CREATE = OrderTypes.CREATE
UPDATE = OrderTypes.UPDATE

DAYS_IN_YEAR = 365
HOURS_IN_YEAR = 365 * 24

Units = invoice_models.Units


class SimulationError(ValueError):
    """The change cannot be simulated as given."""


@dataclasses.dataclass
class Change:
    """One change to a customer's resources.

    - ``resize``: ``resource`` gets ``limits`` (merged into its current limits).
    - ``switch``: ``resource`` moves to ``plan``.
    - ``add``: a new resource on ``plan`` in ``project`` with ``limits``,
      ending on ``end_date`` if given. A prepaid component is charged for the
      whole period up to the end date; without one it is not estimated.
    - ``remove``: ``resource`` is no longer billed.
    """

    action: str
    resource: marketplace_models.Resource | None = None
    plan: marketplace_models.Plan | None = None
    limits: dict | None = None
    project: Project | None = None
    name: str = ""
    end_date: datetime.date | None = None

    def __post_init__(self):
        if self.action not in ACTIONS:
            raise SimulationError(f"Unknown action {self.action!r}.")
        if self.action in (RESIZE, SWITCH, REMOVE) and self.resource is None:
            raise SimulationError(f"A {self.action} needs a resource.")
        if self.action == RESIZE and not self.limits:
            raise SimulationError("A resize needs new limits.")
        if self.action in (SWITCH, ADD) and self.plan is None:
            raise SimulationError(f"A {self.action} needs a plan.")
        if self.action == ADD and self.project is None:
            raise SimulationError("An add needs a project.")
        if self.end_date is not None and self.action != ADD:
            raise SimulationError("Only an add takes an end date.")
        if (
            self.action == SWITCH
            and self.plan is not None
            and self.resource is not None
            and self.plan.offering_id != self.resource.offering_id
        ):
            raise SimulationError("A switch must stay within the offering.")


@dataclasses.dataclass
class Line:
    """One invoice item the simulated month would contain."""

    resource_uuid: str
    resource_name: str
    offering_name: str
    plan_name: str
    component_type: str
    billing_type: str
    unit: str
    quantity: Decimal
    unit_price: Decimal
    price: Decimal
    is_discount: bool
    is_one_off: bool
    is_added: bool
    # A cost billed once a quarter or once a year is billed for its period.
    months: int = 1
    # The unit the line's price is counted in over the month: a discount
    # line is a single quantity, but it scales with the line it discounts.
    period_unit: str = ""
    # Whether the reference month's invoice bills the line. A line billed
    # for a longer period is due in one month of it only: a quarterly cost
    # in the first month of the quarter, an annual one in the month of the
    # resource's anniversary.
    is_due: bool = True

    @property
    def invoiced_price(self) -> Decimal:
        """What the reference month's own invoice bills of the line."""
        return self.price if self.is_due else Decimal(0)

    @property
    def monthly_price(self) -> Decimal:
        """The line's share of one month."""
        return self.price / self.months

    @property
    def key(self):
        return (self.resource_uuid, self.component_type, self.is_discount)


@dataclasses.dataclass
class Simulation:
    """The items of one simulated month.

    ``monthly_total``, ``yearly_total`` and ``one_off_total`` are gross:
    before credit and excluding VAT. ``monthly_total`` is the cost of a month
    in the steady state, with a cost billed for a quarter or a year spread
    over its months; ``invoice_total`` is what the reference month's own
    invoice bills, with such a cost in full in the month it falls due and
    not at all in the others. ``current_month_total`` is what the orders of
    the change bill into the current month.

    ``credit_compensation`` is what the customer's credit would pay of the
    month, and ``monthly_total_net`` the month's total after it;
    ``credit_drawn`` is what the month takes from the credit, which a
    minimal consumption can make more than it pays. ``monthly_tax`` is the
    VAT of the net total at the invoice's ``tax_percent``.
    """

    year: int
    month: int
    lines: list[Line]
    not_estimated: list[dict]
    # The resources the additions created, in the order of their changes.
    added: list[str] = dataclasses.field(default_factory=list)
    tax_percent: Decimal = Decimal(0)
    credit_compensation: Decimal = Decimal(0)
    credit_drawn: Decimal = Decimal(0)
    # What the orders of the change bill into the current month, one-off
    # fees included: the rest of the month of an added resource, a year
    # billed ahead.
    current_month_lines: list[Line] = dataclasses.field(default_factory=list)
    # What the figures leave out, beyond the components in not_estimated.
    notes: list[str] = dataclasses.field(default_factory=list)

    @property
    def recurring(self) -> list[Line]:
        return [line for line in self.lines if not line.is_one_off]

    @property
    def one_off(self) -> list[Line]:
        return [line for line in self.lines if line.is_one_off]

    @property
    def monthly_total(self) -> Decimal:
        """The recurring cost of a month, before credit and excluding VAT."""
        return quantize_price(
            sum((line.monthly_price for line in self.recurring), Decimal(0))
        )

    @property
    def invoice_total(self) -> Decimal:
        """The recurring cost the reference month's invoice bills, before
        credit and excluding VAT."""
        return quantize_price(
            sum((line.invoiced_price for line in self.recurring), Decimal(0))
        )

    @property
    def current_month_total(self) -> Decimal:
        """What the orders bill into the current month, before credit and
        excluding VAT."""
        return quantize_price(
            sum((line.price for line in self.current_month_lines), Decimal(0))
        )

    @property
    def monthly_total_net(self) -> Decimal:
        """The month's cost after credit, excluding VAT."""
        return quantize_price(self.monthly_total - self.credit_compensation)

    @property
    def monthly_tax(self) -> Decimal:
        """The VAT of the month's cost after credit, as the invoice adds it."""
        return self.monthly_total_net * self.tax_percent / 100

    @property
    def one_off_total(self) -> Decimal:
        """The one-off fees, before credit and excluding VAT."""
        return quantize_price(sum((line.price for line in self.one_off), Decimal(0)))

    @property
    def yearly_total(self) -> Decimal:
        """The recurring cost over a year, before credit and excluding VAT."""
        return self.yearly_of(self.recurring)

    def yearly_of(self, lines: list[Line]) -> Decimal:
        """The recurring cost of ``lines`` over a year: per-day and per-hour
        items are scaled from this month's days or hours to a 365-day year,
        so the figure does not depend on the reference month; quarterly and
        annual items count as often as they recur in a year."""
        days = monthrange(self.year, self.month)[1]
        total = Decimal(0)
        for line in lines:
            unit = line.period_unit or line.unit
            if line.months > 1:
                total += line.price * 12 / line.months
            elif unit == Units.PER_DAY:
                total += line.price / days * DAYS_IN_YEAR
            elif unit == Units.PER_HOUR:
                total += line.price / (days * 24) * HOURS_IN_YEAR
            else:
                total += line.price * 12
        return quantize_price(total)


@dataclasses.dataclass
class Estimate:
    current: Simulation
    changed: Simulation

    @property
    def monthly_difference(self) -> Decimal:
        """Before credit and excluding VAT."""
        return self.changed.monthly_total - self.current.monthly_total

    @property
    def monthly_net_difference(self) -> Decimal:
        """After credit, excluding VAT."""
        return self.changed.monthly_total_net - self.current.monthly_total_net

    @property
    def yearly_difference(self) -> Decimal:
        """Before credit and excluding VAT."""
        return self.changed.yearly_total - self.current.yearly_total

    @property
    def invoice_difference(self) -> Decimal:
        """What the change adds to the reference month's own invoice, before
        credit and excluding VAT."""
        return self.changed.invoice_total - self.current.invoice_total

    @property
    def current_month_total(self) -> Decimal:
        """What the orders of the change bill into the current month."""
        return self.changed.current_month_total

    @property
    def one_off_total(self) -> Decimal:
        return self.changed.one_off_total

    def differences(self) -> list[dict]:
        """Recurring cost per resource and component, before and after."""
        before = defaultdict(Decimal)
        after = defaultdict(Decimal)
        names = {}
        for line in self.current.recurring:
            before[line.key] += line.monthly_price
            names[line.key] = line
        for line in self.changed.recurring:
            after[line.key] += line.monthly_price
            names[line.key] = line
        rows = []
        for key in sorted(set(before) | set(after), key=lambda k: tuple(map(str, k))):
            difference = after[key] - before[key]
            if not difference and before[key] == after[key] == 0:
                continue
            line = names[key]
            rows.append(
                {
                    "resource_uuid": line.resource_uuid,
                    "resource_name": line.resource_name,
                    "component_type": line.component_type,
                    "billing_type": line.billing_type,
                    "unit": line.unit,
                    "is_discount": line.is_discount,
                    "current": quantize_price(before[key]),
                    "changed": quantize_price(after[key]),
                    "difference": quantize_price(difference),
                }
            )
        return rows


def _first_of_month(month: datetime.date) -> datetime.date:
    """Any day stands for its whole month, as an invoice covers one."""
    return datetime.date(month.year, month.month, 1)


def _next_month(month: datetime.date) -> datetime.date:
    return (
        datetime.date(month.year + 1, 1, 1)
        if month.month == 12
        else datetime.date(month.year, month.month + 1, 1)
    )


def _current_month() -> datetime.date:
    # Local time, as create_monthly_invoices decides which month is current.
    return timezone.localdate().replace(day=1)


def default_month(now=None) -> datetime.date:
    """The month after the current one: its invoice does not exist yet."""
    now = timezone.localtime(now or timezone.now())
    return (core_utils.month_end(now) + datetime.timedelta(days=1)).date()


def estimate(
    customer: Customer, changes: list[Change], month: datetime.date | None = None
) -> Estimate:
    """The monthly cost of ``customer`` before and after ``changes``."""
    month = _first_of_month(month or default_month())
    return Estimate(
        current=simulate(customer, [], month),
        changed=simulate(customer, changes, month),
    )


def simulate(
    customer: Customer, changes: list[Change], month: datetime.date | None = None
) -> Simulation:
    """Bill ``customer`` for ``month`` with ``changes`` applied, then undo it."""
    month = _first_of_month(month or default_month())
    _check_month(customer, month)
    for change in changes:
        if change.resource is not None and (
            change.resource.project.customer_id != customer.id
        ):
            raise SimulationError(
                f"Resource {change.resource.uuid.hex} belongs to another customer."
            )
        if change.project is not None and change.project.customer_id != customer.id:
            raise SimulationError("The project belongs to another customer.")
        if change.plan is not None and (
            change.plan.archived
            or change.plan.offering.state == OfferingStates.ARCHIVED
        ):
            raise SimulationError(f"Plan {change.plan.name} cannot be ordered.")
        if (
            change.action == SWITCH
            and change.resource is not None
            and change.plan is not None
            and change.plan.id == change.resource.plan_id
        ):
            raise SimulationError("The resource is already on this plan.")

    if _is_invoiced(customer, month):
        raise _already_invoiced(month)

    # A copy: the rolled-back billing must not leave its credit and invoices
    # cached on the caller's object, nor read what it has cached.
    customer = Customer.objects.get(pk=customer.pk)
    with simulation.active(), skip_side_effects():
        with transaction.atomic():
            try:
                return _simulate(customer, changes, month)
            except ValueError as e:
                if isinstance(e, SimulationError) or not _raised_by_campaign(e):
                    raise
                raise SimulationError(
                    "The discount period of a promotion campaign of these "
                    "resources cannot be computed: it runs past the end of the "
                    "year."
                ) from e
            finally:
                transaction.set_rollback(True)


def _check_month(customer, month):
    """Refuse what the monthly task would not invoice."""
    if month < _current_month():
        raise SimulationError(
            f"{month.year}-{month.month:02d} is in the past; pick the current "
            "month or a later one."
        )
    if customer.archived:
        raise SimulationError(
            f"Organization {customer.name} is archived: it is not invoiced."
        )
    if settings.WALDUR_CORE["ENABLE_ACCOUNTING_START_DATE"]:
        start = core_utils.month_start(
            timezone.make_aware(datetime.datetime(month.year, month.month, 1))
        )
        accounting_start = customer.accounting_start_date
        if accounting_start is None or accounting_start >= start:
            raise SimulationError(
                f"Organization {customer.name} is not invoiced for "
                f"{month.year}-{month.month:02d}: its accounting starts on "
                f"{accounting_start.date() if accounting_start else 'no date'}."
            )


def _is_invoiced(customer, month) -> bool:
    return invoice_models.Invoice.objects.filter(
        customer=customer, year=month.year, month=month.month
    ).exists()


def _already_invoiced(month) -> SimulationError:
    return SimulationError(
        f"The invoice for {month.year}-{month.month:02d} already exists; "
        "pick a month that has not been invoiced yet."
    )


def _raised_by_campaign(error) -> bool:
    """Whether ``error`` comes from Campaign.get_discount_for_resource, which
    builds an invalid month when a campaign's discount runs past December."""
    return any(
        frame.name == "get_discount_for_resource"
        for frame in traceback.extract_tb(error.__traceback__)
    )


def _billable_resources(customer):
    return list(
        marketplace_models.Resource.objects.filter(project__customer=customer)
        .exclude(state__in=[ResourceStates.CREATING, ResourceStates.TERMINATED])
        .filter(offering__billable=True)
        .select_related("offering", "plan", "project", "project__customer")
        .prefetch_related("plan__components__component")
        .distinct()
        .order_by("pk")
    )


def _simulate(customer, changes, month) -> Simulation:
    start = core_utils.month_start(
        timezone.make_aware(datetime.datetime(month.year, month.month, 1))
    )
    end = core_utils.month_end(start)
    in_current_month = month == _current_month()

    not_estimated = []
    resources = {}
    for resource in _billable_resources(customer):
        if resource.end_date and resource.end_date < month:
            # Terminated by its end date before the month starts.
            continue
        resources[resource.pk] = resource
    switched, added, removed = set(), set(), set()
    added_uuids = []
    for change in changes:
        if change.action == ADD:
            resource = _add_resource(change)
            resources[resource.pk] = resource
            added.add(resource.pk)
            added_uuids.append(resource.uuid.hex)
            continue
        if change.resource.pk in removed:
            raise SimulationError(
                f"Resource {change.resource.uuid.hex} is removed by an earlier change."
            )
        resource = resources.get(change.resource.pk)
        if resource is None:
            raise SimulationError(
                f"Resource {change.resource.uuid.hex} is not billed: it is "
                "terminated, still being created, past its end date, or on an "
                "offering that is not billable."
            )
        if change.action == REMOVE:
            del resources[resource.pk]
            removed.add(resource.pk)
        elif change.action == RESIZE:
            not_estimated += _unpriced_limit_changes(resource, change.limits)
            resource.limits = {**(resource.limits or {}), **change.limits}
        elif change.action == SWITCH:
            resource.plan = change.plan
            switched.add(resource.pk)

    one_off_items, order_items = [], []
    if not in_current_month:
        # The orders are placed today: they bill the current month.
        one_off_items, order_items, rows = _bill_orders(resources, added, switched)
        not_estimated += rows
        _close_current_month(customer)

    plans = _plans(resources.values(), future_prices=not in_current_month)
    for resource in resources.values():
        if resource.plan_id:
            resource.plan = plans[resource.plan_id]
    if not in_current_month:
        _close_months_before(customer, resources.values(), month)

    invoice = _create_invoice(customer, month)
    periods = {}
    due = set()
    period_invoices = {}
    for resource in resources.values():
        if resource.end_date and resource.end_date < month:
            # An addition that ends before the month: its order bills it.
            continue
        resource_end = _billed_until(resource, end)
        if resource_end != end:
            not_estimated.append(
                {
                    "resource_uuid": resource.uuid.hex,
                    "resource_name": resource.name,
                    "component_type": "",
                    "unit_price": Decimal(0),
                    "reason": f"ends on {resource.end_date}: billed up to that "
                    "day only",
                }
            )
        MarketplaceBillingService.bill_resource(resource, invoice, start, resource_end)
        periods.update(
            _bill_long_periods(
                resource, invoice, start, resource_end, period_invoices, due
            )
        )

    if in_current_month:
        # No order can come before the month: it is the current one.
        for pk in added:
            one_off_items += _bill_one_off(resources[pk], invoice, start, end, CREATE)
        for pk in switched:
            one_off_items += _bill_one_off(resources[pk], invoice, start, end, UPDATE)
        one_off_items, rows = _drop_unpriced_prepaid(one_off_items)
        not_estimated += rows

    billing_discount.apply_aggregated_volume_discounts(invoice, estimate=True)
    for period_invoice in period_invoices.values():
        billing_discount.apply_aggregated_volume_discounts(
            period_invoice, estimate=True
        )

    one_off_pks = {item.pk for item in one_off_items}
    items = _items(invoice, period_invoices.values(), periods)
    lines = _read_lines(items, one_off_pks, added, periods, due)
    current_month_lines = []
    if not in_current_month and order_items:
        current_month_lines = _read_lines(
            _with_discounts(order_items), one_off_pks, added
        )
        lines += [line for line in current_month_lines if line.is_one_off]
    else:
        current_month_lines = [line for line in lines if line.is_one_off]
    compensation, drawn = _credit_compensation(
        customer, invoice, periods, one_off_pks, month
    )

    return Simulation(
        year=month.year,
        month=month.month,
        lines=lines,
        not_estimated=not_estimated + _not_estimated(resources.values()),
        added=added_uuids,
        tax_percent=invoice.tax_percent,
        credit_compensation=compensation,
        credit_drawn=drawn,
        current_month_lines=current_month_lines,
    )


def _billed_until(resource, end):
    """The end of the month, or the resource's end date within it: it is
    terminated then."""
    if resource.end_date and resource.end_date <= end.date():
        return timezone.make_aware(
            datetime.datetime.combine(resource.end_date, datetime.time.max)
        )
    return end


def _create_invoice(customer, month):
    try:
        with transaction.atomic():
            return invoice_models.Invoice.objects.create(
                customer=customer, year=month.year, month=month.month
            )
    except IntegrityError:
        # The month's invoice was created meanwhile. Only this one: an
        # integrity error of anything else, a handler of the new invoice
        # among them, is a fault, not a month to pick again.
        if invoice_models.Invoice.objects.filter(
            customer=customer, year=month.year, month=month.month
        ).exists():
            raise _already_invoiced(month)
        raise


def _plans(resources, future_prices) -> dict:
    """The plans in use, with their components loaded. The monthly task
    copies scheduled prices to the current ones before it bills a new month;
    the copy is made here on the loaded components only, so that the plan
    components, which every customer's billing reads, are neither written
    nor locked."""
    plan_ids = {resource.plan_id for resource in resources if resource.plan_id}
    plans = marketplace_models.Plan.objects.filter(id__in=plan_ids).prefetch_related(
        Prefetch(
            "components",
            queryset=marketplace_models.PlanComponent.objects.select_related(
                "component"
            ),
        )
    )
    result = {}
    for plan in plans:
        if future_prices:
            for plan_component in plan.components.all():
                if plan_component.future_price is not None:
                    plan_component.price = plan_component.future_price
        result[plan.id] = plan
    return result


def _new_items(resource, plan_component, bill) -> list[int]:
    """Run ``bill`` and return the primary keys of the items it created for
    ``resource`` and ``plan_component``."""
    items = invoice_models.InvoiceItem.objects.filter(
        resource=resource, plan_component=plan_component
    )
    before = set(items.values_list("pk", flat=True))
    bill()
    return sorted(set(items.values_list("pk", flat=True)) - before)


LONG_PERIODS = {LimitPeriods.QUARTERLY: 3, LimitPeriods.ANNUAL: 12}
PERIOD_NAMES = {LimitPeriods.QUARTERLY: "quarter", LimitPeriods.ANNUAL: "year"}
QUARTER_MONTHS = 3
QUARTER_START_MONTHS = (1, 4, 7, 10)


def _bill_long_periods(resource, invoice, start, end, period_invoices, due) -> dict:
    """A cost billed once a period is billed in one month of the period, so
    a single month shows either all of it or none. Bill the whole period
    containing the month instead, and remember how many months it covers so
    that each month carries its share:

    - quarterly and annual limits, the annual one on the invoice of the
      resource's anniversary month (see ``_anniversary_invoice``);
    - the fixed fee of a plan priced per quarter, which the first month of a
      quarter bills.

    The items billed in the month the real invoice bills them are added to
    ``due``.
    """
    periods = {}
    plan = resource.plan
    if not plan:
        return periods
    for plan_component in plan.components.all():
        component = plan_component.component
        if not component:
            continue
        effective = billing_mode.resolve_component(component, plan)
        if effective.billing_type == BillingTypes.LIMIT:
            months = LONG_PERIODS.get(effective.limit_period)
            if not months:
                continue
            target = invoice
            is_due = start.month in QUARTER_START_MONTHS
            if effective.limit_period == LimitPeriods.ANNUAL:
                target = _anniversary_invoice(resource, invoice, period_invoices)
                is_due = target == invoice
            bill = partial(
                LimitPeriodProcessor.bill_period,
                resource,
                plan_component,
                target,
                start,
            )

        elif effective.billing_type == BillingTypes.FIXED and (
            plan.unit == Units.PER_QUARTER
        ):
            months = QUARTER_MONTHS
            is_due = start.month in QUARTER_START_MONTHS
            bill = partial(
                MarketplaceBillingService.bill_component,
                resource,
                plan_component,
                invoice,
                core_utils.get_quarter_start(start),
                min(core_utils.get_quarter_end(start), end),
                None,
            )

        else:
            continue
        invoice.items.filter(resource=resource, plan_component=plan_component).delete()
        for pk in _new_items(resource, plan_component, bill):
            periods[pk] = months
            if is_due:
                due.add(pk)
    return periods


def _anniversary_invoice(resource, invoice, period_invoices):
    """An annual limit is billed in the month of the resource's creation, on
    an invoice with the other annual items of that month only, so a volume
    discount aggregated over the customer adds up the resources billed in the
    same month. Bill it on the invoice of the next such month, no earlier
    than the reference one: a month after the current one, which has no
    invoice yet and gets one inside the rolled-back transaction."""
    month = resource.created.month
    year = invoice.year if month >= invoice.month else invoice.year + 1
    if (year, month) == (invoice.year, invoice.month):
        return invoice
    if (year, month) not in period_invoices:
        period_invoices[(year, month)], _ = (
            invoice_models.Invoice.objects.get_or_create(
                customer=invoice.customer, year=year, month=month
            )
        )
    return period_invoices[(year, month)]


def _items(invoice, period_invoices, periods) -> list:
    """The reference month's items: those of its invoice and the annual items
    billed on the invoices of other months, with their discount lines."""
    related = (
        "resource",
        "resource__offering",
        "plan_component",
        "plan_component__plan",
        "plan_component__component",
    )
    items = list(invoice.items.select_related(*related).order_by("pk"))
    for period_invoice in period_invoices:
        own = list(
            period_invoice.items.filter(pk__in=periods)
            .select_related(*related)
            .order_by("pk")
        )
        items += own
        items += list(
            period_invoice.items.filter(
                details__discount_of_item__in=[item.uuid.hex for item in own]
            )
            .select_related(*related)
            .order_by("pk")
        )
    return items


def _unpriced_limit_changes(resource, limits) -> list[dict]:
    """A change to a total limit or a prepaid one-time limit is charged at
    once, as a supplement for the rest of the period, which a steady month
    does not show. A change to a quarterly or annual limit amends the item of
    the period's first month, whose invoice may already be closed: the new
    limit is priced from the next period on."""
    result = []
    if not resource.plan:
        return result
    for plan_component in resource.plan.components.all():
        component = plan_component.component
        if not component or component.type not in limits:
            continue
        if limits[component.type] == (resource.limits or {}).get(component.type):
            continue
        effective = billing_mode.resolve_component(component, resource.plan)
        is_limit = effective.billing_type == BillingTypes.LIMIT
        if (is_limit and effective.limit_period == LimitPeriods.TOTAL) or (
            effective.billing_type == BillingTypes.ONE_TIME and effective.is_prepaid
        ):
            reason = (
                "changing this limit is charged at once for the rest of its "
                "period; that charge is not estimated"
            )
        elif is_limit and effective.limit_period in LONG_PERIODS:
            reason = (
                "changing this limit amends the charge of the current "
                f"{PERIOD_NAMES[effective.limit_period]}, billed on the invoice "
                "of its first month; that amendment is not estimated"
            )
        else:
            continue
        result.append(
            {
                "resource_uuid": resource.uuid.hex,
                "resource_name": resource.name,
                "component_type": component.type,
                "unit_price": plan_component.price,
                "reason": reason,
            }
        )
    return result


def _add_resource(change: Change) -> marketplace_models.Resource:
    """A resource row for an addition, created now as the order would create
    it, with the creation order that brings it: billing looks for that order
    to apply a promotion campaign, which takes the resources created while
    it runs. ``bulk_create`` saves both without signals, so nothing reacts to
    a resource that will never exist."""
    plan = change.plan
    if plan is None:
        raise SimulationError("An add needs a plan.")
    resource = marketplace_models.Resource(
        name=change.name or "new resource",
        project=change.project,
        offering=plan.offering,
        plan=plan,
        limits=dict(change.limits or {}),
        end_date=change.end_date,
        state=ResourceStates.OK,
    )
    (resource,) = marketplace_models.Resource.objects.bulk_create([resource])
    marketplace_models.Order.objects.bulk_create(
        [
            marketplace_models.Order(
                type=CREATE,
                state=OrderStates.DONE,
                resource=resource,
                project=change.project,
                offering=plan.offering,
                plan=plan,
                limits=dict(change.limits or {}),
            )
        ]
    )
    resource.refresh_from_db()
    return resource


def _is_one_off(item) -> bool:
    """Whether an item an order created is charged once: a one-time
    component or limit, the plan-switch fee."""
    plan_component = item.plan_component
    if not plan_component or not plan_component.component:
        return False
    effective = billing_mode.resolve_component(
        plan_component.component, plan_component.plan
    )
    return effective.billing_type in ONE_OFF_TYPES or (
        effective.billing_type == BillingTypes.LIMIT
        and effective.limit_period == LimitPeriods.TOTAL
    )


def _bill_orders(resources, added, switched) -> tuple[list, list, list[dict]]:
    """Bill the orders of the additions and plan switches into the current
    month as the orders themselves will: the current month's campaigns and
    prices apply to their one-off fees. Returns the one-off items, every item
    the orders bill (the one-off ones, the rest of the month of an added
    resource, a quarter or a year billed ahead), and the components that
    cannot be priced."""
    pks = added | switched
    if not pks:
        return [], [], []
    items = invoice_models.InvoiceItem.objects.filter(resource_id__in=pks)
    before = set(items.values_list("pk", flat=True))
    for pk in sorted(added):
        MarketplaceBillingService.handle_resource_creation(resources[pk])
    for pk in sorted(switched):
        MarketplaceBillingService.handle_plan_change(resources[pk])
    billed = list(
        items.exclude(pk__in=before)
        .select_related(
            "invoice", "resource", "plan_component__plan", "plan_component__component"
        )
        .order_by("pk")
    )
    candidates = [item for item in billed if _is_one_off(item)]
    one_off, rows = _drop_unpriced_prepaid(candidates)
    dropped = {item.pk for item in candidates} - {item.pk for item in one_off}
    billed = [item for item in billed if item.pk not in dropped]
    for invoice in {item.invoice for item in billed}:
        billing_discount.apply_aggregated_volume_discounts(invoice, estimate=True)
    return one_off, billed, rows


def _drop_unpriced_prepaid(items) -> tuple[list, list[dict]]:
    """A prepaid component is charged for the resource's whole period, up to
    its end date. Without one the order charges a single month, which is not
    what the resource will cost: leave it out and say so."""
    kept, rows = [], []
    for item in items:
        plan_component = item.plan_component
        effective = billing_mode.resolve_component(
            plan_component.component, plan_component.plan
        )
        if (
            effective.billing_type == BillingTypes.ONE_TIME
            and effective.is_prepaid
            and not item.resource.end_date
        ):
            rows.append(
                {
                    "resource_uuid": item.resource.uuid.hex,
                    "resource_name": item.resource.name,
                    "component_type": plan_component.component.type,
                    "unit_price": plan_component.price,
                    "reason": "prepaid for the resource's whole period; give "
                    "an end date to estimate it",
                }
            )
            item.delete()
            continue
        kept.append(item)
    return kept, rows


def _with_discounts(items) -> list:
    """``items`` as they are now, with their discount lines; not the credit
    compensations, which copy the details of the item they offset."""
    return list(
        invoice_models.InvoiceItem.objects.filter(
            Q(pk__in=[item.pk for item in items])
            | Q(details__discount_of_item__in=[item.uuid.hex for item in items])
        )
        .filter(credit__isnull=True)
        .select_related(
            "resource",
            "resource__offering",
            "plan_component__plan",
            "plan_component__component",
        )
        .order_by("pk")
    )


def _has_credit(customer) -> bool:
    return invoice_models.CustomerCredit.objects.filter(customer=customer).exists()


def _close_current_month(customer):
    """Close the current month as the monthly task will, when the customer
    has credit: its compensation draws the credit first, and the months
    after it get what is left. A month before it still in its grace period
    is closed first, as the finalization will."""
    if not _has_credit(customer):
        return
    current = _current_month()
    for earlier in invoice_models.Invoice.objects.filter(
        customer=customer,
        state=invoice_models.Invoice.States.PENDING_FINALIZATION,
    ).order_by("year", "month"):
        _close(customer, earlier)
    invoice = invoice_models.Invoice.objects.filter(
        customer=customer,
        year=current.year,
        month=current.month,
        state__in=invoice_models.Invoice.States.MUTABLE_STATES,
    ).first()
    if invoice:
        _close(customer, invoice)


def _close_months_before(customer, resources, month):
    """Bill and close each month between the current one and ``month``, with
    the change applied, when the customer has credit: each draws its share,
    and the reference month gets what they leave. The months are billed by
    the rules of the real invoice, a quarter or a year in the month it falls
    due."""
    if not _has_credit(customer):
        return
    between = _next_month(_current_month())
    while between < month:
        start = core_utils.month_start(
            timezone.make_aware(datetime.datetime(between.year, between.month, 1))
        )
        end = core_utils.month_end(start)
        invoice = _create_invoice(customer, between)
        for resource in resources:
            if resource.end_date and resource.end_date < between:
                continue
            MarketplaceBillingService.bill_resource(
                resource, invoice, start, _billed_until(resource, end)
            )
        _close(customer, invoice)
        between = _next_month(between)


def _close(customer, invoice):
    """What the monthly task does to a month's invoice before the next one:
    its VAT rate, the volume discounts, the expiry of the credits due before
    the next month, the credit's compensation."""
    if vat.is_enabled():
        try:
            vat.update_invoice_tax_percent(invoice)
        except vat.VATError:
            pass
    billing_discount.apply_aggregated_volume_discounts(invoice)
    month = datetime.date(invoice.year, invoice.month, 1)
    invoice_tasks.expire_overdue_credits(_next_month(month), customer)
    invoice_tasks.process_invoice_credits(invoice)


def _credit_compensation(
    customer, invoice, periods, one_off_pks, month
) -> tuple[Decimal, Decimal]:
    """What the customer's credit pays of the reference month, and what the
    month draws from it, by the compensation the month close runs, on the
    month as estimated: a cost billed for a longer period counts its share
    of the month, and one-off fees are left out. A minimal consumption draws
    more than the month costs: the draw is the larger figure. Not written:
    the compensation is read, not saved."""
    if not _has_credit(customer):
        return Decimal(0), Decimal(0)
    invoice.items.filter(pk__in=one_off_pks).delete()
    for item in invoice_models.InvoiceItem.objects.filter(pk__in=periods):
        months = periods[item.pk]
        invoice_models.InvoiceItem.objects.filter(pk=item.pk).update(
            invoice=invoice, quantity=item.quantity / months
        )
        invoice_models.InvoiceItem.objects.filter(
            details__discount_of_item=item.uuid.hex
        ).update(invoice=invoice, unit_price=F("unit_price") / months)
    invoice_tasks.expire_overdue_credits(_next_month(month), customer)
    projection = compensations.MonthlyCompensation(customer, invoice=invoice)
    return (
        quantize_price(
            -sum((item.price for item in projection.compensations), Decimal(0))
        ),
        # Zero, an int, when the credit is spent before the month.
        quantize_price(Decimal(projection.total_compensation)),
    )


def _bill_one_off(resource, invoice, start, end, order_type) -> list:
    """Bill what the order itself charges: one-time components and one-time
    limits of a new resource, the switch fee of a plan change. Returns the
    items created."""
    pks = []
    for plan_component in resource.plan.components.all():
        offering_component = plan_component.component
        if not offering_component:
            continue
        effective = billing_mode.resolve_component(offering_component, resource.plan)
        if (
            effective.billing_type == BillingTypes.LIMIT
            and order_type == CREATE
            and effective.limit_period == LimitPeriods.TOTAL
        ):
            bill = LimitPeriodProcessor.process_creation
        elif effective.billing_type in ONE_OFF_TYPES:
            bill = MarketplaceBillingService.bill_component
        else:
            continue
        pks += _new_items(
            resource,
            plan_component,
            partial(bill, resource, plan_component, invoice, start, end, order_type),
        )
    return list(
        invoice_models.InvoiceItem.objects.filter(pk__in=pks)
        .select_related("resource", "plan_component__plan", "plan_component__component")
        .order_by("pk")
    )


def _read_lines(items, one_off_pks, added, periods=None, due=None) -> list[Line]:
    periods = periods or {}
    due = due or set()
    lines = []
    # A discount line is spread, scaled and counted like the line it
    # discounts.
    by_uuid = {item.uuid.hex: item for item in items}
    for item in items:
        details = item.details or {}
        plan_component = item.plan_component
        resource = item.resource
        is_discount = bool(details.get("is_discount"))
        discounted = (
            by_uuid.get(details.get("discount_of_item"), item) if is_discount else item
        )
        billing_type = ""
        if plan_component and plan_component.component:
            billing_type = billing_mode.resolve_component(
                plan_component.component, plan_component.plan
            ).billing_type
        lines.append(
            Line(
                resource_uuid=resource.uuid.hex if resource else "",
                resource_name=resource.name if resource else item.name,
                offering_name=resource.offering.name if resource else "",
                plan_name=plan_component.plan.name if plan_component else "",
                component_type=details.get("offering_component_type", "")
                or (plan_component.component.type if plan_component else ""),
                billing_type=billing_type,
                unit=item.unit,
                quantity=Decimal(item.quantity),
                unit_price=Decimal(item.unit_price),
                price=item.price,
                is_discount=is_discount,
                is_one_off=discounted.pk in one_off_pks,
                is_added=bool(resource and resource.pk in added),
                months=periods.get(discounted.pk, 1),
                period_unit=discounted.unit,
                is_due=discounted.pk not in periods or discounted.pk in due,
            )
        )
    return lines


def _not_estimated(resources) -> list[dict]:
    """Components the simulation cannot price: usage is billed as it is
    reported, and a simulated month has none."""
    result = []
    for resource in resources:
        if not resource.plan:
            continue
        for plan_component in resource.plan.components.all():
            component = plan_component.component
            if not component:
                continue
            effective = billing_mode.resolve_component(component, resource.plan)
            if effective.billing_type == BillingTypes.USAGE and plan_component.price:
                result.append(
                    {
                        "resource_uuid": resource.uuid.hex,
                        "resource_name": resource.name,
                        "component_type": component.type,
                        "unit_price": plan_component.price,
                        "reason": "usage-based: billed as usage is reported",
                    }
                )
    return result
