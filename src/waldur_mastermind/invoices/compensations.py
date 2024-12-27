import datetime
import logging
from decimal import Decimal

from django.db import transaction
from django.db.models import Sum

from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models

from . import log, models

logger = logging.getLogger(__name__)


class MonthlyCompensation:
    """
    Handles monthly compensation calculations and applications for customer invoices.

    This class manages the process of applying credits (discounts) to customer invoices,
    handling both customer-level and project-specific credits while ensuring minimal
    consumption requirements are met.
    """

    def __init__(self, customer):
        self.customer = customer
        self.invoice = (
            models.Invoice.objects.filter(
                state=models.Invoice.States.PENDING, customer=customer
            )
            .order_by("-year", "-month")
            .first()
        )
        self._calculated = False
        self._compensations = []
        self._projects_credits = []
        self._total_compensation = 0
        self._tail = 0

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
            p.project: p
            for p in models.ProjectCredit.objects.filter(
                project_id__in=items_projects_ids
            ).select_related("project")  # Prefetch related project data
        }
        credit_offerings = list(self.credit.offerings.all())

        items = sorted(
            [
                i
                for i in self.invoice.items.exclude(resource__isnull=True)
                # if credit offerings are limited, check if item belongs to the limited offering
                if not credit_offerings or i.resource.offering in credit_offerings
            ],
            key=models.InvoiceItem._price,
        )

        for item in items:
            project_credit: models.ProjectCredit = projects_credits.get(
                item.project, None
            )
            cost = item.total

            if project_credit:
                if cost >= project_credit.value:
                    cost -= project_credit.value
                    credit_compensation = project_credit.value  # item compensation
                    project_credit.value = 0
                    self.credit.value -= credit_compensation
                else:
                    credit_compensation = cost
                    project_credit.value -= cost
                    self.credit.value -= cost

            else:
                if cost >= self.credit.value:
                    credit_compensation = self.credit.value
                    self.credit.value = 0
                else:
                    credit_compensation = cost
                    self.credit.value -= cost

            if credit_compensation:
                self._compensations.append(
                    models.InvoiceItem(
                        invoice=self.invoice,
                        unit_price=credit_compensation * -1,
                        quantity=1,
                        unit=models.InvoiceItem.Units.QUANTITY,
                        credit=self.credit,
                        name=f"Credit compensation. {item}",
                        resource=item.resource,
                        project=item.resource.project,
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

        self._projects_credits = projects_credits.values()
        self._calculated = True
        return

    @property
    def compensations(self):
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

    @staticmethod
    def calculate_linear_minimal_consumption(
        customer: structure_models.Customer,
        credit_value: int | Decimal,
        end_date: datetime.date,
    ):
        today = datetime.date.today()
        months = (end_date.year * 12 + end_date.month) - (today.year * 12 + today.month)
        last_month = core_utils.get_last_month()

        if models.Invoice.objects.filter(
            customer=customer,
            year=last_month.year,
            month=last_month.month,
            state=models.Invoice.States.PENDING,
        ).exists():
            months += 1

        if not months:
            return credit_value

        return credit_value / months

    def update_linear_minimal_consumption(self):
        if (
            self.credit
            and self.credit.minimal_consumption_logic
            == models.CustomerCredit.MinimalConsumptionLogic.LINEAR
            and self.credit.end_date
        ):
            self.credit.minimal_consumption = (
                MonthlyCompensation.calculate_linear_minimal_consumption(
                    self.customer,
                    self.credit.value,
                    self.credit.end_date,
                )
            )
            self.credit.save(update_fields=["minimal_consumption"])

    @transaction.atomic
    def save(self):
        if not self.credit:
            return

        models.InvoiceItem.objects.bulk_create(self.compensations)

        for pc in self.projects_credits:
            pc.save()

        self.credit.save(update_fields=["value"])

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
            self.__init__(self.customer)

        if not self.credit:
            return

        compensation_items = self.invoice.items.filter(credit=self.credit)

        if not compensation_items:
            return

        applied_compensations_sum = (
            compensation_items.aggregate(sum=Sum("unit_price"))["sum"] or 0
        ) * -1

        old_credit_value = self.credit.value
        self.credit.value += max(
            applied_compensations_sum, self.credit.minimal_consumption
        )
        self.credit.save()
        log.log_roll_back_customer_credit(
            self.credit.customer,
            old_credit_value,
            self.credit.value,
        )

        project_consumptions = list(
            compensation_items.values("project_id").annotate(value=Sum("unit_price"))
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
                project_credit.save()
                log.log_roll_back_project_credit(
                    self.credit.customer,
                    project_credit.project,
                    old_project_credit_value,
                    project_credit.value,
                )

        compensation_items.delete()

    def apply_compensations(self):
        self.clear_compensations()
        self.save()
