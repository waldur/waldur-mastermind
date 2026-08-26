import datetime
import decimal
import logging

from django.db import transaction
from django.db.models import Sum

from waldur_core.logging import event_logger
from waldur_core.logging.enums import EventType
from waldur_core.structure.models import Project
from waldur_mastermind.billing import handlers as billing_handlers
from waldur_mastermind.common.enums import Units

from . import ledger, log, models
from .audit import skip_credit_audit

logger = logging.getLogger(__name__)


class MonthlyCompensation:
    """
    Handles monthly compensation calculations and applications for customer invoices.

    This class manages the process of applying credits (discounts) to customer invoices,
    handling both customer-level and project-specific credits while ensuring minimal
    consumption requirements are met.
    """

    def __init__(self, customer, invoice=None):
        self.customer = customer
        if invoice is not None:
            self.invoice = invoice
        else:
            self.invoice = (
                models.Invoice.objects.filter(
                    state__in=models.Invoice.States.MUTABLE_STATES, customer=customer
                )
                .order_by("-year", "-month")
                .first()
            )
        self._calculated = False
        self._compensations = []
        self._projects_credits = []
        self._total_compensation = 0
        self._tail = 0
        self._project_tails: dict[models.ProjectCredit, float] = {}
        # What each balance gave up to real usage this run. Measured at the
        # point of each subtraction rather than derived from the compensation
        # items afterwards: the last partial compensation of an exhausted credit
        # is reduced by tax, so the item and the balance movement differ there.
        self._customer_usage_draw = decimal.Decimal(0)
        self._project_usage_draws: dict[models.ProjectCredit, decimal.Decimal] = {}

        self.credit = models.CustomerCredit.objects.filter(
            customer=self.customer
        ).first()

    def calculate_current_compensations(self):
        if self._calculated:
            return

        if not self.credit or not self.credit.value or not self.invoice:
            return

        items_projects_ids = self.invoice.items.all().values_list(
            "resource__project_id", flat=True
        )

        projects_credits = {
            project_credit.project: project_credit
            for project_credit in models.ProjectCredit.objects.filter(
                project_id__in=items_projects_ids
            ).select_related("project")
        }
        chargeable_items, discount_by_item = models.creditable_items(
            self.invoice, self.credit
        )

        items: list[models.InvoiceItem] = sorted(
            chargeable_items,
            key=models.InvoiceItem._price,
        )

        for item in items:
            project_credit: models.ProjectCredit = projects_credits.get(
                item.project, None
            )
            # Net of any volume discount paired with this item (discount prices
            # are negative). Never draw credit below zero.
            cost = item.price + discount_by_item.get(item.uuid.hex, decimal.Decimal(0))
            if cost < 0:
                cost = decimal.Decimal(0)

            if project_credit:
                if cost >= project_credit.value:
                    cost -= project_credit.value
                    credit_compensation = project_credit.value  # item compensation
                    project_credit.value = 0
                    self.credit.value -= credit_compensation
                    self._record_usage_draw(project_credit, credit_compensation)
                else:
                    credit_compensation = cost
                    project_credit.value -= cost
                    self.credit.value -= cost
                    self._record_usage_draw(project_credit, cost)

            else:
                if cost >= self.credit.value:
                    credit_compensation = self.credit.value / (
                        1 + decimal.Decimal(self.invoice.tax_percent) / 100
                    )
                    self._record_usage_draw(None, self.credit.value)
                    self.credit.value = 0
                else:
                    credit_compensation = cost
                    self.credit.value -= cost
                    self._record_usage_draw(None, cost)

            if credit_compensation:
                # Copy the source item's details and link back to it, so the UI
                # can pair the compensation with the exact line item it offsets.
                compensation_details = dict(item.details or {})
                compensation_details["is_compensation"] = True
                compensation_details["compensation_of_item"] = item.uuid.hex
                compensation_project = item.resource.project
                self._compensations.append(
                    models.InvoiceItem(
                        invoice=self.invoice,
                        unit_price=credit_compensation * -1,
                        quantity=1,
                        unit=Units.QUANTITY,
                        credit=self.credit,
                        name=f"Credit compensation. {item}",
                        resource=item.resource,
                        project=compensation_project,
                        # These are normally denormalised by a post_save handler,
                        # which bulk_create below does not fire. Without them the
                        # project-scoped costs endpoint — it filters on
                        # project_uuid, not project_id — never sees compensations.
                        project_name=compensation_project.name,
                        project_uuid=compensation_project.uuid.hex,
                        details=compensation_details,
                    )
                )

            if not self.credit.value:
                break

        self._total_compensation = sum(
            credit.unit_price * -1 for credit in self._compensations
        )
        self._tail = 0

        if self.credit.minimal_consumption:
            if self._total_compensation < self.credit.minimal_consumption:
                self._tail = self.credit.minimal_consumption - self._total_compensation

                if self.credit.value - self._tail < 0:
                    self._tail = self.credit.value
                    self.credit.value = 0
                else:
                    self.credit.value -= self._tail

                self._total_compensation += self._tail

        # We need to set _calculated = True here, before calculating project tails
        # to avoid circular dependency. The issue is:
        # 1. calculate_current_compensations calls get_total_project_compensation
        # 2. get_total_project_compensation calls compensations property
        # 3. compensations property calls calculate_current_compensations again
        # By setting _calculated = True here, we break this cycle
        self._calculated = True

        for project_credit in projects_credits.values():
            if not project_credit.minimal_consumption:
                continue
            total_project_compensation = self.get_total_project_compensation(
                project_credit.project
            )
            if total_project_compensation < project_credit.minimal_consumption:
                tail = project_credit.minimal_consumption - total_project_compensation
                if project_credit.value - tail < 0:
                    tail = project_credit.value
                    project_credit.value = 0
                else:
                    project_credit.value -= tail

                self._project_tails[project_credit] = tail

        self._projects_credits = projects_credits.values()
        return

    @property
    def compensations(self) -> list[models.InvoiceItem]:
        self.calculate_current_compensations()
        return self._compensations

    @property
    def projects_credits(self):
        self.calculate_current_compensations()
        return self._projects_credits

    @property
    def total_compensation(self):
        self.calculate_current_compensations()
        return self._total_compensation

    @property
    def tail(self):
        self.calculate_current_compensations()
        return self._tail

    @property
    def billing_period(self) -> datetime.date | None:
        """First day of the month under compensation.

        None when the customer has no invoice open — there is then no month to
        bill and nothing to draw, and every caller here is a no-op rather than
        an error.
        """
        if not self.invoice:
            return None
        return datetime.date(self.invoice.year, self.invoice.month, 1)

    def update_linear_expected_consumption(self):
        if (
            self.credit
            and self.credit.minimal_consumption_logic
            == models.CustomerCredit.MinimalConsumptionLogic.LINEAR
            and self.credit.end_date
        ):
            new_expected_consumption = (
                self.credit.calculate_linear_expected_consumption(
                    self.total_compensation
                )
            )
            diff = new_expected_consumption - self.credit.expected_consumption
            self.credit.expected_consumption = new_expected_consumption
            self.credit.save(update_fields=["expected_consumption"])
            event_logger.emit(
                "Reduction of {customer_name} expected consumption by {consumption} according to linear minimal consumption logic.",
                event_type=EventType.REDUCTION_OF_CUSTOMER_EXPECTED_CONSUMPTION,
                event_context={
                    "consumption": diff,
                    "customer": self.customer,
                },
                scopes=[self.customer],
            )

        # Build tail lookup by PK for efficient access
        project_tails_by_pk = {pc.pk: tail for pc, tail in self._project_tails.items()}

        # Query ALL linear project credits for this customer, not just those
        # in _project_tails (fixes chicken-and-egg: when expected_consumption=0,
        # minimal_consumption=0, so credits never enter _project_tails)
        all_linear_project_credits = models.ProjectCredit.objects.filter(
            project__customer=self.customer,
            minimal_consumption_logic=models.ProjectCredit.MinimalConsumptionLogic.LINEAR,
            end_date__isnull=False,
            end_date__gt=datetime.date.today(),
        ).select_related("project")

        for project_credit in all_linear_project_credits:
            tail = project_tails_by_pk.get(project_credit.pk, decimal.Decimal("0"))
            new_expected_consumption = (
                project_credit.calculate_linear_expected_consumption(
                    tail + self.get_total_project_compensation(project_credit.project)
                )
            )
            diff = new_expected_consumption - project_credit.expected_consumption
            project_credit.expected_consumption = new_expected_consumption
            project_credit.save(update_fields=["expected_consumption"])
            event_logger.emit(
                "Reduction of {project_name} expected consumption by {consumption} according to linear minimal consumption logic.",
                event_type=EventType.REDUCTION_OF_PROJECT_EXPECTED_CONSUMPTION,
                event_context={
                    "consumption": diff,
                    "customer": self.customer,
                    "project": project_credit.project,
                },
                scopes=[self.customer, project_credit.project],
            )

    def _record_usage_draw(self, project_credit, amount):
        """Remember what a balance gave up to usage, as it happens."""
        amount = decimal.Decimal(amount)
        if project_credit is None:
            self._customer_usage_draw += amount
            return
        self._project_usage_draws[project_credit] = (
            self._project_usage_draws.get(project_credit, decimal.Decimal(0)) + amount
        )
        # Usage drawn against a project allocation is drawn from the
        # organization pool as well, in the same movement.
        self._customer_usage_draw += amount

    def _ledger_parts(self):
        """The breakdown of this run's value changes, per credit.

        Two kinds of movement share one save: what usage consumed, and the
        top-up to the minimal-consumption floor. The floor draw is the "Lost"
        figure — credit spent without buying anything — so it has to stay
        separable from compensation in the ledger.
        """
        if not self.invoice:
            return {}

        billing_period = self.billing_period
        parts = {}

        customer_parts = [
            ledger.TransactionPart(
                models.CreditTransaction.Types.COMPENSATION,
                -self._customer_usage_draw,
                billing_period,
            ),
            ledger.TransactionPart(
                models.CreditTransaction.Types.MINIMAL_DRAW,
                -decimal.Decimal(self.tail or 0),
                billing_period,
            ),
        ]
        parts[ledger.part_key(self.credit)] = customer_parts

        for project_credit in self.projects_credits:
            parts[ledger.part_key(project_credit)] = [
                ledger.TransactionPart(
                    models.CreditTransaction.Types.COMPENSATION,
                    -self._project_usage_draws.get(project_credit, decimal.Decimal(0)),
                    billing_period,
                ),
                ledger.TransactionPart(
                    models.CreditTransaction.Types.MINIMAL_DRAW,
                    -decimal.Decimal(self._project_tails.get(project_credit, 0)),
                    billing_period,
                ),
            ]

        return parts

    def get_total_project_compensation(self, project: Project):
        return sum(
            c.unit_price * -1
            for c in self.compensations
            if c.resource and c.resource.project == project
        )

    @transaction.atomic
    def save(self):
        if not self.credit:
            return

        models.InvoiceItem.objects.bulk_create(self.compensations)

        # The compensation flow emits its own REDUCTION_OF_*_CREDIT* events below;
        # suppress the generic UPDATE_OF_*_CREDIT_BY_STAFF audit to avoid duplicates.
        #
        # The declared breakdown is refused if it does not add up to the delta
        # it claims to explain, so the enclosing type says what the movement is
        # when that happens: still this month's compensation run, just no longer
        # apportioned. Without it a refused breakdown would degrade all the way
        # to an undated, unreferenced staff grant, and a run that already looks
        # wrong would be filed as a grant of credit.
        with (
            skip_credit_audit(),
            ledger.credit_transaction_type(
                models.CreditTransaction.Types.COMPENSATION,
                reference=self.invoice,
                billing_period=self.billing_period,
            ),
            ledger.credit_transaction_parts(
                self._ledger_parts(), reference=self.invoice
            ),
        ):
            for pc in self.projects_credits:
                pc.save(update_fields=["value"])

            self.credit.save(update_fields=["value"])

        if self.tail:
            event_logger.emit(
                "Reduction of {customer_name} credit by {consumption} due to minimal consumption of {minimal_consumption}",
                event_type=EventType.REDUCTION_OF_CUSTOMER_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
                event_context={
                    "consumption": self.tail,
                    "minimal_consumption": self.credit.minimal_consumption,
                    "customer": self.customer,
                    "credit_balance": int(self.credit.value),
                },
                scopes=[self.customer],
            )

        for invoice_item in self.compensations:
            event_logger.emit(
                "Reduction of {customer_name} credit by {consumption} due to compensation of invoice item {invoice_item}.",
                event_type=EventType.REDUCTION_OF_CUSTOMER_CREDIT,
                event_context={
                    "consumption": invoice_item.unit_price,
                    "customer": self.customer,
                    "invoice_item": str(invoice_item),
                    "credit_balance": int(self.credit.value),
                },
                scopes=[self.customer],
            )
            project_credit = next(
                (
                    pc
                    for pc in self.projects_credits
                    if pc.project == invoice_item.project
                ),
                None,
            )
            event_logger.emit(
                "Reduction of {project_name} credit by {consumption} due to compensation of invoice item {invoice_item}.",
                event_type=EventType.REDUCTION_OF_PROJECT_CREDIT,
                event_context={
                    "consumption": invoice_item.unit_price,
                    "customer": self.customer,
                    "project": invoice_item.project,
                    "invoice_item": str(invoice_item),
                    "credit_balance": int(self.credit.value),
                    **(
                        {"project_credit_balance": int(project_credit.value)}
                        if project_credit
                        else {}
                    ),
                },
                scopes=[self.customer, invoice_item.project],
            )
            # Because bulk_create is used for InvoiceItem, the post_save signals
            # will not be sent, and event would not be emitted by handler.
            event_logger.emit(
                "Invoice item has been created",
                event_type=EventType.INVOICE_ITEM_CREATED,
                event_context={
                    "invoice_item": invoice_item,
                },
                scopes=[invoice_item.invoice.customer, invoice_item.invoice],
            )

        for project_credit, tail in self._project_tails.items():
            event_logger.emit(
                "Reduction of {project_name} credit by {consumption} due to minimal consumption of {minimal_consumption}",
                event_type=EventType.REDUCTION_OF_PROJECT_CREDIT_DUE_TO_MINIMAL_CONSUMPTION,
                event_context={
                    "consumption": tail,
                    "minimal_consumption": project_credit.minimal_consumption,
                    "customer": self.customer,
                    "project": project_credit.project,
                    "credit_balance": int(self.credit.value),
                    "project_credit_balance": int(project_credit.value),
                },
                scopes=[self.customer, project_credit.project],
            )

        self.refresh_price_estimates(
            {compensation.project for compensation in self._compensations}
        )

    def refresh_price_estimates(self, projects):
        """Recompute PriceEstimate for the customer and the given projects.

        PriceEstimate.total sums every invoice item for the month, compensations
        included — they are ordinary items with a negative unit_price — so it is
        a cost net of credit. It is kept current by a post_save handler on
        InvoiceItem, which bulk_create does not fire and which is not connected
        for deletes at all. Without this call the stored estimate keeps the
        pre-compensation value, and everything reading billing_price_estimate
        (cost policy rows, the project dashboard) reports a cost the credit has
        already paid for.

        Only the projects whose invoice items changed are passed: the minimal
        consumption tail moves credit values without writing an invoice item, so
        it cannot alter an estimate. The customer scope always changes with any
        of its projects, so it is always included.
        """
        if not projects:
            return
        billing_handlers.update_estimates_for_scopes([self.customer, *projects])

    def get_project_credit_consumption(self, project):
        """Returns the value by which the project credit will be reduced next month."""

        if [p for p in self.projects_credits if p.project == project]:
            projects_credit = [
                p for p in self.projects_credits if p.project == project
            ][0]
            new_project_value = projects_credit.value
            projects_credit.refresh_from_db()
            old_project_value = projects_credit.value
            return old_project_value - new_project_value

        return 0

    def get_project_compensation(self, project):
        """Returns the sum of compensation in the next month for the project."""

        return sum(
            [
                c.unit_price * -1
                for c in self.compensations
                if c.resource.project == project
            ]
        )

    def get_resource_compensation(self, resource):
        """Returns the sum of compensation in the next month for the resource."""

        return sum(
            [c.unit_price * -1 for c in self.compensations if c.resource == resource]
        )

    @transaction.atomic
    def clear_compensations(self):
        """
        This method removes compensations in pended invoice.

        Attention!
        This method works correctly only if the minimal consumption has not changed since the moment
        compensation was applied for the current month and until now.
        Also this method does not work correctly if compensations have been applied
        but compensation items have not created and was consumption only due to minimal consumption.
        """

        if self._calculated:
            # If compensations have been calculated then we have dirty values of credits,
            # and we needed initiate the object again.
            self.__init__(self.customer, invoice=self.invoice)

        if not self.credit:
            return

        if not self.invoice:
            return

        compensation_items = self.invoice.items.filter(credit=self.credit)

        if not compensation_items:
            return

        applied_compensations_sum = (
            compensation_items.aggregate(sum=Sum("unit_price"))["sum"] or 0
        ) * -1

        # Resolved before the delete below, because the queryset is lazy and
        # would come back empty afterwards. These are the projects whose net
        # cost changes, and so the estimates to refresh at the end.
        affected_projects = list(
            Project.objects.filter(id__in=compensation_items.values("project_id"))
        )

        # The roll-back flow emits its own ROLL_BACK_*_CREDIT events below;
        # suppress the generic UPDATE_OF_*_CREDIT_BY_STAFF audit to avoid duplicates.
        #
        # Dated to the month it reverses, not left open: applying compensations
        # is clear-then-save, and staff can run it against a pending invoice as
        # often as they like. An undated roll-back leaves each superseded run's
        # drawdown standing in its month, so the month reports the drawdown once
        # per run instead of once.
        with (
            skip_credit_audit(),
            ledger.credit_transaction_type(
                models.CreditTransaction.Types.ROLLBACK,
                reference=self.invoice,
                billing_period=self.billing_period,
            ),
        ):
            old_credit_value = self.credit.value
            self.credit.value += max(
                applied_compensations_sum, self.credit.minimal_consumption
            )
            self.credit.save(update_fields=["value"])
            log.log_roll_back_customer_credit(
                self.credit.customer,
                old_credit_value,
                self.credit.value,
            )

            project_consumptions = list(
                compensation_items.values("project_id").annotate(
                    value=Sum("unit_price")
                )
            )

            for project_credit in models.ProjectCredit.objects.filter(
                project__customer=self.customer
            ):
                value = [
                    consumption["value"]
                    for consumption in project_consumptions
                    if consumption["project_id"] == project_credit.project.id
                ]

                if value:
                    value = value[0] * -1
                    old_project_credit_value = project_credit.value
                    project_credit.value += value
                    project_credit.save(update_fields=["value"])
                    log.log_roll_back_project_credit(
                        self.credit.customer,
                        project_credit.project,
                        old_project_credit_value,
                        project_credit.value,
                    )

            compensation_items.delete()

        # Removing compensations raises the net cost again, and no post_delete
        # handler is connected for InvoiceItem, so refresh here too rather than
        # relying on a later save() — clear_compensations is also called on its
        # own from the API.
        self.refresh_price_estimates(affected_projects)

    def apply_compensations(self):
        self.clear_compensations()
        self.save()
