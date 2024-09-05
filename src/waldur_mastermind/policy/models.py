import datetime
import logging

from dateutil.relativedelta import relativedelta
from django.conf import settings
from django.core import exceptions
from django.db import models
from django.db.models import Q, Sum
from django.utils.translation import gettext_lazy as _
from django_fsm import FSMIntegerField
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import policy_actions

logger = logging.getLogger(__name__)


class Policy(
    TimeStampedModel,
    core_models.UuidMixin,
):
    trigger_class = NotImplemented
    observable_classes = [marketplace_models.Resource]
    available_actions = NotImplemented

    has_fired = models.BooleanField(default=False)
    fired_datetime = models.DateTimeField(null=True, blank=True, editable=False)
    created_by = models.ForeignKey(
        on_delete=models.CASCADE,
        to=settings.AUTH_USER_MODEL,
        related_name="+",
        blank=True,
        null=True,
    )
    actions = NotImplemented
    scope = NotImplemented

    @staticmethod
    def get_scope_from_observable_object(observable_object):
        return

    def get_scope_homeport_url(self):
        return

    def is_triggered(self):
        """Checking if the policy needs to be applied."""
        raise NotImplementedError()

    def get_all_actions(self):
        actions = []

        for action_name in self.actions.split(","):
            if action_name in [a.__name__ for a in self.available_actions]:
                actions.append(
                    [a for a in self.available_actions if a.__name__ == action_name][0]
                )

        return actions

    def get_not_one_time_actions(self):
        actions = self.get_all_actions()
        return [a for a in actions if not getattr(a, "one_time_action", False)]

    def get_one_time_actions(self):
        actions = self.get_all_actions()
        return [a for a in actions if getattr(a, "one_time_action", False)]

    class Meta:
        abstract = True


class PeriodMixin(models.Model):
    class Periods:
        TOTAL = 1
        MONTH_1 = 2
        MONTH_3 = 3
        MONTH_12 = 4

        CHOICES = (
            (TOTAL, "Total"),
            (MONTH_1, "1 month"),
            (MONTH_3, "3 month"),
            (MONTH_12, "12 month"),
        )

    period = FSMIntegerField(default=Periods.MONTH_1, choices=Periods.CHOICES)

    class Meta:
        abstract = True


class EstimatedCostPolicyMixin(PeriodMixin):
    trigger_class = invoices_models.InvoiceItem

    limit_cost = models.IntegerField()

    def _is_triggered(self, invoice_items):
        customers = structure_models.Customer.objects.filter(
            blocked=False,
            archived=False,
        )
        invoice_items = invoice_items.filter(
            invoice__customer__in=customers,
        ).exclude(invoice__state=invoices_models.Invoice.States.CANCELED)
        month_start = core_utils.month_start(datetime.date.today())
        period = 0

        if self.period == self.Periods.MONTH_1:
            period = 1
        elif self.period == self.Periods.MONTH_3:
            period = 3
        elif self.period == self.Periods.MONTH_12:
            period = 12

        query = Q()

        for n in range(period):
            previous_month_date = month_start - relativedelta(months=n)
            query |= Q(
                invoice__month=previous_month_date.month,
                invoice__year=previous_month_date.year,
            )

        invoice_items = invoice_items.filter(query)

        total = sum([i.total for i in invoice_items])
        return total > self.limit_cost

    class Meta:
        abstract = True


class ProjectPolicy(Policy):
    class Permissions:
        customer_path = "scope__customer"
        project_path = "scope"

    available_actions = {
        policy_actions.notify_project_team,
        policy_actions.notify_organization_owners,
        policy_actions.block_creation_of_new_resources,
        policy_actions.block_modification_of_existing_resources,
        policy_actions.terminate_resources,
        policy_actions.request_downscaling,
    }

    scope = models.ForeignKey(structure_models.Project, on_delete=models.CASCADE)
    actions = models.CharField(max_length=255)

    @staticmethod
    def get_scope_from_observable_object(observable_object):
        return structure_permissions._get_project(observable_object)

    def get_scope_homeport_url(self):
        return core_utils.format_homeport_link(
            "projects/{uuid}/", uuid=self.scope.uuid.hex
        )

    class Meta:
        abstract = True


class ProjectEstimatedCostPolicy(EstimatedCostPolicyMixin, ProjectPolicy):
    def is_triggered(self):
        project = self.scope
        invoice_items = invoices_models.InvoiceItem.objects.filter(project=project)

        return self._is_triggered(invoice_items)

    class Meta:
        verbose_name_plural = "Project estimated cost policies"


class CustomerPolicy(Policy):
    class Permissions:
        customer_path = "scope"

    available_actions = {
        policy_actions.notify_organization_owners,
        policy_actions.block_creation_of_new_resources,
        policy_actions.block_modification_of_existing_resources,
        policy_actions.terminate_resources,
        policy_actions.request_downscaling,
    }

    scope = models.ForeignKey(structure_models.Customer, on_delete=models.CASCADE)
    actions = models.CharField(max_length=255)

    @staticmethod
    def get_scope_from_observable_object(observable_object):
        return structure_permissions._get_customer(observable_object)

    def get_scope_homeport_url(self):
        return core_utils.format_homeport_link(
            "/organizations/{uuid}/dashboard/", uuid=self.scope.uuid.hex
        )

    class Meta:
        abstract = True


class CustomerEstimatedCostPolicy(EstimatedCostPolicyMixin, CustomerPolicy):
    def is_triggered(self):
        customer = self.scope
        invoice_items = invoices_models.InvoiceItem.objects.filter(
            invoice__customer=customer
        )

        return self._is_triggered(invoice_items)

    class Meta:
        verbose_name_plural = "Customer estimated cost policies"


class OfferingPolicy(Policy):
    class Permissions:
        customer_path = "scope__customer"

    available_actions = {
        policy_actions.notify_organization_owners,
        policy_actions.block_creation_of_new_resources,
    }
    observable_classes = []

    scope = models.ForeignKey(marketplace_models.Offering, on_delete=models.CASCADE)
    organization_groups = models.ManyToManyField(structure_models.OrganizationGroup)
    actions = models.CharField(max_length=255)

    @staticmethod
    def get_scope_from_observable_object(resource):
        return resource.offering

    def get_scope_homeport_url(self):
        return core_utils.format_homeport_link(
            "/providers/{customer_uuid}/marketplace-provider-offering-details/{uuid}/",
            customer_uuid=self.scope.customer.uuid.hex,
            uuid=self.scope.uuid.hex,
        )

    class Meta:
        abstract = True


class OfferingEstimatedCostPolicy(EstimatedCostPolicyMixin, OfferingPolicy):
    def is_triggered(self):
        customers = structure_models.Customer.objects.filter(
            organization_group__in=self.organization_groups.all()
        )
        items = invoices_models.InvoiceItem.objects.filter(
            resource__offering=self.scope,
            invoice__customer__in=customers,
        )
        return self._is_triggered(items)

    class Meta:
        verbose_name_plural = "Offering estimated cost policies"


class OfferingUsagePolicy(PeriodMixin, OfferingPolicy):
    trigger_class = marketplace_models.ComponentUsage

    component_limit = models.ManyToManyField(
        marketplace_models.OfferingComponent, through="OfferingComponentLimit"
    )

    def is_triggered(self):
        customers = structure_models.Customer.objects.filter(
            organization_group__in=self.organization_groups.all(),
            blocked=False,
            archived=False,
        )
        usages = marketplace_models.ComponentUsage.objects.filter(
            resource__project__customer__in=customers
        )

        if self.period in (
            self.Periods.MONTH_1,
            self.Periods.MONTH_3,
            self.Periods.MONTH_12,
        ):
            start = core_utils.month_start(datetime.date.today())

            if self.period == self.Periods.MONTH_3:
                start = core_utils.month_start(
                    datetime.date.today() - relativedelta(months=2)
                )
            elif self.period == self.Periods.MONTH_12:
                start = core_utils.month_start(
                    datetime.date.today() - relativedelta(months=11)
                )

            usages = usages.filter(
                billing_period__gte=start,
                billing_period__lte=core_utils.month_end(datetime.date.today()),
            )

        for component_limit in self.component_limits_set.all():
            total = (
                usages.filter(component=component_limit.component).aggregate(
                    usage=Sum("usage")
                )["usage"]
                or 0
            )
            if total > component_limit.limit:
                return True
            else:
                return False


class OfferingComponentLimit(TimeStampedModel):
    policy = models.ForeignKey(
        OfferingUsagePolicy,
        on_delete=models.CASCADE,
        null=False,
        related_name="component_limits_set",
    )
    component = models.ForeignKey(
        marketplace_models.OfferingComponent, on_delete=models.CASCADE, null=False
    )
    limit = models.IntegerField()

    class Meta:
        unique_together = (("policy", "component"),)

    def save(self, *args, **kwargs):
        if self.component not in self.policy.scope.components.all():
            raise exceptions.ValidationError(
                _("The selected component does not match the offering.")
            )

        return super().save(*args, **kwargs)
