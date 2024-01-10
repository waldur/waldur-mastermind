from django.utils.functional import cached_property

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.support.tests.fixtures import SupportFixture


class MarketplaceSupportApprovedFixture(SupportFixture):
    def __init__(self, provider_customer=None):
        self.provider_customer = (
            provider_customer or structure_factories.CustomerFactory()
        )
        self.plan_component
        self.order

    @cached_property
    def provider(self):
        return marketplace_factories.ServiceProviderFactory(
            customer=self.provider_customer
        )

    @cached_property
    def marketplace_offering(self):
        return marketplace_factories.OfferingFactory(
            customer=self.provider.customer,
            type=PLUGIN_NAME,
        )

    @cached_property
    def resource(self):
        return marketplace_factories.ResourceFactory(
            offering=self.marketplace_offering, project=self.project, plan=self.plan
        )

    @cached_property
    def plan(self):
        return marketplace_factories.PlanFactory(
            unit=marketplace_models.Plan.Units.PER_MONTH,
            offering=self.marketplace_offering,
        )

    @cached_property
    def offering_component(self):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.marketplace_offering,
        )

    @cached_property
    def plan_component(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
        )

    @cached_property
    def order(self):
        return marketplace_factories.OrderFactory(
            project=self.project,
            offering=self.marketplace_offering,
            plan=self.plan,
            state=marketplace_models.Order.States.DONE,
            resource=self.resource,
        )


class SupportFixture(structure_fixtures.ProjectFixture):
    def __init__(self):
        self.plan_component_cpu
        self.plan_component_ram
        self.new_plan_component_cpu
        self.new_plan_component_ram
        self.service_provider
        self.update_plan_prices()

    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, options={"order": []}
        )

    @cached_property
    def plan(self):
        plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            name="Standard plan",
            unit_price=0,
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        return plan

    @cached_property
    def plan_component_cpu(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component_cpu,
            price=4,
            amount=1,
        )

    @cached_property
    def plan_component_ram(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component_ram,
            price=3,
            amount=2,
        )

    @cached_property
    def order(self) -> marketplace_models.Order:
        return marketplace_factories.OrderFactory(
            project=self.project,
            offering=self.offering,
            attributes={"name": "item_name", "description": "Description"},
            plan=self.plan,
        )

    @cached_property
    def offering_component_cpu(self):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED,
        )

    @cached_property
    def offering_component_ram(self):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED,
            type="ram",
        )

    @cached_property
    def new_plan(self):
        new_plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            unit_price=0,
            name="Small plan",
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        return new_plan

    @cached_property
    def new_plan_component_cpu(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.new_plan, component=self.offering_component_cpu, price=3
        )

    @cached_property
    def new_plan_component_ram(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.new_plan, component=self.offering_component_ram, price=2
        )

    @cached_property
    def new_order(self):
        return marketplace_factories.OrderFactory(
            offering=self.offering,
            project=self.project,
            attributes={"name": "item_name_2", "description": "Description_2"},
            plan=self.plan,
        )

    @cached_property
    def service_provider(self):
        return marketplace_factories.ServiceProviderFactory(
            customer=self.order.offering.customer,
            description="ServiceProvider's description",
        )

    @cached_property
    def service_owner(self):
        owner = structure_factories.UserFactory()
        self.offering.customer.add_user(owner, CustomerRole.OWNER)
        return owner

    def update_plan_prices(self):
        self._update_plan_price("plan")
        self._update_plan_price("new_plan")

    def _update_plan_price(self, plan_name):
        plan = getattr(self, plan_name)
        fixed_components = plan.components.filter(component__billing_type="fixed")
        plan.unit_price = sum(comp.amount * comp.price for comp in fixed_components)
        plan.save()
