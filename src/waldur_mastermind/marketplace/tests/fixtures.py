from django.utils.functional import cached_property

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import (
    BASIC_OFFERING,
    BillingTypes,
    OfferingStates,
    OrderStates,
    OrderTypes,
)
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class MarketplaceFixture(structure_fixtures.ProjectFixture):
    def __init__(self):
        self.plan_component
        self.service_provider
        self.order

    @cached_property
    def offering(self):
        return marketplace_factories.OfferingFactory(
            type=BASIC_OFFERING,
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
            billing_type=BillingTypes.FIXED,
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
            billing_type=BillingTypes.USAGE,
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
            type=OrderTypes.UPDATE,
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
        # service_owner bypasses CustomerFixture.owner, so grant OWNER defaults here.
        CustomerRole.OWNER.add_permission(
            PermissionEnum.MANAGE_MAINTENANCE_ANNOUNCEMENT
        )
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

    @cached_property
    def maintenance_announcement(self):
        return marketplace_factories.MaintenanceAnnouncementFactory(
            service_provider=self.service_provider,
            created_by=self.service_owner,
        )

    @cached_property
    def maintenance_announcement_offering(self):
        return marketplace_factories.MaintenanceAnnouncementOfferingFactory(
            maintenance=self.maintenance_announcement,
            offering=self.offering,
        )

    @cached_property
    def maintenance_announcement_template(self):
        return marketplace_factories.MaintenanceAnnouncementTemplateFactory(
            service_provider=self.service_provider,
        )

    @cached_property
    def maintenance_announcement_offering_template(self):
        return marketplace_factories.MaintenanceAnnouncementOfferingTemplateFactory(
            maintenance_template=self.maintenance_announcement_template,
            offering=self.offering,
        )

    @cached_property
    def user_offering_consent(self):
        """Create consent for admin user so they're visible to service providers"""
        return marketplace_models.UserOfferingConsent.objects.create(
            user=self.admin,
            offering=self.offering,
            version="1.0",
        )
