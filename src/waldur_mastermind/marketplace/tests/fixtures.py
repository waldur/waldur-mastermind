from django.utils.functional import cached_property

from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import PLUGIN_NAME
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class MarketplaceFixture(structure_fixtures.ProjectFixture):
    def __init__(self):
        self.plan_component
        self.service_provider
        self.order_item

    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME, options={'order': []}
        )

    @cached_property
    def plan(self):
        plan = marketplace_factories.PlanFactory(
            offering=self.offering,
            name='Standard plan',
            unit_price=0,
            unit=marketplace_models.Plan.Units.PER_MONTH,
        )
        return plan

    @cached_property
    def plan_component(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component, price=3, amount=2,
        )

    @cached_property
    def offering_component(self):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED,
        )

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
            resource=self.resource,
            state=marketplace_models.OrderItem.States.DONE,
        )

    @cached_property
    def service_provider(self):
        return marketplace_factories.ServiceProviderFactory(
            customer=self.order_item.offering.customer,
            description='ServiceProvider\'s description',
        )

    @cached_property
    def resource(self) -> marketplace_models.Resource:
        return marketplace_factories.ResourceFactory(
            offering=self.offering, plan=self.plan, project=self.project
        )

    @cached_property
    def service_manager(self):
        user = structure_factories.UserFactory()
        self.order_item.offering.customer.add_user(
            user, role=structure_models.CustomerRole.SERVICE_MANAGER
        )
        return user

    @cached_property
    def offering_owner(self):
        user = structure_factories.UserFactory()
        self.order_item.offering.customer.add_user(
            user, role=structure_models.CustomerRole.OWNER
        )
        return user
