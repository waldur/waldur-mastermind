import uuid

from ddt import data, ddt
from django import template
from django.db import connection
from django.template.loader import get_template
from django.test import utils as django_test
from django.utils.translation import gettext_lazy as _
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace.templatetags.waldur_marketplace import plan_details
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.test_offerings import BaseOfferingUpdateTest


@ddt
class PlanGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer_fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)

    @data("staff", "owner", "customer_support")
    def test_plans_should_be_visible_to_connected_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.PlanFactory.get_provider_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data("admin", "manager")
    def test_plans_are_not_visible_to_provider_project_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.PlanFactory.get_provider_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    def test_plans_should_be_invisible_to_unauthenticated_users(self):
        url = factories.PlanFactory.get_provider_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @data("owner", "user")
    def test_owner_can_not_list_other_sp_plans(self, user):
        self.client.force_authenticate(getattr(self.customer_fixture, user))
        url = factories.PlanFactory.get_provider_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class PlanCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_PLAN)

    @data("staff", "owner")
    def test_can_create_plan(self, user):
        response = self.create_plan(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertTrue(models.Plan.objects.filter(offering=self.offering).exists())

    @data("user", "customer_support", "admin", "manager")
    def test_can_not_create_plan(self, user):
        response = self.create_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_can_not_create_plan_for_child_offering(self):
        self.offering = factories.OfferingFactory(
            customer=self.customer,
            parent=factories.OfferingFactory(customer=self.customer),
        )
        response = self.create_plan("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(models.Plan.objects.filter(offering=self.offering).exists())

    def test_can_create_plan_for_non_billable_offering(self):
        # A top-level offering that is not invoiced still needs a plan of its
        # own: activation requires one and there is no parent to inherit it from.
        self.offering = factories.OfferingFactory(
            customer=self.customer, billable=False
        )
        response = self.create_plan("owner")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Plan.objects.filter(offering=self.offering).exists())

    def create_plan(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.PlanFactory.get_provider_list_url()
        payload = {
            "name": "plan",
            "offering": factories.OfferingFactory.get_url(self.offering),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "unit": UnitPriceMixin.Units.QUANTITY,
        }
        return self.client.post(url, payload)


@ddt
class PlanUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.url = factories.PlanFactory.get_url(self.plan)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)

    @data("staff", "owner")
    def test_authorized_user_can_update_plan(self, user):
        response = self.update_plan(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.name, "New plan")

    @data("customer_support")
    def test_unauthorized_user_can_not_update_plan(self, user):
        response = self.update_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_it_should_not_be_possible_to_update_plan_for_an_existing_resources(self):
        factories.ResourceFactory(offering=self.offering, plan=self.plan)
        response = self.update_plan("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def update_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {"name": "New plan"})


@ddt
class PlanDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.plan = factories.PlanFactory()
        self.url = factories.PlanFactory.get_url(self.plan)

    def test_staff_user_can_delete_plan(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Plan.objects.filter(pk=self.plan.pk).exists())

    @data("owner", "customer_support", "admin", "global_support")
    def test_unauthorized_user_can_not_delete_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class PlanArchiveTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.url = factories.PlanFactory.get_url(self.plan, "archive")
        CustomerRole.OWNER.add_permission(PermissionEnum.ARCHIVE_OFFERING_PLAN)

    @data("staff", "owner")
    def test_authorized_user_can_archive_plan(self, user):
        response = self.archive_plan(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.plan.refresh_from_db()
        self.assertTrue(self.plan.archived)

    @data("customer_support")
    def test_unauthorized_user_can_not_archive_plan(self, user):
        response = self.archive_plan(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.plan.refresh_from_db()
        self.assertFalse(self.plan.archived)

    def archive_plan(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(self.url)


class PlanRenderTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.offering_component_fix = factories.OfferingComponentFactory(
            offering=self.offering
        )
        self.offering_component_usage = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.USAGE,
            type="ram",
        )
        self.offering_component_one = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="one",
        )
        self.offering_component_one_switch = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ON_PLAN_SWITCH,
            type="switch",
        )
        self.component_fix = factories.PlanComponentFactory(
            component=self.offering_component_fix, plan=self.plan
        )
        self.component_usage = factories.PlanComponentFactory(
            component=self.offering_component_usage, plan=self.plan
        )
        self.component_one = factories.PlanComponentFactory(
            component=self.offering_component_one, plan=self.plan
        )
        self.component_one_switch = factories.PlanComponentFactory(
            component=self.offering_component_one_switch, plan=self.plan
        )

    def test_plan_render(self):
        rendered_plan = plan_details(self.plan)

        context = {
            "plan": self.plan,
            "components": [
                {
                    "name": self.component_fix.component.name,
                    "amount": self.component_fix.amount,
                    "price": self.component_fix.price,
                },
                {
                    "name": self.component_one.component.name,
                    "amount": _("one-time fee"),
                    "price": self.component_one.price,
                },
                {
                    "name": self.component_one_switch.component.name,
                    "amount": _("one-time on plan switch"),
                    "price": self.component_one_switch.price,
                },
            ],
        }
        plan_template = get_template(
            "marketplace/marketplace_plan_template.txt"
        ).template
        rendered_plan_expected = plan_template.render(
            template.Context(context, autoescape=False)
        )

        self.assertEqual(rendered_plan, rendered_plan_expected)


@ddt
class PlanOrganizationGroupsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer)
        self.plan = factories.PlanFactory(offering=self.offering)
        self.url = factories.PlanFactory.get_url(
            self.plan, action="update_organization_groups"
        )
        self.delete_url = factories.PlanFactory.get_url(
            self.plan, action="delete_organization_groups"
        )
        self.organization_group = structure_factories.OrganizationGroupFactory()
        self.organization_group_url = (
            structure_factories.OrganizationGroupFactory.get_url(
                self.organization_group
            )
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)

    @data("staff", "owner")
    def test_user_can_update_organization_groups(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, {"organization_groups": [self.organization_group_url]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.plan.organization_groups.count(), 1)

    @data("customer_support")
    def test_user_cannot_update_organization_groups(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, {"organization_groups": [self.organization_group_url]}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "owner")
    def test_user_can_delete_organization_groups(self, user):
        self.plan.organization_groups.add(self.organization_group)
        self.customer.organization_groups.add(self.organization_group)
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

        self.plan.refresh_from_db()
        self.assertEqual(self.offering.organization_groups.count(), 0)

    @data("customer_support")
    def test_user_cannot_delete_organization_groups(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_get_all_plans(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.PlanFactory.get_provider_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

        self.plan.organization_groups.add(self.organization_group)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)

    def test_owner_can_get_his_plans(self):
        self.client.force_authenticate(self.fixture.owner)
        url = factories.PlanFactory.get_provider_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

        self.customer.organization_groups.add(self.organization_group)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 1)

    def test_filter_offerings_plans_by_organization_groups(self):
        new_customer = structure_factories.CustomerFactory()
        self.client.force_authenticate(self.fixture.owner)
        self.offering.organization_groups.add(self.organization_group)
        url = factories.OfferingFactory.get_list_url()

        response = self.client.get(
            url, {"allowed_customer_uuid": new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        new_customer.organization_groups.add(self.organization_group)
        response = self.client.get(
            url, {"allowed_customer_uuid": new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(
            url, {"allowed_customer_uuid": new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)

        other_organization_group = structure_factories.OrganizationGroupFactory()
        second_other_organization_group = structure_factories.OrganizationGroupFactory()
        self.plan.organization_groups.add(other_organization_group)
        self.plan.organization_groups.add(second_other_organization_group)
        response = self.client.get(
            url, {"allowed_customer_uuid": new_customer.uuid.hex}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 0)


class OfferingUpdatePlansTest(BaseOfferingUpdateTest):
    def update_plan(self, plan, role, payload):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)
        url = factories.PlanFactory.get_url(plan)
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.patch(url, payload)

    def update_quotas(self, plan, role, payload):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)
        url = factories.PlanFactory.get_url(plan, "update_quotas")
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def update_prices(self, plan, role, payload):
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)
        url = factories.PlanFactory.get_url(plan, "update_prices")
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def archive_plan(self, plan, role):
        CustomerRole.OWNER.add_permission(PermissionEnum.ARCHIVE_OFFERING_PLAN)
        url = factories.PlanFactory.get_url(plan, "archive")
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url)

    def create_plan(self, role, payload):
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_PLAN)
        url = factories.PlanFactory.get_provider_list_url()
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def test_it_should_be_possible_to_update_plan_name(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering, name="Old name")

        # Act
        response = self.update_plan(plan, "owner", {"name": "New name"})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertEqual(plan.name, "New name")

    def test_it_should_be_possible_to_update_quotas(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_quotas(plan, "owner", {"quotas": {"ram": 20}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertEqual(plan_component.amount, 20)

    def test_quotas_are_not_allowed_for_usage_based_components(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            billing_type=BillingTypes.USAGE,
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_quotas(plan, "owner", {"quotas": {"ram": 20}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_zero_quotas_are_allowed_for_fixed_components(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        factories.OfferingComponentFactory(offering=self.offering, type="ram")

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_quotas(plan, "owner", {"quotas": {"ram": 0}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_invalid_price_components_are_not_allowed(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_quotas(plan, "owner", {"quotas": {"invalid": 10}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_quotas_are_not_allowed_for_child_offering(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        factories.OfferingComponentFactory(offering=self.offering, type="ram")
        self.offering.parent = factories.OfferingFactory(customer=self.customer)
        self.offering.save()

        # Act
        response = self.update_quotas(plan, "owner", {"quotas": {"ram": 20}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_prices_are_not_allowed_for_child_offering(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        factories.OfferingComponentFactory(offering=self.offering, type="ram")
        self.offering.parent = factories.OfferingFactory(customer=self.customer)
        self.offering.save()

        # Act
        response = self.update_prices(plan, "owner", {"prices": {"ram": 2}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_prices_are_allowed_for_non_billable_offering(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )
        self.offering.billable = False
        self.offering.save()

        # Act
        response = self.update_prices(plan, "owner", {"prices": {"ram": 2}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertEqual(plan_component.price, 2)

    def test_if_there_are_no_resources_using_plan_price_is_updated(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_prices(plan, "owner", {"prices": {"ram": 2}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertEqual(plan_component.price, 2)

    def test_if_there_no_resources_using_plan_future_price_is_updated(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        factories.ResourceFactory(offering=self.offering, plan=plan)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_prices(plan, "owner", {"prices": {"ram": 2}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertEqual(plan_component.future_price, 2)

    def test_future_price_can_be_set_to_zero(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        factories.ResourceFactory(offering=self.offering, plan=plan)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )
        models.PlanComponent.objects.create(
            plan=plan, component=offering_component, price=10
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_prices(plan, "owner", {"prices": {"ram": 0}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertEqual(plan_component.future_price, 0)
        self.assertEqual(plan_component.price, 10)

    def test_price_can_be_set_to_zero_if_there_are_no_resources(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )
        models.PlanComponent.objects.create(
            plan=plan, component=offering_component, price=10
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_prices(plan, "owner", {"prices": {"ram": 0}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertEqual(plan_component.price, 0)

    def test_future_price_is_not_set_if_new_price_matches_current_price(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)
        factories.ResourceFactory(offering=self.offering, plan=plan)
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering, type="ram"
        )
        models.PlanComponent.objects.create(
            plan=plan, component=offering_component, price=10
        )

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.update_prices(plan, "owner", {"prices": {"ram": 10}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        plan_component = models.PlanComponent.objects.get(
            plan=plan, component=offering_component
        )
        self.assertIsNone(plan_component.future_price)

    def test_it_should_be_possible_to_archive_plan(self):
        # Arrange
        plan = factories.PlanFactory(offering=self.offering)

        # Act
        response = self.archive_plan(plan, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        plan.refresh_from_db()
        self.assertTrue(plan.archived)

    def test_it_should_be_possible_to_add_new_plan(self):
        factories.OfferingComponentFactory(offering=self.offering, type="cores")
        response = self.create_plan(
            "owner",
            {
                "offering": factories.OfferingFactory.get_url(self.offering),
                "name": "small",
                "unit": UnitPriceMixin.Units.PER_MONTH,
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(1, self.offering.plans.count())


class PlanSumComponentsTest(test.APITestCase):
    """Tests for Plan.sum_components() method which calculates price * amount."""

    def setUp(self):
        self.offering = factories.OfferingFactory()
        self.plan = factories.PlanFactory(offering=self.offering)

    def test_one_time_component_with_zero_amount_returns_zero(self):
        """One-time component with amount=0 should contribute 0 to init_price."""
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="setup_fee",
        )
        factories.PlanComponentFactory(
            component=offering_component,
            plan=self.plan,
            price=100,
            amount=0,  # Zero amount
        )

        # init_price uses sum_components(BillingTypes.ONE_TIME)
        self.assertEqual(self.plan.init_price, 0)

    def test_one_time_component_with_nonzero_amount(self):
        """One-time component with amount > 0 should correctly calculate price * amount."""
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="setup_fee",
        )
        factories.PlanComponentFactory(
            component=offering_component,
            plan=self.plan,
            price=100,
            amount=3,
        )

        self.assertEqual(self.plan.init_price, 300)  # 100 * 3

    def test_fixed_component_with_zero_amount_returns_zero(self):
        """Fixed component with amount=0 should contribute 0 to fixed_price."""
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.FIXED,
            type="cpu",
        )
        factories.PlanComponentFactory(
            component=offering_component,
            plan=self.plan,
            price=50,
            amount=0,
        )

        self.assertEqual(self.plan.fixed_price, 0)

    def test_fixed_component_with_nonzero_amount(self):
        """Fixed component with amount > 0 should correctly calculate price * amount."""
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.FIXED,
            type="cpu",
        )
        factories.PlanComponentFactory(
            component=offering_component,
            plan=self.plan,
            price=50,
            amount=4,
        )

        self.assertEqual(self.plan.fixed_price, 200)  # 50 * 4

    def test_switch_component_with_zero_amount_returns_zero(self):
        """On plan switch component with amount=0 should contribute 0 to switch_price."""
        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ON_PLAN_SWITCH,
            type="migration_fee",
        )
        factories.PlanComponentFactory(
            component=offering_component,
            plan=self.plan,
            price=25,
            amount=0,
        )

        self.assertEqual(self.plan.switch_price, 0)

    def test_multiple_components_sum_correctly(self):
        """Multiple components should sum their individual price * amount values."""
        component1 = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="setup",
        )
        component2 = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="activation",
        )

        factories.PlanComponentFactory(
            component=component1,
            plan=self.plan,
            price=100,
            amount=2,  # 200
        )
        factories.PlanComponentFactory(
            component=component2,
            plan=self.plan,
            price=50,
            amount=3,  # 150
        )

        self.assertEqual(self.plan.init_price, 350)  # 200 + 150

    def test_mixed_zero_and_nonzero_amounts(self):
        """Components with mixed zero and non-zero amounts should calculate correctly."""
        component1 = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="fee1",
        )
        component2 = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.ONE_TIME,
            type="fee2",
        )

        factories.PlanComponentFactory(
            component=component1,
            plan=self.plan,
            price=100,
            amount=0,  # 0 (zero amount)
        )
        factories.PlanComponentFactory(
            component=component2,
            plan=self.plan,
            price=75,
            amount=2,  # 150
        )

        self.assertEqual(self.plan.init_price, 150)  # 0 + 150


class PublicOfferingPlanQueryCountTest(test.APITestCase):
    """Serializing plans must not cost queries proportional to plan count.

    ``BasePlanSerializer`` exposes six method fields that each iterate
    ``plan.components.all()`` and dereference ``component``, plus a
    per-plan resource count, so without prefetching the public offering
    endpoint scales with plans x components.
    """

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, state=models.Offering.States.ACTIVE
        )

    def _add_plan(self, component_count=4):
        plan = factories.PlanFactory(offering=self.offering)
        for _i in range(component_count):
            component = factories.OfferingComponentFactory(
                offering=self.offering, type=f"comp-{uuid.uuid4().hex[:8]}"
            )
            factories.PlanComponentFactory(plan=plan, component=component)
        return plan

    def _get(self):
        url = factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response

    def test_query_count_does_not_grow_with_number_of_plans(self):
        self._add_plan()
        # Warm ContentType and permission caches; they otherwise skew the count.
        self._get()

        with django_test.CaptureQueriesContext(connection) as baseline:
            self._get()

        for _i in range(4):
            self._add_plan()

        with self.assertNumQueries(len(baseline)):
            self._get()

    def test_plan_payload_is_unchanged_by_prefetching(self):
        plan = self._add_plan()
        response = self._get()

        (payload,) = [p for p in response.data["plans"] if p["uuid"] == plan.uuid.hex]
        self.assertEqual(len(payload["prices"]), 4)
        self.assertEqual(len(payload["quotas"]), 4)
        self.assertEqual(payload["resources_count"], 0)
        self.assertEqual(payload["plan_type"], "fixed")


class QuotaUpdateComponentTypesTest(BaseOfferingUpdateTest):
    """Which components accept an amount.

    Restricted to FIXED, this endpoint rejected the whole request whenever the
    form offered a one-time component beside a fixed one, so nothing could be
    saved and every setup fee sat at the field's default of zero.
    """

    def setUp(self):
        super().setUp()
        self.plan = factories.PlanFactory(offering=self.offering)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PLAN)
        self.url = factories.PlanFactory.get_url(self.plan, "update_quotas")
        self.client.force_authenticate(self.fixture.owner)

    def _component(self, comp_type, billing_type, is_prepaid=False):
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            type=comp_type,
            billing_type=billing_type,
            is_prepaid=is_prepaid,
        )
        factories.PlanComponentFactory(
            plan=self.plan, component=component, price=10, amount=0
        )
        return component

    def _amount(self, comp_type):
        return models.PlanComponent.objects.get(
            plan=self.plan, component__type=comp_type
        ).amount

    def test_a_setup_fee_accepts_an_amount(self):
        self._component("setup", BillingTypes.ONE_TIME)

        response = self.client.post(self.url, {"quotas": {"setup": 3}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._amount("setup"), 3)

    def test_a_switch_fee_accepts_an_amount(self):
        self._component("migration", BillingTypes.ON_PLAN_SWITCH)

        response = self.client.post(self.url, {"quotas": {"migration": 2}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._amount("migration"), 2)

    def test_a_fixed_component_still_accepts_one(self):
        self._component("licence", BillingTypes.FIXED)

        response = self.client.post(self.url, {"quotas": {"licence": 4}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._amount("licence"), 4)

    def test_a_setup_fee_beside_a_fixed_one_no_longer_fails_the_request(self):
        # The form shows both, so it sends both. Rejecting the pair left the
        # fixed component unsettable too.
        self._component("setup", BillingTypes.ONE_TIME)
        self._component("licence", BillingTypes.FIXED)

        response = self.client.post(self.url, {"quotas": {"setup": 3, "licence": 4}})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self._amount("setup"), 3)
        self.assertEqual(self._amount("licence"), 4)

    def test_a_prepaid_component_is_refused(self):
        # Its quantity comes from the requested limit and the subscription's
        # length; an amount here would be settable and then ignored.
        self._component("support", BillingTypes.ONE_TIME, is_prepaid=True)

        response = self.client.post(self.url, {"quotas": {"support": 3}})

        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertEqual(self._amount("support"), 0)

    def test_a_component_the_form_did_not_show_keeps_its_amount(self):
        self._component("setup", BillingTypes.ONE_TIME)
        self._component("licence", BillingTypes.FIXED)
        self.client.post(self.url, {"quotas": {"setup": 3, "licence": 4}})

        self.client.post(self.url, {"quotas": {"setup": 5}})

        self.assertEqual(self._amount("setup"), 5)
        self.assertEqual(self._amount("licence"), 4)
