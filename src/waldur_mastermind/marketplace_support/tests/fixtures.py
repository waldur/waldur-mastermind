from django.utils.functional import cached_property

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support.tests.fixtures import SupportFixture


class MarketplaceSupportApprovedFixture(SupportFixture):
    def __init__(self, provider_customer=None):
        self.provider_customer = (
            provider_customer or structure_factories.CustomerFactory()
        )
        self.plan_component
        self.order_item

    @cached_property
    def provider(self):
        return marketplace_factories.ServiceProviderFactory(
            customer=self.provider_customer
        )

    @cached_property
    def marketplace_offering(self):
        offering = marketplace_factories.OfferingFactory(
            customer=self.provider.customer
        )
        offering.scope = self.offering.template
        offering.save()
        return offering

    @cached_property
    def resource(self):
        resource = marketplace_factories.ResourceFactory(
            offering=self.marketplace_offering, project=self.project, plan=self.plan
        )
        resource.scope = self.offering
        resource.save()
        return resource

    @cached_property
    def plan(self):
        return marketplace_factories.PlanFactory(
            unit=marketplace_models.Plan.Units.PER_MONTH
        )

    @cached_property
    def offering_component(self):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.marketplace_offering,
        )

    @cached_property
    def plan_component(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component,
        )

    @cached_property
    def order(self):
        return marketplace_factories.OrderFactory(project=self.project)

    @cached_property
    def order_item(self):
        return marketplace_factories.OrderItemFactory(
            order=self.order,
            offering=self.marketplace_offering,
            plan=self.plan,
            state=marketplace_models.OrderItem.States.DONE,
            resource=self.resource,
        )
