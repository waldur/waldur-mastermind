import base64
import datetime
import json
import os
import tempfile
import uuid
from itertools import product
from unittest import mock

from constance.test.unittest import override_config
from cryptography import x509
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from ddt import data, ddt, idata
from django.core.exceptions import ObjectDoesNotExist
from rest_framework import exceptions as rest_exceptions
from rest_framework import status, test

from waldur_core.core import utils as core_utils
from waldur_core.core.tests.helpers import load_json_resource
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import (
    CustomerRole,
    OfferingRole,
    ProjectRole,
    ServiceProviderRole,
)
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.common.mixins import UnitPriceMixin
from waldur_mastermind.invoices.tests import factories as invoices_factories
from waldur_mastermind.marketplace import models, serializers, utils
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OrderStates,
    ResourceStates,
)
from waldur_mastermind.marketplace.management.commands.export_offering import (
    export_offering,
)
from waldur_mastermind.marketplace.management.commands.import_offering import (
    create_offering,
    update_offering,
)
from waldur_mastermind.marketplace.plugins import manager
from waldur_mastermind.marketplace.tests import factories
from waldur_mastermind.marketplace.tests.factories import OFFERING_OPTIONS
from waldur_mastermind.marketplace_support import PLUGIN_NAME
from waldur_mastermind.marketplace_vmware import VIRTUAL_MACHINE_TYPE

from . import fixtures as marketplace_fixtures


@ddt
class OfferingGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True,
            project=self.fixture.project,
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )

    @data("staff", "global_support", "owner", "customer_support", "admin", "manager")
    def test_offerings_should_be_visible_to_staff_and_related_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data(
        "user",
    )
    def test_offerings_should_be_not_visible_to_unrelated_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_offerings_should_be_invisible_to_unauthenticated_users(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 0)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_offerings_should_be_visible_to_unauthenticated_users(self):
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    def test_field_query_param(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url, {"field": ["organization_groups"]})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)
        self.assertEqual(len(response.json()[0].keys()), 1)
        self.assertEqual(list(response.json()[0].keys())[0], "organization_groups")


class OfferingExtraFieldsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering_1 = factories.OfferingFactory(shared=True)
        self.offering_2 = factories.OfferingFactory(shared=True)
        self.url = factories.OfferingFactory.get_list_url()
        self.detail_url = factories.OfferingFactory.get_url(self.offering_2)

    def test_total_customers(self):
        self.client.force_authenticate(self.fixture.staff)

        factories.ResourceFactory(
            offering=self.offering_2,
            state=ResourceStates.OK,
        )

        self._check_field_after_set_of_it("total_customers", 1)

    def test_total_cost_estimated(self):
        self.client.force_authenticate(self.fixture.staff)

        invoice_item = invoices_factories.InvoiceItemFactory()
        resource = factories.ResourceFactory(
            project=invoice_item.project,
            offering=self.offering_2,
            state=ResourceStates.OK,
        )
        invoice_item.resource = resource
        invoice_item.unit_price = 10
        invoice_item.quantity = 2
        invoice_item.save()

        self._check_field_after_set_of_it("total_cost_estimated", 20)

    def test_total_cost(self):
        self.client.force_authenticate(self.fixture.staff)

        invoice_item = invoices_factories.InvoiceItemFactory()
        resource = factories.ResourceFactory(
            project=invoice_item.project,
            offering=self.offering_2,
            state=ResourceStates.OK,
        )
        invoice_item.resource = resource
        invoice_item.unit_price = 10
        invoice_item.quantity = 3
        invoice_item.save()

        last_month = core_utils.get_last_month()
        invoice_item.invoice.year = last_month.year
        invoice_item.invoice.month = last_month.month
        invoice_item.invoice.save()

        self._check_field_after_set_of_it("total_cost", 30)

    def _check_field_before_set_of_it(self, field_name):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)
        self.assertFalse(field_name in response.json()[0].keys())

        response = self.client.get(self.detail_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(field_name in response.json().keys())

    def _check_field_after_set_of_it(self, field_name, value):
        response = self.client.get(self.url, {"o": "-%s" % field_name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(response.json()[0][field_name], value)
        self.assertEqual(response.json()[1][field_name], 0)

        response = self.client.get(self.url, {"o": field_name})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 2)
        self.assertEqual(response.json()[0][field_name], 0)
        self.assertEqual(response.json()[1][field_name], value)


class OfferingPlanInfoTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(shared=True)
        self.url = factories.OfferingFactory.get_url(self.offering)

        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.FIXED,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )

    def test_plan_info(self):
        self.client.force_authenticate(self.fixture.staff)
        self._check_plan_info(models.OfferingComponent.BillingTypes.FIXED, "fixed")
        self._check_plan_info(
            models.OfferingComponent.BillingTypes.USAGE, "usage-based"
        )
        self._check_plan_info(
            models.OfferingComponent.BillingTypes.ONE_TIME, "one-time"
        )
        self._check_plan_info(
            models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH, "on-plan-switch"
        )
        self._check_plan_info(models.OfferingComponent.BillingTypes.LIMIT, "limit")

        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=models.OfferingComponent.BillingTypes.FIXED,
            type="ram",
            name="RAM",
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=offering_component
        )

        self._check_plan_info(
            models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH, "mixed"
        )

    def test_minimal_price(self):
        self.client.force_authenticate(self.fixture.staff)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.LIMIT
        )
        self.plan_component.price = 10
        self._check_minimal_price(10)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.FIXED
        )
        self.plan_component.price = 100
        self.plan_component.amount = 0
        self._check_minimal_price(100)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.FIXED
        )
        self.plan_component.price = 100
        self.plan_component.amount = 1
        self._check_minimal_price(100)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.ONE_TIME
        )
        self.plan_component.price = 200
        self._check_minimal_price(200)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.ON_PLAN_SWITCH
        )
        self.plan_component.price = 300
        self._check_minimal_price(0)

        self.offering_component.billing_type = (
            models.OfferingComponent.BillingTypes.USAGE
        )
        self.plan_component.price = 500
        self._check_minimal_price(0)

    def _check_minimal_price(self, minimal_price):
        self.offering_component.save()
        self.plan_component.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["plans"][0]["minimal_price"], minimal_price)

    def _check_plan_info(self, billing_type, plan_type):
        self.offering_component.billing_type = billing_type
        self.offering_component.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["plans"][0]["plan_type"], plan_type)


@ddt
class SecretOptionsTests(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer, project=self.fixture.project
        )
        self.url = factories.OfferingFactory.get_url(self.offering)

    @data("staff", "owner")
    def test_secret_options_are_visible_to_authorized_user(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue("secret_options" in response.data)

    @data("customer_support", "admin", "manager")
    def test_secret_options_are_not_visible_to_unauthorized_user(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse("secret_options" in response.data)


class OfferingFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        attributes = {
            "cloudDeploymentModel": "private_cloud",
            "userSupportOption": ["phone"],
        }
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer, attributes=attributes, shared=False
        )
        self.url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)

    def test_filter_choice_positive(self):
        response = self.client.get(
            self.url,
            {
                "attributes": json.dumps(
                    {
                        "cloudDeploymentModel": "private_cloud",
                    }
                )
            },
        )
        self.assertEqual(len(response.data), 1)

    def test_filter_choice_negative(self):
        response = self.client.get(
            self.url,
            {
                "attributes": json.dumps(
                    {
                        "cloudDeploymentModel": "public_cloud",
                    }
                )
            },
        )
        self.assertEqual(len(response.data), 0)

    def test_filter_list_positive(self):
        """
        If an attribute is a list, we use multiple choices.
        """
        factories.OfferingFactory(
            attributes={
                "userSupportOption": ["phone", "email", "fax"],
            }
        )
        factories.OfferingFactory(
            attributes={
                "userSupportOption": ["email"],
            }
        )
        response = self.client.get(
            self.url,
            {
                "attributes": json.dumps(
                    {
                        "userSupportOption": ["fax", "email"],
                    }
                )
            },
        )
        self.assertEqual(len(response.data), 2)

    def test_shared_offerings_are_not_available_for_all_users(self):
        # Arrange
        factories.OfferingFactory(customer=self.fixture.customer, shared=False)
        self.offering.shared = True
        self.offering.save()

        # Act
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)

        # Assert
        self.assertEqual(len(response.data), 0)

    def test_private_offerings_are_not_available_for_users_in_other_customers(self):
        fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

    def test_private_offering_is_available_for_users_in_related_project(self):
        fixture = fixtures.ProjectFixture()
        self.offering.project = fixture.project
        self.offering.save()
        self.client.force_authenticate(fixture.manager)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_private_offering_is_not_available_for_users_in_other_project_of_the_same_customer(
        self,
    ):
        fixture = fixtures.ProjectFixture()
        self.offering.project = fixture.project
        self.offering.save()

        other_manager = structure_factories.UserFactory()
        other_project = structure_factories.ProjectFactory(
            customer=fixture.project.customer
        )
        other_project.add_user(other_manager, ProjectRole.MANAGER)

        self.client.force_authenticate(other_manager)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

    def test_private_offerings_are_not_available_for_users_in_other_projects(self):
        fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(fixture.manager)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 0)

    def test_private_offerings_are_available_for_users_in_original_customer(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_private_offerings_are_available_for_staff(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_private_offerings_are_available_for_support(self):
        self.client.force_authenticate(self.fixture.global_support)
        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 1)

    def test_filter_offerings_for_service_manager(self):
        # Arrange
        factories.OfferingFactory(customer=self.fixture.customer, shared=False)

        self.offering.shared = True
        self.offering.save()
        self.offering.add_user(self.fixture.user, OfferingRole.MANAGER)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            self.url, {"service_manager_uuid": self.fixture.user.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.offering.uuid.hex)

    def test_filter_limited_shared_offerings_for_customer_uuid_if_organization_groups_match(
        self,
    ):
        # Arrange
        self.offering.delete()
        offering = factories.OfferingFactory(
            shared=True, customer=self.fixture.customer
        )
        url = factories.OfferingFactory.get_list_url()
        organization_group = structure_factories.OrganizationGroupFactory()
        offering.organization_groups.add(organization_group)

        self.fixture.customer.organization_groups.add(organization_group)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            url, {"allowed_customer_uuid": self.fixture.customer.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], offering.uuid.hex)

    def test_filter_limited_shared_offerings_for_customer_uuid_if_organization_groups_do_not_match(
        self,
    ):
        # Arrange
        self.offering.delete()
        offering = factories.OfferingFactory(shared=True)
        url = factories.OfferingFactory.get_list_url()
        organization_group = structure_factories.OrganizationGroupFactory()
        offering.organization_groups.add(organization_group)

        # Act
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            url, {"allowed_customer_uuid": self.fixture.customer.uuid.hex}
        )

        # Assert
        self.assertEqual(len(response.data), 0)

    def test_filter_keyword(self):
        factories.OfferingFactory(name="name keyword")
        factories.OfferingFactory(description="description Keyword")
        offering = factories.OfferingFactory()
        offering.customer.name = "name keyword"
        offering.customer.save()
        url = factories.OfferingFactory.get_list_url()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(len(response.data), 4)
        response = self.client.get(url, {"keyword": "keyword"})
        self.assertEqual(len(response.data), 3)


class OfferingPlansFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.shared = True
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        self.plan = self.fixture.plan
        self.url = factories.OfferingFactory.get_public_url(self.offering)

    def test_anonymous_user_cannot_get_plans_matched_with_organization_groups(self):
        url = factories.OfferingFactory.get_public_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)

        organization_group = structure_factories.OrganizationGroupFactory()
        self.plan.organization_groups.add(organization_group)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 0)

    def test_staff_can_get_all_plans(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)

        organization_group = structure_factories.OrganizationGroupFactory()
        self.plan.organization_groups.add(organization_group)

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)

    def test_filtering_plans_by_owner(self):
        self.client.force_authenticate(self.fixture.owner)

        # user can get plans if they are not connected with organization_groups
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)

        organization_group = structure_factories.OrganizationGroupFactory()
        self.plan.organization_groups.add(organization_group)

        # user cannot get plans if they are connected with organization_groups
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 0)

        self.fixture.customer.organization_groups.add(organization_group)

        # user can get plans if they are connected with the same organization_groups
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)

    def test_filtering_plans_by_admin(self):
        self.client.force_authenticate(self.fixture.admin)

        # user can get plans if they are not connected with organization_groups
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)

        organization_group = structure_factories.OrganizationGroupFactory()
        self.plan.organization_groups.add(organization_group)

        # user cannot get plans if they are connected with organization_groups
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 0)

        self.fixture.project.customer.organization_groups.add(organization_group)

        # user can get plans if they are connected with the same organization_groups
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["plans"]), 1)


@ddt
class OfferingCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

    @data("staff", "owner")
    def test_authorized_user_can_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def test_options_default_value(self):
        response = self.create_offering("staff")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(offering.options, {"options": {}, "order": []})

    def test_validate_correct_geolocations(self):
        response = self.create_offering(
            "staff", add_payload={"latitude": 123, "longitude": 345}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    @data("user", "customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_create_offering(self, user):
        response = self.create_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)

    def test_create_offering_with_attributes(self):
        response = self.create_offering("staff", attributes=True)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(
            offering.attributes,
            {
                "cloudDeploymentModel": "private_cloud",
                "vendorType": "reseller",
                "userSupportOptions": ["web_chat", "phone"],
                "dataProtectionInternal": "ipsec",
                "dataProtectionExternal": "tls12",
            },
        )

    def test_dont_create_offering_if_attributes_is_not_valid(self):
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.attribute = factories.AttributeFactory(
            section=self.section, key="userSupportOptions"
        )
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()

        payload = {
            "name": "offering",
            "native_name": "native_name",
            "category": factories.CategoryFactory.get_url(category=self.category),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "attributes": json.dumps(
                {
                    "cloudDeploymentModel": "private_cloud",
                    "vendorType": "reseller",
                    "userSupportOptions": ["chat", "phone"],
                    "dataProtectionInternal": "ipsec",
                    "dataProtectionExternal": "tls12",
                }
            ),
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_is_not_created_if_attributes_are_not_provided(self):
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()

        payload = {
            "name": "offering",
            "category": factories.CategoryFactory.get_url(category=self.category),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "attributes": '"String is not allowed, dictionary is expected."',
        }
        response = self.client.post(url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_offering_with_plans(self):
        plans_request = {
            "plans": [
                {
                    "name": "Small",
                    "description": "Basic plan",
                }
            ]
        }
        response = self.create_offering("owner", add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(len(response.data["plans"]), 1)

    def test_specify_max_amount_for_plan(self):
        plans_request = {
            "plans": [
                {
                    "name": "Small",
                    "description": "Basic plan",
                    "max_amount": 10,
                }
            ]
        }
        response = self.create_offering("owner", add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["plans"][0]["max_amount"], 10)

    def test_max_amount_should_be_at_least_one(self):
        plans_request = {
            "plans": [
                {
                    "name": "Small",
                    "description": "Basic plan",
                    "max_amount": -1,
                }
            ]
        }
        response = self.create_offering("owner", add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_offering_with_custom_components(self):
        plans_request = {
            "components": [
                {
                    "type": "cores",
                    "name": "Cores",
                    "measured_unit": "hours",
                    "billing_type": "fixed",
                }
            ],
        }
        response = self.create_offering("owner", add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        component = offering.components.get(type="cores")
        self.assertEqual(
            component.billing_type, models.OfferingComponent.BillingTypes.FIXED
        )

    def test_component_name_should_not_contain_spaces(self):
        plans_request = {
            "components": [
                {
                    "type": "vCPU cores",
                    "name": "Cores",
                    "measured_unit": "hours",
                    "billing_type": "fixed",
                }
            ],
        }
        response = self.create_offering("owner", add_payload=plans_request)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_offering_with_options(self):
        response = self.create_offering(
            "staff", attributes=True, add_payload={"options": OFFERING_OPTIONS}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())
        offering = models.Offering.objects.get(customer=self.customer)
        self.assertEqual(offering.options, OFFERING_OPTIONS)

    def test_create_offering_with_invalid_options(self):
        options = {"foo": "bar"}
        response = self.create_offering(
            "staff", attributes=True, add_payload={"options": options}
        )
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_offering_with_invalid_type(self):
        response = self.create_offering(
            "staff", attributes=True, add_payload={"type": "invalid"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue("type" in response.data)

    def test_validate_required_attribute(self):
        user = getattr(self.fixture, "staff")
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        factories.ServiceProviderFactory(customer=self.customer)
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key="required_attribute", required=True
        )
        payload = {
            "name": "offering",
            "category": factories.CategoryFactory.get_url(category),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": PLUGIN_NAME,
            "attributes": {"vendorType": "reseller"},
        }

        response = self.client.post(url, payload)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertTrue(b"required_attribute" in response.content)

    def test_default_attribute_value_is_used_if_user_did_not_override_it(self):
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key="support_phone", default="support@example.com"
        )

        response = self.create_offering(
            "staff",
            add_payload={
                "category": factories.CategoryFactory.get_url(category),
                "attributes": {},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["attributes"]["support_phone"], "support@example.com"
        )

    def test_default_attribute_value_is_not_used_if_user_has_overriden_it(self):
        category = factories.CategoryFactory()
        section = factories.SectionFactory(category=category)
        factories.AttributeFactory(
            section=section, key="support_phone", default="support@example.com"
        )

        response = self.create_offering(
            "staff",
            add_payload={
                "category": factories.CategoryFactory.get_url(category),
                "attributes": {"support_phone": "admin@example.com"},
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(
            response.data["attributes"]["support_phone"], "admin@example.com"
        )

    def create_offering(self, user, attributes=False, add_payload=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        payload = {
            "name": "offering",
            "category": factories.CategoryFactory.get_url(),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": PLUGIN_NAME,
            "plans": [
                {
                    "name": "Small",
                    "unit": UnitPriceMixin.Units.PER_MONTH,
                }
            ],
        }

        if attributes:
            payload["attributes"] = {
                "cloudDeploymentModel": "private_cloud",
                "vendorType": "reseller",
                "userSupportOptions": ["web_chat", "phone"],
                "dataProtectionInternal": "ipsec",
                "dataProtectionExternal": "tls12",
            }

        if add_payload:
            payload.update(add_payload)

        return self.client.post(url, payload)

    def test_offering_creating_is_not_available_for_blocked_organization(self):
        self.customer.blocked = True
        self.customer.save()
        response = self.create_offering("owner")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_create_offering_with_minimal_information_in_draft_state(self):
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_list_url()
        self.provider = factories.ServiceProviderFactory(customer=self.customer)

        for offering_type in list(manager.backends.keys()):
            payload = {
                "name": "offering",
                "category": factories.CategoryFactory.get_url(),
                "customer": structure_factories.CustomerFactory.get_url(self.customer),
                "type": offering_type,
            }
            response = self.client.post(url, payload)
            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, offering_type
            )
            self.assertTrue(
                models.Offering.objects.filter(
                    customer=self.customer, type=offering_type
                ).exists()
            )
            offering = models.Offering.objects.filter(
                customer=self.customer, type=offering_type
            ).get()
            self.assertEqual(offering.state, OfferingStates.DRAFT)


class BaseOfferingUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer,
            project=self.fixture.project,
            shared=True,
            state=OfferingStates.DRAFT,
        )
        for role in (
            CustomerRole.OWNER,
            ServiceProviderRole.MANAGER,
            OfferingRole.MANAGER,
        ):
            role.add_permission(PermissionEnum.UPDATE_OFFERING)


@ddt
class OfferingUpdateOverviewTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        for role in (
            CustomerRole.OWNER,
            ServiceProviderRole.MANAGER,
            OfferingRole.MANAGER,
        ):
            role.add_permission(PermissionEnum.UPDATE_OFFERING)

    def update_overview(self, role):
        url = factories.OfferingFactory.get_url(self.offering, "update_overview")
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, {"name": "new_offering"})

    @data("staff", "owner")
    def test_staff_and_owner_can_update_offering_in_draft_state(self, role):
        response = self.update_overview(role)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, "new_offering")

    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_update_offering(self, role):
        response = self.update_overview(role)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unrelated_user_can_not_update_offering(self):
        response = self.update_overview("user")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data(
        OfferingStates.ACTIVE,
        OfferingStates.PAUSED,
        OfferingStates.ARCHIVED,
    )
    def test_owner_can_not_update_offering_in_active_or_paused_state(self, state):
        # Arrange
        self.offering.state = state
        self.offering.save()

        # Act
        response = self.update_overview("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(OfferingStates.ACTIVE, OfferingStates.PAUSED)
    def test_staff_can_update_offering_in_active_or_paused_state(self, state):
        # Arrange
        self.offering.state = state
        self.offering.save()

        # Act
        response = self.update_overview("staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_staff_can_not_update_offering_in_archived_state(self):
        # Arrange
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()

        # Act
        response = self.update_overview("staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_updating_is_not_available_for_blocked_organization(self):
        self.customer.blocked = True
        self.customer.save()

        response = self.update_overview("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class OfferingUpdateAttributesTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        self.fixture.service_manager = UserFactory()
        self.offering.add_user(self.fixture.service_manager, OfferingRole.MANAGER)
        for role in (
            CustomerRole.OWNER,
            ServiceProviderRole.MANAGER,
            OfferingRole.MANAGER,
        ):
            role.add_permission(PermissionEnum.UPDATE_OFFERING_ATTRIBUTES)

    def update_attributes(self, attributes, role):
        url = factories.OfferingFactory.get_url(self.offering, "update_attributes")
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, attributes)

    def test_attributes_are_validated(self):
        # Arrange
        section = factories.SectionFactory(category=self.offering.category)
        factories.AttributeFactory(
            section=section, key="userSupportOptions", required=True
        )

        # Act
        response = self.update_attributes({"userSupportOptions": "email"}, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @idata(
        product(
            (
                OfferingStates.DRAFT,
                OfferingStates.ACTIVE,
                OfferingStates.PAUSED,
            ),
            ("staff", "owner", "service_manager"),
        )
    )
    def test_authorized_user_can_update_offering_attributes_in_valid_state(self, pair):
        state, role = pair
        # Arrange
        self.offering.state = state
        self.offering.save()

        # Act
        response = self.update_attributes({"key": "value"}, role)

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.attributes, {"key": "value"})

    @data("staff", "owner", "service_manager")
    def test_authorized_user_can_not_update_offering_attributes_in_archived_state(
        self, role
    ):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()

        response = self.update_attributes({"key": "value"}, role)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OfferingComponentRemoveTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

    def remove_offering_component(self, component, role):
        url = factories.OfferingFactory.get_url(
            self.offering, "remove_offering_component"
        )
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, {"uuid": component.uuid.hex})

    def test_it_should_not_be_possible_to_remove_builtin_components(self):
        # Arrange
        self.offering.type = VIRTUAL_MACHINE_TYPE
        self.offering.save()

        cpu_component = factories.OfferingComponentFactory(
            offering=self.offering, type="cpu"
        )

        # Act
        response = self.remove_offering_component(cpu_component, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        cpu_component.refresh_from_db()

    def test_it_should_not_be_possible_to_remove_components_if_they_are_used(self):
        # Arrange
        component = factories.OfferingComponentFactory(offering=self.offering)
        factories.ResourceFactory(offering=self.offering)

        # Act
        response = self.remove_offering_component(component, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_it_should_be_possible_to_remove_components_if_they_are_not_used(self):
        # Arrange
        component = factories.OfferingComponentFactory(offering=self.offering)

        # Act
        response = self.remove_offering_component(component, "owner")

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(0, self.offering.components.count())


class OfferingComponentCreateTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

    def create_offering_component(self, role, payload):
        url = factories.OfferingFactory.get_url(
            self.offering, "create_offering_component"
        )
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def test_validation_of_offering_and_type(self):
        # Act
        response = self.create_offering_component(
            "owner",
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # Act
        response = self.create_offering_component(
            "owner",
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_it_should_be_possible_to_create_new_components(self):
        # Act
        response = self.create_offering_component(
            "owner",
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
            },
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        component = self.offering.components.get()
        self.assertEqual("cores", component.type)
        self.assertEqual("hours", component.measured_unit)
        self.assertEqual(
            models.OfferingComponent.BillingTypes.FIXED, component.billing_type
        )


class OfferingComponentUpdateTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)

    def update_offering_component(self, payload, role):
        url = factories.OfferingFactory.get_url(
            self.offering, "update_offering_component"
        )
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, payload)

    def test_it_should_be_possible_to_update_existing_components(self):
        component = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cores",
            name="CPU",
            measured_unit="H",
        )
        # Act
        response = self.update_offering_component(
            {
                "type": "cores",
                "name": "Cores",
                "measured_unit": "hours",
                "billing_type": "fixed",
                "uuid": component.uuid.hex,
            },
            "owner",
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        component = self.offering.components.get()
        self.assertEqual("Cores", component.name)
        self.assertEqual("hours", component.measured_unit)
        self.assertEqual(
            models.OfferingComponent.BillingTypes.FIXED, component.billing_type
        )


@ddt
class OfferingPartialUpdateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(customer=self.customer, shared=True)

        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_ATTRIBUTES)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_LOCATION)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_DESCRIPTION)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)

    def test_update_offering_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        response = self.client.post(url, {"backend_id": "new_backend_id"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.backend_id, "new_backend_id")

    def test_update_openstack_tenant_password(self):
        self.offering.type = "OpenStack.Tenant"
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        response = self.client.post(
            url, {"service_attributes": {"password": "new_password"}}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.scope.password, "new_password")

    def test_update_openstack_tenant_password_keeps_backend_url(self):
        # Arrange
        self.offering.type = "OpenStack.Tenant"
        backend_url = "http://example.com"
        self.offering.scope = structure_factories.ServiceSettingsFactory(
            backend_url=backend_url, password="old_password"
        )
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")

        # Act
        response = self.client.post(
            url, {"service_attributes": {"password": "new_password"}}
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.scope.refresh_from_db()
        self.assertEqual(self.offering.scope.password, "new_password")
        self.assertEqual(self.offering.scope.backend_url, backend_url)

    def test_update_openstack_tenant_verify_ssl(self):
        # Arrange
        self.offering.type = "OpenStack.Tenant"
        self.offering.scope = structure_factories.ServiceSettingsFactory()
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")

        # Act
        response = self.client.post(url, {"service_attributes": {"verify_ssl": False}})

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.scope.refresh_from_db()
        self.assertEqual(self.offering.scope.options.get("verify_ssl"), False)

        # Test updating to True
        response = self.client.post(url, {"service_attributes": {"verify_ssl": True}})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.scope.refresh_from_db()
        self.assertEqual(self.offering.scope.options.get("verify_ssl"), True)

    def test_update_openstack_tenant_certificate(self):
        self.offering.type = "OpenStack.Tenant"
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")

        # Generate a private key
        private_key = rsa.generate_private_key(
            public_exponent=65537, key_size=2048, backend=default_backend()
        )

        # Create a self-signed certificate
        subject = issuer = x509.Name(
            [
                x509.NameAttribute(NameOID.COUNTRY_NAME, "US"),
                x509.NameAttribute(NameOID.STATE_OR_PROVINCE_NAME, "California"),
                x509.NameAttribute(NameOID.LOCALITY_NAME, "Sunnyvale"),
                x509.NameAttribute(NameOID.ORGANIZATION_NAME, "MyCompany"),
                x509.NameAttribute(NameOID.COMMON_NAME, "mycompany.com"),
            ]
        )
        certificate = (
            x509.CertificateBuilder()
            .subject_name(subject)
            .issuer_name(issuer)
            .public_key(private_key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.datetime.utcnow())
            .not_valid_after(datetime.datetime.utcnow() + datetime.timedelta(days=365))
            .add_extension(
                x509.SubjectAlternativeName([x509.DNSName("localhost")]),
                critical=False,
            )
            .sign(private_key, hashes.SHA256(), default_backend())
        )

        # Convert the certificate to PEM format
        valid_certificate = certificate.public_bytes(serialization.Encoding.PEM).decode(
            "utf-8"
        )

        response = self.client.post(
            url,
            {"secret_options": {"openstack_api_tls_certificate": valid_certificate}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertTrue(
            self.offering.secret_options.get("openstack_api_tls_certificate")
        )
        self.assertTrue(self.offering.scope.options["certificate"])

        response = self.client.post(
            url,
            {"secret_options": {"openstack_api_tls_certificate": ""}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertFalse(
            self.offering.secret_options.get("openstack_api_tls_certificate")
        )
        self.assertFalse(self.offering.scope.options.get("certificate"))

    @data("staff", "owner")
    def test_update_location(self, user):
        url = factories.OfferingFactory.get_url(self.offering, "update_location")
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(url, {"latitude": 1, "longitude": 2})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.latitude, 1)
        self.assertEqual(self.offering.longitude, 2)

    @data("staff", "owner")
    def test_update_description(self, user):
        url = factories.OfferingFactory.get_url(self.offering, "update_description")
        self.client.force_authenticate(getattr(self.fixture, user))
        new_category = factories.CategoryFactory()
        response = self.client.post(
            url, {"category": factories.CategoryFactory.get_url(new_category)}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.category, new_category)

    @data("staff", "owner")
    def test_update_options(self, user):
        url = factories.OfferingFactory.get_url(self.offering, "update_options")
        self.client.force_authenticate(getattr(self.fixture, user))
        options = {
            "order": ["email"],
            "options": {
                "email": {
                    "type": "string",
                    "label": "email",
                    "default": "user@example.com",
                    "required": False,
                }
            },
        }
        response = self.client.post(url, {"options": options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.options, options)

    @data("staff", "owner")
    def test_update_secret_options(self, user):
        url = factories.OfferingFactory.get_url(self.offering, "update_integration")
        self.client.force_authenticate(getattr(self.fixture, user))
        secret_options = {
            "environ": [{"name": "DJANGO_SETTINGS", "value": "settings.py"}],
            "language": "python",
        }
        response = self.client.post(url, {"secret_options": secret_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.secret_options, secret_options)


@ddt
class OfferingOrganizationGroupsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer

        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            project=self.fixture.project, customer=self.customer, shared=True
        )
        self.url = factories.OfferingFactory.get_url(
            self.offering, action="update_organization_groups"
        )
        self.delete_url = factories.OfferingFactory.get_url(
            self.offering, action="delete_organization_groups"
        )
        self.organization_group = structure_factories.OrganizationGroupFactory()
        self.organization_group_url = (
            structure_factories.OrganizationGroupFactory.get_url(
                self.organization_group
            )
        )

    @data("staff", "owner")
    def test_user_can_update_organization_groups(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, {"organization_groups": [self.organization_group_url]}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.organization_groups.count(), 1)

    @data("customer_support", "admin", "manager")
    def test_user_cannot_update_organization_groups(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, {"organization_groups": [self.organization_group_url]}
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data("staff", "owner")
    def test_user_can_delete_organization_groups(self, user):
        self.offering.organization_groups.add(self.organization_group)
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.organization_groups.count(), 0)

    @data("customer_support", "admin", "manager")
    def test_user_cannot_delete_organization_groups(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.delete_url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class OfferingDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer,
            project=self.fixture.project,
            shared=True,
            state=OfferingStates.DRAFT,
        )
        factories.PlanFactory(offering=self.offering)
        CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_OFFERING)

    @data("staff", "owner")
    def test_authorized_user_can_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Offering.objects.filter(customer=self.customer).exists()
        )

    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_delete_offering(self, user):
        response = self.delete_offering(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    def test_offering_deleting_is_not_available_for_blocked_organization(self):
        self.customer.blocked = True
        self.customer.save()
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_owner_can_not_delete_offering_if_it_is_not_in_draft_state(self):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data["detail"],
            "Offering was not deleted since offering is not in draft state.",
        )

    def test_customer_owner_can_not_delete_offering_if_it_has_resources(self):
        self.offering.state = OfferingStates.DRAFT
        factories.ResourceFactory(offering=self.offering)
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data["detail"], "Offering was not deleted since it has resources."
        )

    def delete_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.delete(url)
        return response

    def test_when_offering_is_deleted_related_service_setting_is_deleted(self):
        # Arrange
        self.service_settings = structure_factories.ServiceSettingsFactory(
            customer=self.customer
        )
        self.offering.scope = self.service_settings
        self.offering.save()

        # Act
        self.delete_offering("owner")

        # Assert
        self.assertRaises(ObjectDoesNotExist, self.service_settings.refresh_from_db)


@ddt
class OfferingAttributesTest(test.APITransactionTestCase):
    def setUp(self):
        self.serializer = serializers.OfferingCreateSerializer()
        self.category = factories.CategoryFactory()
        self.section = factories.SectionFactory(category=self.category)
        self.attribute = factories.AttributeFactory(
            section=self.section, key="userSupportOptions", type="list"
        )
        (
            models.AttributeOption.objects.create(
                attribute=self.attribute, key="web_chat", title="Web chat"
            ),
        )
        models.AttributeOption.objects.create(
            attribute=self.attribute, key="phone", title="Telephone"
        )

    @data(["web_chat", "phone"])
    def test_list_attribute_is_valid(self, value):
        self._valid("list", value)

    @data(["chat", "phone"], "web_chat", 1, False)
    def test_list_attribute_is_not_valid(self, value):
        self._not_valid("list", value)

    @data("web_chat")
    def test_choice_attribute_is_valid(self, value):
        self._valid("choice", value)

    @data(["web_chat"], "chat", 1, False)
    def test_choice_attribute_is_not_valid(self, value):
        self._not_valid("choice", value)

    @data("name")
    def test_string_attribute_is_valid(self, value):
        self._valid("string", value)

    @data(["web_chat"], 1, False)
    def test_string_attribute_is_not_valid(self, value):
        self._not_valid("string", value)

    def test_integer_attribute_is_valid(self):
        self._valid("integer", 1)

    @data(["web_chat"], "web_chat", -1)
    def test_integer_attribute_is_not_valid(self, value):
        self._not_valid("integer", value)

    def test_boolean_attribute_is_valid(self):
        self._valid("boolean", True)

    @data(["web_chat"], "web_chat", 1)
    def test_boolean_attribute_is_not_valid(self, value):
        self._not_valid("boolean", value)

    def _valid(self, attribute_type, value):
        self.attribute.type = attribute_type
        self.attribute.save()
        attributes = {
            "attributes": {
                "userSupportOptions": value,
            },
            "category": self.category,
        }
        self.assertIsNone(self.serializer._validate_attributes(attributes))

    def _not_valid(self, attribute_type, value):
        self.attribute.type = attribute_type
        self.attribute.save()
        attributes = {
            "attributes": {
                "userSupportOptions": value,
            },
            "category": self.category,
        }
        self.assertRaises(
            rest_exceptions.ValidationError,
            self.serializer._validate_attributes,
            attributes,
        )


class OfferingQuotaTest(test.APITransactionTestCase):
    def get_usage(self, category):
        return category.get_quota_usage("offering_count")

    def test_empty_category(self):
        self.assertEqual(0, self.get_usage(factories.CategoryFactory()))

    def test_active_offerings_are_counted(self):
        category = factories.CategoryFactory()
        provider = factories.ServiceProviderFactory()
        factories.OfferingFactory.create_batch(
            3,
            category=category,
            customer=provider.customer,
            state=OfferingStates.ACTIVE,
        )
        self.assertEqual(3, self.get_usage(category))

    def test_draft_offerings_are_not_counted(self):
        category = factories.CategoryFactory()
        provider = factories.ServiceProviderFactory()
        factories.OfferingFactory.create_batch(
            2,
            category=category,
            customer=provider.customer,
            state=OfferingStates.DRAFT,
        )
        self.assertEqual(0, self.get_usage(category))


@ddt
class OfferingStateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer,
            project=self.fixture.project,
            shared=True,
            state=OfferingStates.DRAFT,
        )
        self.plan = factories.PlanFactory(offering=self.offering)

        user = UserFactory()
        self.offering.add_user(user, OfferingRole.MANAGER)
        service_provider.add_user(user, ServiceProviderRole.MANAGER)
        self.fixture.service_manager = user

        CustomerRole.OWNER.add_permission(PermissionEnum.PAUSE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.PAUSE_OFFERING)

        CustomerRole.OWNER.add_permission(PermissionEnum.UNPAUSE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UNPAUSE_OFFERING)

    @data(
        "staff",
    )
    def test_authorized_user_can_activate_offering(self, user):
        response, offering = self.update_offering_state(user, "activate")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    def test_validate_offering_has_plans(self):
        self.offering.plans.all().delete()
        response, offering = self.update_offering_state("staff", "activate")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertTrue("Offering does not have any billing plans." in response.data)

    @data("owner", "user", "customer_support", "admin", "manager", "service_manager")
    def test_unauthorized_user_can_not_activate_offering(self, user):
        response, offering = self.update_offering_state(user, "activate")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.DRAFT)

    @data("owner", "service_manager")
    def test_authorized_user_can_pause_offering(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        response, offering = self.update_offering_state(user, "pause")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.PAUSED)

    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_pause_offering(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        response, offering = self.update_offering_state(user, "pause")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @data("owner", "service_manager")
    def test_authorized_user_can_unpause_offering(self, user):
        self.offering.state = OfferingStates.PAUSED
        self.offering.save()

        response, offering = self.update_offering_state(user, "unpause")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_unpause_offering(self, user):
        self.offering.state = OfferingStates.PAUSED
        self.offering.save()

        response, offering = self.update_offering_state(user, "unpause")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.PAUSED)

    def test_invalid_state(self):
        response, offering = self.update_offering_state("staff", "pause")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )
        self.assertEqual(offering.state, OfferingStates.DRAFT)

    @data("activate", "pause", "archive")
    def test_offering_state_changing_is_not_available_for_blocked_organization(
        self, state
    ):
        self.customer.blocked = True
        self.customer.save()
        response, _ = self.update_offering_state("staff", state)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_provide_paused_reason(self):
        # Arrange
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        # Act
        response, offering = self.update_offering_state(
            "staff", "pause", {"paused_reason": "Not available anymore."}
        )

        # Assert
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.paused_reason, "Not available anymore.")

    def test_authorized_user_can_not_activate_offering_without_plans(self):
        self.plan.delete()
        response, _ = self.update_offering_state("staff", "activate")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    @data("owner", "service_manager")
    def test_authorized_user_can_not_unpause_offering_without_plans(self, user):
        self.plan.delete()
        self.offering.state = OfferingStates.PAUSED
        self.offering.save()

        response, offering = self.update_offering_state(user, "unpause")
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def update_offering_state(self, user, state, payload=None):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering, state)
        response = self.client.post(url, payload)
        self.offering.refresh_from_db()

        return response, self.offering


@ddt
class OfferingPublicGetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offerings = [
            factories.OfferingFactory(state=OfferingStates.ACTIVE),
            factories.OfferingFactory(state=OfferingStates.DRAFT),
            factories.OfferingFactory(state=OfferingStates.PAUSED, shared=False),
            factories.OfferingFactory(state=OfferingStates.ACTIVE, shared=False),
        ]
        factories.PlanFactory(offering=self.offerings[-1])

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_anonymous_cannot_view_offerings(self):
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_anonymous_cannot_view_draft_offerings(self):
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertNotEqual(OfferingStates.DRAFT, offering["state"])

    def test_anonymous_can_view_offering_scope(self):
        url = factories.OfferingFactory.get_public_url(self.offerings[0])
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("staff", "owner", "user", "customer_support", "admin")
    def test_authenticated_user_can_view_offering_scope(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        for offering in response.data:
            self.assertIn("scope", offering)

    @data("owner", "user", "customer_support", "admin", "manager", None)
    def test_private_offerings_are_hidden_and_shared_offering_visible(self, user):
        if user:
            user = getattr(self.fixture, user)
            self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        shared_exists = None
        private_exists = None

        for offering in response.data:
            if offering["shared"]:
                shared_exists = True
            else:
                private_exists = True

        self.assertTrue(shared_exists)
        self.assertFalse(private_exists)

    @data("staff", "global_support")
    def test_private_offerings_and_shared_offering_are_visible(self, user):
        if user:
            user = getattr(self.fixture, user)
            self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        shared_exists = None
        private_exists = None

        for offering in response.data:
            if offering["shared"]:
                shared_exists = True
            else:
                private_exists = True

        self.assertTrue(shared_exists)
        self.assertTrue(private_exists)

    @data("owner", "customer_support", "admin", "manager")
    def test_private_offerings_are_visible_for_related_user(self, user):
        private_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            shared=False,
            customer=self.fixture.customer,
            project=self.fixture.project,
        )
        factories.PlanFactory(offering=private_offering)

        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)

        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)
        self.assertEqual(len(response.data), 2)

        shared_exists = None
        private_exists = None

        for offering in response.data:
            if offering["shared"]:
                shared_exists = True
            else:
                private_exists = True

        self.assertTrue(shared_exists)
        self.assertTrue(private_exists)

    def test_anonymous_can_get_offerings(self):
        offering_list_url = factories.OfferingFactory.get_public_list_url()
        result = self.client.get(offering_list_url)
        self.assertEqual(result.status_code, status.HTTP_200_OK)
        self.assertEqual(len(result.data), 1)


class OfferingExportImportTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.temp_dir = tempfile.gettempdir()

    def test_export_offering(self):
        offering = factories.OfferingFactory(
            description="Описание с non-ASCII символами."
        )
        export_offering(offering, self.temp_dir)
        json_path = os.path.join(self.temp_dir, offering.uuid.hex + ".json")
        self.assertTrue(os.path.exists(json_path))

    def test_export_offering_with_thumbnail(self):
        GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"

        with open(os.path.join(self.temp_dir, "pic.gif"), "wb") as pic:
            pic.write(base64.b64decode(GIF))

        offering = factories.OfferingFactory(thumbnail=pic.name)
        export_offering(offering, self.temp_dir)
        _, file_extension = os.path.splitext(offering.thumbnail.file.name)
        pic_path = os.path.join(self.temp_dir, offering.uuid.hex + file_extension)
        self.assertTrue(os.path.exists(pic_path))

    def test_import_offering(self):
        export_data = self._get_data()
        create_offering(export_data, self.fixture.customer)

        self.assertTrue(
            models.Offering.objects.filter(
                customer=self.fixture.customer, name="offering_name"
            ).exists()
        )
        offering = models.Offering.objects.filter(
            customer=self.fixture.customer, name="offering_name"
        ).get()
        self.assertTrue(offering.thumbnail)
        self.assertEqual(offering.plans.count(), 1)
        self.assertEqual(offering.plans.first().name, "Start")
        self.assertEqual(offering.plans.first().components.count(), 1)
        self.assertEqual(offering.components.count(), 1)
        self.assertEqual(offering.components.first().type, "node")

    def test_update_offering(self):
        export_data = self._get_data()
        offering = create_offering(export_data, self.fixture.customer)
        export_data["name"] = "new_offering_name"
        export_data["plans"][0]["name"] = "new_plan"
        export_data["components"][0]["type"] = "new_type"
        export_data["plans"][0]["components"][0]["component"]["type"] = "new_type"

        update_offering(offering, export_data)
        offering.refresh_from_db()
        self.assertEqual(offering.name, "new_offering_name")
        self.assertEqual(offering.plans.first().name, "new_plan")
        self.assertEqual(offering.components.first().type, "new_type")

    def _get_data(self):
        path = os.path.abspath(os.path.dirname(__file__))
        data = load_json_resource("offering.json", __name__)
        category = factories.CategoryFactory()
        data["category_id"] = category.id

        thumbnail = data.get("thumbnail")
        if thumbnail:
            data["thumbnail"] = os.path.join(os.path.dirname(path), thumbnail)

        return data


class OfferingDoiTest(test.APITransactionTestCase):
    def setUp(self):
        self.dc_resp = load_json_resource("datacite-resp.json", __name__)["data"]
        self.ref_pids = [
            x["relatedIdentifier"]
            for x in self.dc_resp["attributes"]["relatedIdentifiers"]
        ]
        self.offering = factories.OfferingFactory(
            datacite_doi="10.15159/t9zh-k971",
            citation_count=self.dc_resp["attributes"]["citationCount"],
        )
        self.offering_referral = factories.OfferingReferralFactory(scope=self.offering)
        self.offering2 = factories.OfferingFactory(
            datacite_doi="10.15159/t9zh-k972",
            citation_count=0,
        )
        self.offering_referral2 = factories.OfferingReferralFactory(
            scope=self.offering2
        )
        self.fixture = fixtures.ProjectFixture()

    def test_viewing_datacite_related_fields(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url).json()

        self.assertEqual(response["datacite_doi"], self.dc_resp["id"])
        self.assertEqual(
            response["citation_count"], self.dc_resp["attributes"]["citationCount"]
        )

    def test_authenticated_user_can_lookup_offering_referrals(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingReferralFactory.get_list_url()

        response = self.client.get(
            url, {"scope": factories.OfferingFactory.get_url(self.offering)}
        ).json()

        self.assertTrue("pid" in response[0])
        self.assertTrue(len(response) == 1)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_anonymous_user_cannot_lookup_offering_referrals(self):
        url = factories.OfferingReferralFactory.get_list_url()

        response = self.client.get(
            url, {"scope": factories.OfferingFactory.get_url(self.offering)}
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


@ddt
class OfferingThumbnailTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_THUMBNAIL)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_THUMBNAIL
        )

    @data("staff")
    def test_staff_can_update_or_delete_thumbnail_of_archived_offering(self, user):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()
        self._test_positive(user)

    @data("offering_owner", "service_manager", "offering_admin", "offering_manager")
    def test_user_cannot_update_or_delete_thumbnail_of_archived_offering(self, user):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()
        self._test_negative(user)

    @data("staff", "offering_owner", "service_manager")
    def test_user_can_update_or_delete_thumbnail(self, user):
        self._test_positive(user)

    @data("offering_admin", "offering_manager")
    def test_user_cannot_update_or_delete_thumbnail(self, user):
        self._test_negative(user)

    def update_thumbnail(self):
        url = factories.OfferingFactory.get_url(
            offering=self.offering, action="update_thumbnail"
        )
        return self.client.post(url, {"thumbnail": dummy_image()}, format="multipart")

    def delete_thumbnail(self):
        url_delete = factories.OfferingFactory.get_url(
            offering=self.offering, action="delete_thumbnail"
        )
        return self.client.post(url_delete)

    def _test_positive(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.update_thumbnail()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.thumbnail)

        response = self.delete_thumbnail()
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.offering.refresh_from_db()
        self.assertFalse(self.offering.thumbnail)

    def _test_negative(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.update_thumbnail()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering.refresh_from_db()
        self.assertFalse(self.offering.thumbnail)

        response = self.delete_thumbnail()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class OfferingCreateComponentsTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.OfferingFactory.get_url(
            self.offering, "create_offering_component"
        )
        resource = self.fixture.resource
        resource.delete()

        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS
        )

    @data("offering_owner", "service_manager")
    def test_offering_components_create_succeed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "type": "created_type",
            "name": "created_name",
            "measured_unit": "cpu_k_hours",
        }

        self.assertEqual(1, models.OfferingComponent.objects.count())
        response = self.client.post(self.url, payload)
        self.assertEqual(201, response.status_code)
        self.assertEqual(2, models.OfferingComponent.objects.count())

        offering_component = models.OfferingComponent.objects.latest("id")
        self.assertEqual("created_name", offering_component.name)
        self.assertEqual("created_type", offering_component.type)
        self.assertEqual(
            models.OfferingComponent.BillingTypes.USAGE, offering_component.billing_type
        )

    @data("offering_owner", "service_manager")
    def test_offering_components_create_with_invalid_payload_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "name": "updated_name",
            "measured_unit": "cpu_k_hours",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(400, response.status_code)

    @data("offering_owner", "service_manager")
    def test_offering_components_create_to_builtin_type_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        self.offering.type = VIRTUAL_MACHINE_TYPE
        self.offering.save()

        payload = {
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "type": "cpu",
            "name": "cpu",
            "measured_unit": "cpu_k_hours",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(400, response.status_code)

    @data("offering_manager", "offering_admin")
    def test_offering_components_create_with_wrong_roles_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, [])

        self.assertEqual(403, response.status_code)


@ddt
class OfferingUpdateComponentsTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering_component = self.fixture.offering_component
        self.url = factories.OfferingFactory.get_url(
            self.offering, "update_offering_component"
        )
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
        )
        resource = self.fixture.resource
        resource.delete()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS
        )

    @data("offering_owner", "service_manager")
    def test_offering_components_update_succeed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "uuid": self.offering_component.uuid,
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "type": "updated_type",
            "name": "updated_name",
            "measured_unit": "cpu_k_hours",
        }

        self.assertEqual("CPU", self.offering_component.name)
        self.assertEqual("cpu", self.offering_component.type)
        self.assertEqual(
            models.OfferingComponent.BillingTypes.FIXED,
            self.offering_component.billing_type,
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(200, response.status_code)
        offering_component = models.OfferingComponent.objects.get(
            offering=self.offering, uuid=self.offering_component.uuid
        )

        self.assertEqual("updated_name", offering_component.name)
        self.assertEqual("updated_type", offering_component.type)
        self.assertEqual(
            models.OfferingComponent.BillingTypes.USAGE, offering_component.billing_type
        )

    @data("offering_owner", "service_manager")
    def test_offering_components_update_with_invalid_payload_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "type": "updated_type",
            "name": "updated_name",
            "measured_unit": "cpu_k_hours",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(400, response.status_code)
        payload["uuid"] = self.offering_component.uuid
        payload["billing_type"] = "random_billing_type"

        response = self.client.post(self.url, payload)
        self.assertEqual(400, response.status_code)

    @data("offering_owner", "service_manager")
    def test_offering_components_update_to_builtin_type_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        self.offering.type = VIRTUAL_MACHINE_TYPE
        self.offering.save()

        payload = {
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "type": "gpu",
            "name": "GPU",
            "measured_unit": "cpu_k_hours",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("offering_manager", "offering_admin")
    def test_offering_components_update_with_wrong_roles_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, [])

        self.assertEqual(403, response.status_code)


@ddt
class OfferingRemoveComponentsTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering_component = self.fixture.offering_component
        self.url = factories.OfferingFactory.get_url(
            self.offering, "remove_offering_component"
        )
        factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
        )
        resource = self.fixture.resource
        resource.delete()

        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_COMPONENTS)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_COMPONENTS
        )

    @data("offering_owner", "service_manager")
    def test_offering_components_remove_succeed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "uuid": self.offering_component.uuid,
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "type": "updated_type",
            "name": "updated_name",
            "measured_unit": "cpu_k_hours",
        }

        self.assertEqual(2, models.OfferingComponent.objects.all().count())
        response = self.client.post(self.url, payload)
        self.assertEqual(200, response.status_code)
        self.assertEqual(1, models.OfferingComponent.objects.all().count())

    @data("offering_owner", "service_manager")
    def test_offering_components_remove_without_uuid_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "billing_type": models.OfferingComponent.BillingTypes.USAGE,
            "type": "updated_type",
            "name": "updated_name",
            "measured_unit": "cpu_k_hours",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(400, response.status_code)

    @data("offering_manager", "offering_admin")
    def test_offering_components_remove_with_wrong_roles_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        response = self.client.post(self.url, [])

        self.assertEqual(403, response.status_code)


@ddt
class OfferingBackendMetadataTest(test.APITransactionTestCase):
    def setUp(self) -> None:
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.UPDATE_OFFERING)

    @data("offering_owner", "service_manager")
    def test_offering_backend_metadata_setting_is_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        url = factories.OfferingFactory.get_url(self.offering, "set_backend_metadata")
        backend_metadata = {"field1": "value1", "field2": {"field3": "value3"}}
        payload = {"backend_metadata": backend_metadata}
        response = self.client.post(url, payload)
        self.assertEqual(200, response.status_code)
        self.offering.refresh_from_db()
        self.assertEqual(backend_metadata, self.offering.backend_metadata)

    @data("offering_manager", "offering_admin")
    def test_offering_backend_metadata_setting_is_not_allowed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        url = factories.OfferingFactory.get_url(self.offering, "set_backend_metadata")
        payload = {"backend_metadata": {}}
        response = self.client.post(url, payload)
        self.assertEqual(403, response.status_code)


@ddt
class ListCustomerProjectsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()

    @data("staff", "offering_owner")
    def test_user_can_get_list_customer_projects(self, user):
        response = self.get_list_customer_projects(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

    @data("owner", "admin", "user")
    def test_user_can_not_get_list_customer_projects(self, user):
        response = self.get_list_customer_projects(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

    def get_list_customer_projects(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(
            self.fixture.offering, "list_customer_projects"
        )
        return self.client.get(url)


@ddt
class ListCustomerUsersTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()
        self.fixture.admin

    @data("staff", "offering_owner")
    def test_user_can_get_list_customer_users(self, user):
        response = self.get_list_customer_users(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)

    @data("owner", "admin", "user")
    def test_user_can_not_get_list_customer_users(self, user):
        response = self.get_list_customer_users(user)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND, response.data)

    def get_list_customer_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(
            self.fixture.offering, "list_customer_users"
        )
        return self.client.get(url)


class ResourceOfferingsViewSetTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.category = self.fixture.offering.category
        self.resource = self.fixture.resource

    def test_filter_offerings_by_category(self):
        url = f"/api/marketplace-resource-offerings/{self.category.uuid.hex}/"
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.offering.uuid.hex)


@ddt
class RefreshOfferingUsernamesTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.customer = self.fixture.offering_customer
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_INTEGRATION)
        ServiceProviderRole.MANAGER.add_permission(
            PermissionEnum.UPDATE_OFFERING_INTEGRATION
        )
        self.offering = self.fixture.offering
        self.offering.plugin_options = {
            "username_generation_policy": utils.UsernameGenerationPolicy.WALDUR_USERNAME.value
        }
        self.offering.save()
        self.offering_user = factories.OfferingUserFactory(
            offering=self.offering, username="old_username"
        )
        self.url = factories.OfferingFactory.get_url(
            self.offering, "refresh_offering_usernames"
        )

    @data("staff", "service_owner", "service_manager")
    def test_refresh_offering_usernames(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        new_username = "new_username"
        with mock.patch(
            "waldur_mastermind.marketplace.utils.generate_username",
            return_value=new_username,
        ):
            response = self.client.post(self.url)
            self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, new_username)

    @data("admin", "owner", "manager")
    def test_refresh_offering_usernames_forbidden(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

        self.offering_user.refresh_from_db()
        self.assertEqual(self.offering_user.username, "old_username")

    def test_refresh_offering_usernames_does_not_update_if_policy_is_service_provider(
        self,
    ):
        self.client.force_authenticate(self.fixture.service_owner)
        offering = factories.OfferingFactory(
            plugin_options={
                "username_generation_policy": utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
            },
            customer=self.customer,
        )
        offering_user = factories.OfferingUserFactory(
            offering=offering, username="old_username"
        )
        url = factories.OfferingFactory.get_url(offering, "refresh_offering_usernames")

        with mock.patch(
            "waldur_mastermind.marketplace.utils.generate_username",
            return_value="new_username",
        ):
            response = self.client.post(url)
            self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.username, "old_username")

    def test_refresh_offering_usernames_validation_for_offering_state(self):
        self.client.force_authenticate(self.fixture.service_owner)
        offering = factories.OfferingFactory(
            plugin_options={
                "username_generation_policy": utils.UsernameGenerationPolicy.SERVICE_PROVIDER.value
            },
            customer=self.customer,
        )
        url = factories.OfferingFactory.get_url(offering, "refresh_offering_usernames")

        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class OrderNotificationTest(test.APITransactionTestCase):
    def setUp(self):
        self.order = factories.OrderFactory(state=OrderStates.PENDING_PROVIDER)

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.notify_user_that_order_been_rejected.delay"
    )
    def test_notify_user_when_order_rejected(self, mock_notify):
        self.order.state = OrderStates.REJECTED
        self.order.save()
        mock_notify.assert_called_once_with(self.order.uuid.hex)


class ProviderOfferingOrdersTest(test.APITransactionTestCase):
    """
    This test is to check that the marketplace offering provider orders endpoint is working as expected
    """

    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.order = factories.OrderFactory(offering=self.offering)
        self.url = factories.OfferingFactory.get_url(self.offering, "orders")
        self.detail_url = self.url + str(self.order.uuid.hex) + "/"

    def test_staff_can_get_orders(self):
        """
        This test is to check that the staff can get the orders
        """
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected {status.HTTP_200_OK}",
        )
        # There are two orders, one from the fixture and one from the setup
        self.assertEqual(
            len(response.json()),
            2,
            f"Expected 2 orders, but got {len(response.json())}",
        )

    def test_offering_manager_can_get_orders(self):
        """
        This test is to check that the offering manager can get the orders
        """
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(self.url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected {status.HTTP_200_OK}",
        )
        self.assertEqual(
            len(response.json()),
            2,
            f"Expected 2 orders, but got {len(response.json())}",
        )

    def test_service_provider_can_get_orders(self):
        """
        This test is to check that the service provider can get the orders
        """
        self.client.force_authenticate(self.fixture.service_owner)
        response = self.client.get(self.url)
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Expected 200 OK, but got {response.status_code}",
        )
        self.assertEqual(
            len(response.json()),
            2,
            f"Expected 2 orders, but got {len(response.json())}",
        )

    def test_customer_support_cannot_get_orders(self):
        """
        This test is to check that the customer support role cannot get the orders
        """
        self.client.force_authenticate(self.fixture.customer_support)
        response = self.client.get(self.url)
        # Assert that the response is a 403 error
        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"Response status code is {response.status_code}, expected {status.HTTP_403_FORBIDDEN}",
        )

    def test_orders_are_filtered_by_state(self):
        """
        This test is to check that the orders are filtered by state
        There are two orders, one in state done and one in state pending provider
        """
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(self.url, {"state": "done"})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected {status.HTTP_200_OK}",
        )
        self.assertEqual(
            len(response.json()), 1, f"Expected 1 order, but got {len(response.json())}"
        )
        response = self.client.get(self.url, {"state": "executing"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            len(response.json()),
            0,
            f"Expected 0 orders, but got {len(response.json())}",
        )

    def test_orders_are_filtered_by_type(self):
        """
        This test is to check that the orders are filtered by type
        There are two orders, one in state done and one in state pending provider
        """
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url, {"type": "Create"})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected 200 OK",
        )
        self.assertEqual(
            len(response.json()),
            2,
            f"Expected 2 orders, but got {len(response.json())}",
        )

        response = self.client.get(self.url, {"type": "Update"})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected 200 OK",
        )
        self.assertEqual(
            len(response.json()),
            0,
            f"Expected 0 orders of type Update, but got {len(response.json())}",
        )

    def test_orders_are_filtered_by_resource_uuid(self):
        """
        This test is to check that the orders are filtered by resource uuid
        """
        self.client.force_authenticate(self.fixture.offering_manager)
        response = self.client.get(
            self.url, {"resource_uuid": self.order.resource.uuid.hex}
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected 200 OK",
        )
        self.assertEqual(
            len(response.json()), 1, f"Expected 1 order, but got {len(response.json())}"
        )
        response = self.client.get(self.url, {"resource_uuid": uuid.uuid4().hex})
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected 200 OK",
        )
        self.assertEqual(
            len(response.json()),
            0,
            f"Expected 0 orders, but got {len(response.json())}",
        )

    def test_order_detail(self):
        """
        This test is to check that the order detail view returns the correct order
        """
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.detail_url)
        # Assert that the response is OK and the values match
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            f"Response status code is {response.status_code}, expected {status.HTTP_200_OK}",
        )
        self.assertEqual(
            response.json()["uuid"],
            self.order.uuid.hex,
            f"The order with uuid {self.order.uuid.hex} should be found, but the response is {response.json()}",
        )
        self.assertEqual(
            response.json()["offering_name"],
            self.order.offering.name,
            f"The order with offering name {self.order.offering.name} should be found, but the response is {response.json()}",
        )

    def test_order_detail_not_found(self):
        """
        This test is to check that the order detail view returns a 404 error
        when the order does not exist.
        """
        self.client.force_authenticate(self.fixture.staff)
        url = self.url + uuid.uuid4().hex + "/"
        response = self.client.get(url)
        # Assert that the response is a 404 error
        self.assertEqual(
            response.status_code,
            status.HTTP_404_NOT_FOUND,
            f"No order with uuid {uuid.uuid4().hex} should be found, but the response is {response.status_code}",
        )

    def test_unauthenticated_user_cannot_get_orders(self):
        """
        This test is to check that the unauthenticated user cannot get the orders
        """
        self.client.force_authenticate(None)
        response = self.client.get(self.url)
        # Assert that the response is a 401 error
        self.assertEqual(
            response.status_code,
            status.HTTP_401_UNAUTHORIZED,
            f"Response status code is {response.status_code}, expected 401 Unauthorized",
        )
