from django.utils.functional import cached_property

from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import PLUGIN_NAME
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import OfferingStates, OrderStates
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class MarketplaceFixture(structure_fixtures.ProjectFixture):
    def __init__(self):
        self.plan_component
        self.service_provider
        self.order

    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=PLUGIN_NAME,
            state=OfferingStates.ACTIVE,
            project=self.offering_project,
            customer=self.offering_customer,
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
    def plan_component(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_component,
            price=3,
            amount=2,
        )

    @cached_property
    def offering_component(self):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=marketplace_models.OfferingComponent.BillingTypes.FIXED,
        )

    @cached_property
    def plan_usage_component(self):
        return marketplace_factories.PlanComponentFactory(
            plan=self.plan,
            component=self.offering_usage_component,
        )

    @cached_property
    def offering_usage_component(self):
        return marketplace_factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            name="RAM",
            billing_type=marketplace_models.OfferingComponent.BillingTypes.USAGE,
        )

    @cached_property
    def order(self):
        return marketplace_factories.OrderFactory(
            project=self.project,
            offering=self.offering,
            attributes={"name": "item_name", "description": "Description"},
            plan=self.plan,
            resource=self.resource,
            state=OrderStates.DONE,
        )

    @cached_property
    def update_order(self):
        """Order specifically for testing resource updates"""
        return marketplace_factories.OrderFactory(
            project=self.project,
            offering=self.offering,
            resource=self.resource,
            plan=self.plan,
            state=OrderStates.EXECUTING,
            type=marketplace_models.Order.Types.UPDATE,
        )

    @cached_property
    def service_provider(self):
        return marketplace_factories.ServiceProviderFactory(
            customer=self.offering_customer,
            description="ServiceProvider's description",
        )

    @cached_property
    def service_owner(self):
        user = structure_factories.UserFactory()
        self.offering_customer.add_user(user, CustomerRole.OWNER)
        return user

    @cached_property
    def resource(self) -> marketplace_models.Resource:
        return marketplace_factories.ResourceFactory(
            offering=self.offering, plan=self.plan, project=self.project
        )

    @cached_property
    def offering_fixture(self):
        return structure_fixtures.ProjectFixture()

    @cached_property
    def offering_owner(self):
        return self.offering_fixture.owner

    @cached_property
    def service_manager(self):
        return self.offering_fixture.service_manager

    @cached_property
    def offering_admin(self):
        return self.offering_fixture.admin

    @cached_property
    def offering_manager(self):
        manager = structure_factories.UserFactory()
        self.offering.add_user(manager, OfferingRole.MANAGER)
        return manager

    @cached_property
    def offering_project(self):
        return self.offering_fixture.project

    @cached_property
    def offering_customer(self):
        return self.offering_fixture.customer

    @cached_property
    def offering_support(self):
        return self.offering_fixture.customer_support

    @cached_property
    def provider_owner(self):
        user = structure_factories.UserFactory()
        self.offering_customer.add_user(user, CustomerRole.OWNER)
        return user

    @cached_property
    def provider_manager(self):
        user = structure_factories.UserFactory()
        self.offering_customer.add_user(user, ServiceProviderRole.MANAGER)
        self.offering.add_user(user, OfferingRole.MANAGER)
        return user
