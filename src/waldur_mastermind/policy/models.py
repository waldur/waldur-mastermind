import datetime
import logging

from django.conf import settings
from django.db import models
from django_fsm import FSMIntegerField
from model_utils.models import TimeStampedModel

from waldur_core.core import models as core_models
from waldur_core.core import utils as core_utils
from waldur_core.structure import models as structure_models
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.billing import models as billing_models
from waldur_mastermind.invoices import models as invoices_models
from waldur_mastermind.marketplace import models as marketplace_models

from . import policy_actions

logger = logging.getLogger(__name__)


class Policy(
    TimeStampedModel,
    core_models.UuidMixin,
):
    trigger_class = billing_models.PriceEstimate
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

    @classmethod
    def get_scope_class(cls):
        return cls.scope.field.related_model

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


class EstimatedCostPolicyMixin(models.Model):
    limit_cost = models.IntegerField()

    def is_triggered(self):
        try:
            price_estimate = billing_models.PriceEstimate.objects.get(scope=self.scope)
            return price_estimate.total > self.limit_cost
        except billing_models.PriceEstimate.DoesNotExist:
            return False

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
    class Meta:
        verbose_name_plural = "Customer estimated cost policies"


class OfferingPolicy(Policy):
    class Permissions:
        customer_path = "scope__customer"

    available_actions = {
        policy_actions.notify_organization_owners,
        policy_actions.block_creation_of_new_resources,
    }

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
    trigger_class = invoices_models.InvoiceItem

    class Periods:
        TOTAL = 1
        MONTH = 2

        CHOICES = (
            (TOTAL, "Total"),
            (MONTH, "Month"),
        )

    period = FSMIntegerField(default=Periods.TOTAL, choices=Periods.CHOICES)

    def is_triggered(self):
        customers = structure_models.Customer.objects.filter(
            organization_group__in=self.organization_groups.all(),
            blocked=False,
            archived=False,
        )
        items = invoices_models.InvoiceItem.objects.filter(
            resource__offering=self.scope,
            invoice__customer__in=customers,
        )

        if self.period == self.Periods.MONTH:
            items = items.filter(
                invoice__created__gte=core_utils.month_start(datetime.date.today()),
                invoice__created__lte=core_utils.month_end(datetime.date.today()),
            )

        total = sum([i.price for i in items])
        return total > self.limit_cost

    class Meta:
        verbose_name_plural = "Offering estimated cost policies"
