from django.utils.functional import cached_property

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace_support import PLUGIN_NAME


class SupportFixture(structure_fixtures.ProjectFixture):
    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(type=PLUGIN_NAME, options={'order': []})

    @cached_property
    def plan(self):
        plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            name='Standard plan',
            unit_price=10,
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        marketplace_factories.PlanComponentFactory(
            plan=plan,
            component=self.offering_component_cpu,
            price=4,
            amount=1,
        )
        marketplace_factories.PlanComponentFactory(
            plan=plan,
            component=self.offering_component_ram,
            price=3,
            amount=2,
        )
        return plan

    @cached_property
    def order(self):
        return marketplace_factories.OrderFactory(project=self.project)

    @cached_property
    def order_item(self):
        return marketplace_factories.OrderItemFactory(
            order=self.order,
            offering=self.offering,
            attributes={'name': 'item_name', 'description': 'Description'},
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
            type='ram'
        )

    @cached_property
    def new_plan(self):
        new_plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            unit_price=5,
            name='Small plan',
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        marketplace_factories.PlanComponentFactory(
            plan=new_plan,
            component=self.offering_component_cpu,
            price=3
        )
        marketplace_factories.PlanComponentFactory(
            plan=new_plan,
            component=self.offering_component_ram,
            price=2
        )
        return new_plan

    @cached_property
    def new_order(self):
        return marketplace_factories.OrderFactory(project=self.project)

    @cached_property
    def new_order_item(self):
        return marketplace_factories.OrderItemFactory(
            offering=self.offering,
            attributes={'name': 'item_name_2', 'description': 'Description_2'},
            plan=self.plan,
            order=self.new_order
        )
