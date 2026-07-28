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
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection as db_connection
from django.test.utils import CaptureQueriesContext
from rest_framework import exceptions as rest_exceptions
from rest_framework import status, test

from waldur_core.checklist import enums as checklist_enums
from waldur_core.core import utils as core_utils
from waldur_core.core.pagination import RESULT_COUNT_HEADER
from waldur_core.core.tests.helpers import load_json_resource
from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.media.models import File
from waldur_core.media.utils import dummy_image, dummy_svg
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
    BASIC_OFFERING,
    OPENSTACK_TENANT_OFFERING,
    SITE_AGENT_OFFERING,
    SUPPORT_OFFERING,
    VMWARE_VM_OFFERING,
    BillingTypes,
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

from . import fixtures as marketplace_fixtures


@ddt
class OfferingGetTest(test.APITestCase):
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


class OfferingExtraFieldsTest(test.APITestCase):
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


class OfferingPlanInfoTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(shared=True)
        self.url = factories.OfferingFactory.get_url(self.offering)

        self.offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.FIXED,
        )
        self.plan = factories.PlanFactory(offering=self.offering)
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=self.offering_component
        )

    def test_plan_info(self):
        self.client.force_authenticate(self.fixture.staff)
        self._check_plan_info(BillingTypes.FIXED, "fixed")
        self._check_plan_info(BillingTypes.USAGE, "usage-based")
        self._check_plan_info(BillingTypes.ONE_TIME, "one-time")
        self._check_plan_info(BillingTypes.ON_PLAN_SWITCH, "on-plan-switch")
        self._check_plan_info(BillingTypes.LIMIT, "limit")

        offering_component = factories.OfferingComponentFactory(
            offering=self.offering,
            billing_type=BillingTypes.FIXED,
            type="ram",
            name="RAM",
        )
        self.plan_component = factories.PlanComponentFactory(
            plan=self.plan, component=offering_component
        )

        self._check_plan_info(BillingTypes.ON_PLAN_SWITCH, "mixed")

    def test_minimal_price(self):
        self.client.force_authenticate(self.fixture.staff)

        self.offering_component.billing_type = BillingTypes.LIMIT
        self.plan_component.price = 10
        self._check_minimal_price(10)

        self.offering_component.billing_type = BillingTypes.FIXED
        self.plan_component.price = 100
        self.plan_component.amount = 0
        self._check_minimal_price(100)

        self.offering_component.billing_type = BillingTypes.FIXED
        self.plan_component.price = 100
        self.plan_component.amount = 1
        self._check_minimal_price(100)

        self.offering_component.billing_type = BillingTypes.ONE_TIME
        self.plan_component.price = 200
        self._check_minimal_price(200)

        self.offering_component.billing_type = BillingTypes.ON_PLAN_SWITCH
        self.plan_component.price = 300
        self._check_minimal_price(0)

        self.offering_component.billing_type = BillingTypes.USAGE
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
class SecretOptionsTests(test.APITestCase):
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


class OfferingFilterTest(test.APITestCase):
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


class OfferingPlansFilterTest(test.APITestCase):
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
class OfferingCreateTest(test.APITestCase):
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
        self.assertEqual(component.billing_type, BillingTypes.FIXED)

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
        user = self.fixture.staff
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
            "type": SUPPORT_OFFERING,
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
            "type": SUPPORT_OFFERING,
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

    def test_update_offering_plugin_options_with_heappe_new_fields(self):
        """Test that offering plugin options can be updated with new Heappe fields"""
        offering = factories.OfferingFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(offering, "update_integration")
        plugin_options = {
            "heappe_url": "https://heappe.example.com",
            "heappe_username": "test_user",
            "heappe_cluster_id": "1",
            "heappe_local_base_path": "~/",
            "scratch_project_directory": "/scratch/projects",
            "project_permanent_directory": "/permanent/projects",
        }

        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["scratch_project_directory"], "/scratch/projects"
        )
        self.assertEqual(
            offering.plugin_options["project_permanent_directory"],
            "/permanent/projects",
        )

        self.assertEqual(
            offering.plugin_options["heappe_url"], "https://heappe.example.com"
        )
        self.assertEqual(offering.plugin_options["heappe_username"], "test_user")

    def test_update_offering_plugin_options_with_openstack_max_security_groups(self):
        """Test that offering plugin options can be updated with max_security_groups"""
        offering = factories.OfferingFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(offering, "update_integration")
        plugin_options = {
            "max_instances": 10,
            "max_volumes": 20,
            "max_security_groups": 15,
        }

        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering.refresh_from_db()
        self.assertEqual(offering.plugin_options["max_instances"], 10)
        self.assertEqual(offering.plugin_options["max_volumes"], 20)
        self.assertEqual(offering.plugin_options["max_security_groups"], 15)

    def test_update_offering_plugin_options_with_date_field(self):
        """Test that plugin_options with date values can be saved without JSON serialization errors"""
        offering = factories.OfferingFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(offering, "update_integration")
        plugin_options = {
            "latest_date_for_resource_termination": "2026-02-28",
        }

        response = self.client.post(url, {"plugin_options": plugin_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["latest_date_for_resource_termination"],
            "2026-02-28",
        )

    def test_update_offering_plugin_options_required_team_role_allows_blank(self):
        """Clearing required_team_role_for_provisioning must accept empty string."""
        offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={"required_team_role_for_provisioning": "PROJECT.MANAGER"},
        )
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(offering, "update_integration")
        response = self.client.post(
            url,
            {"plugin_options": {"required_team_role_for_provisioning": ""}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["required_team_role_for_provisioning"], ""
        )

    def test_update_offering_plugin_options_required_team_role_allows_null(self):
        """Clearing required_team_role_for_provisioning must accept null."""
        offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={"required_team_role_for_provisioning": "PROJECT.MANAGER"},
        )
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(offering, "update_integration")
        response = self.client.post(
            url,
            {"plugin_options": {"required_team_role_for_provisioning": None}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering.refresh_from_db()
        self.assertIsNone(
            offering.plugin_options["required_team_role_for_provisioning"]
        )

    def test_update_offering_plugin_options_resource_name_pattern_allows_blank(self):
        """Clearing resource_name_pattern must accept empty string."""
        offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={"resource_name_pattern": "{project_slug}-{counter}"},
        )
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(offering, "update_integration")
        response = self.client.post(
            url,
            {"plugin_options": {"resource_name_pattern": ""}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering.refresh_from_db()
        self.assertEqual(offering.plugin_options["resource_name_pattern"], "")

    def test_update_offering_plugin_options_resource_name_pattern_allows_null(self):
        """Clearing resource_name_pattern must accept null."""
        offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={"resource_name_pattern": "{project_slug}-{counter}"},
        )
        self.client.force_authenticate(self.fixture.staff)

        url = factories.OfferingFactory.get_url(offering, "update_integration")
        response = self.client.post(
            url,
            {"plugin_options": {"resource_name_pattern": None}},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering.refresh_from_db()
        self.assertIsNone(offering.plugin_options["resource_name_pattern"])

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

    def test_create_offering_with_plugin_options(self):
        plugin_options = {
            "auto_approve_in_service_provider_projects": True,
            "max_instances": 10,
        }
        response = self.create_offering(
            "owner", add_payload={"plugin_options": plugin_options}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.plugin_options["auto_approve_in_service_provider_projects"], True
        )
        self.assertEqual(offering.plugin_options["max_instances"], 10)

    def test_create_offering_with_date_in_plugin_options(self):
        plugin_options = {
            "latest_date_for_resource_termination": "2026-02-28",
        }
        response = self.create_offering(
            "owner", add_payload={"plugin_options": plugin_options}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.plugin_options["latest_date_for_resource_termination"],
            "2026-02-28",
        )

    def test_create_offering_with_invalid_date_in_plugin_options(self):
        plugin_options = {
            "latest_date_for_resource_termination": "not-a-date",
        }
        response = self.create_offering(
            "owner", add_payload={"plugin_options": plugin_options}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_offering_with_empty_plugin_options(self):
        response = self.create_offering("owner", add_payload={"plugin_options": {}})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        default_plugin_options = {
            "account_name_generation_policy": None,
            "auto_approve_marketplace_script": True,
            "backend_id_display_label": "Backend ID",
            "enable_display_of_order_actions_for_service_provider": True,
            "enforce_qos": False,
            "expose_inference_playground": False,
            "highlight_backend_id_display": False,
            "require_effective_id_for_highlighted_display": False,
            "enable_posix_account": True,
            "homedir_prefix": "/home/",
            "login_shell": "/bin/bash",
            "uid_source": "pool",
            "gid_source": "pool",
            "emit_display_name": False,
            "emit_waldur_username": False,
            "offering_user_auto_deletion": False,
            "resource_expiration_threshold": 30,
            "resource_project_role_group_template": (
                "${resource_slug}_${rp_uuid_short}_${role_name}"
            ),
            "resource_project_role_map": {},
            "resource_role_group_template": "${resource_slug}_${role_name}",
            "resource_role_map": {},
            "slurm_periodic_policy_enabled": False,
            "username_anonymized_prefix": "waldur_",
            "username_generation_policy": "service_provider",
        }
        self.assertEqual(offering.plugin_options, default_plugin_options)

    def test_create_offering_without_plugin_options_uses_default(self):
        response = self.create_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering.plugin_options, {})


class BaseOfferingUpdateTest(test.APITestCase):
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

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data(OfferingStates.ACTIVE, OfferingStates.PAUSED)
    def test_owner_can_update_offering_in_active_or_paused_state_when_management_allowed(
        self, state
    ):
        # Arrange
        self.offering.state = state
        self.offering.save()

        # Act
        response = self.update_overview("owner")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, "new_offering")

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_owner_can_not_update_offering_in_archived_state_when_management_allowed(
        self,
    ):
        # Arrange
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()

        # Act
        response = self.update_overview("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_update_offering_when_management_allowed(
        self, role
    ):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        response = self.update_overview(role)
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


@ddt
class OfferingUpdateTypeTest(BaseOfferingUpdateTest):
    def setUp(self):
        super().setUp()
        self.offering.type = BASIC_OFFERING
        self.offering.save()

    def update_type(self, target_type, role):
        url = factories.OfferingFactory.get_url(self.offering, "update_type")
        self.client.force_authenticate(getattr(self.fixture, role))
        return self.client.post(url, {"type": target_type})

    @data("staff", "owner")
    def test_authorized_user_can_swap_basic_to_site_agent(self, role):
        response = self.update_type(SITE_AGENT_OFFERING, role)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.type, SITE_AGENT_OFFERING)

    @data("staff", "owner")
    def test_authorized_user_can_swap_site_agent_to_basic(self, role):
        self.offering.type = SITE_AGENT_OFFERING
        self.offering.save()

        response = self.update_type(BASIC_OFFERING, role)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.type, BASIC_OFFERING)

    def test_target_type_outside_swap_set_is_rejected(self):
        response = self.update_type(OPENSTACK_TENANT_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.type, BASIC_OFFERING)

    def test_source_type_outside_swap_set_is_rejected(self):
        self.offering.type = OPENSTACK_TENANT_OFFERING
        self.offering.save()

        response = self.update_type(BASIC_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.type, OPENSTACK_TENANT_OFFERING)

    def test_setting_to_same_type_is_a_noop(self):
        response = self.update_type(BASIC_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.type, BASIC_OFFERING)

    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_cannot_change_type(self, role):
        response = self.update_type(SITE_AGENT_OFFERING, role)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_unrelated_user_cannot_change_type(self):
        response = self.update_type(SITE_AGENT_OFFERING, "user")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_owner_cannot_change_type_in_active_state(self):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        response = self.update_type(SITE_AGENT_OFFERING, "owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @data(OfferingStates.ACTIVE, OfferingStates.PAUSED)
    def test_staff_can_change_type_in_non_draft_state(self, state):
        self.offering.state = state
        self.offering.save()

        response = self.update_type(SITE_AGENT_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.type, SITE_AGENT_OFFERING)

    def test_archived_offering_type_cannot_be_changed(self):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()

        response = self.update_type(SITE_AGENT_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blocked_organization_cannot_change_type(self):
        self.customer.blocked = True
        self.customer.save()

        response = self.update_type(SITE_AGENT_OFFERING, "owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_users_are_preserved_across_type_change(self):
        offering_user = factories.OfferingUserFactory(
            offering=self.offering, username="alice"
        )

        response = self.update_type(SITE_AGENT_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        offering_user.refresh_from_db()
        self.assertEqual(offering_user.offering_id, self.offering.id)
        self.assertEqual(offering_user.username, "alice")

    def test_orders_are_preserved_across_type_change(self):
        plan = factories.PlanFactory(offering=self.offering)
        order = factories.OrderFactory(
            offering=self.offering,
            project=self.fixture.project,
            plan=plan,
            state=OrderStates.DONE,
        )

        response = self.update_type(SITE_AGENT_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        order.refresh_from_db()
        self.assertEqual(order.offering_id, self.offering.id)
        self.assertEqual(order.state, OrderStates.DONE)
        self.assertEqual(order.plan_id, plan.id)

    def test_resources_are_preserved_across_type_change(self):
        resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
            name="my-resource",
        )

        response = self.update_type(SITE_AGENT_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        resource.refresh_from_db()
        self.assertEqual(resource.offering_id, self.offering.id)
        self.assertEqual(resource.state, ResourceStates.OK)
        self.assertEqual(resource.name, "my-resource")
        self.assertEqual(resource.limits, {"storage": 123})

    def test_in_flight_order_and_its_graph_survive_type_change(self):
        # An order that is still EXECUTING (and its resource being CREATED)
        # is the worst-case scenario for a type swap: the swap is allowed by
        # design, but the action must not mutate the connected objects in any
        # way (state, FKs, or even trigger a save). Otherwise the in-flight
        # work could be corrupted.
        plan = factories.PlanFactory(offering=self.offering)
        resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            plan=plan,
            state=ResourceStates.CREATING,
            name="in-flight",
        )
        order = factories.OrderFactory(
            offering=self.offering,
            project=self.fixture.project,
            plan=plan,
            resource=resource,
            state=OrderStates.EXECUTING,
        )
        offering_user = factories.OfferingUserFactory(
            offering=self.offering, username="bob"
        )

        order_modified_before = order.modified
        resource_modified_before = resource.modified
        offering_user_modified_before = offering_user.modified

        response = self.update_type(SITE_AGENT_OFFERING, "staff")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        # The swap actually happened.
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.type, SITE_AGENT_OFFERING)

        # In-flight order: state, FKs, and modified timestamp untouched.
        order.refresh_from_db()
        self.assertEqual(order.state, OrderStates.EXECUTING)
        self.assertEqual(order.offering_id, self.offering.id)
        self.assertEqual(order.resource_id, resource.id)
        self.assertEqual(order.plan_id, plan.id)
        self.assertEqual(order.modified, order_modified_before)

        # Resource: state, FKs, and modified timestamp untouched.
        resource.refresh_from_db()
        self.assertEqual(resource.state, ResourceStates.CREATING)
        self.assertEqual(resource.offering_id, self.offering.id)
        self.assertEqual(resource.plan_id, plan.id)
        self.assertEqual(resource.name, "in-flight")
        self.assertEqual(resource.modified, resource_modified_before)

        # OfferingUser: still attached, not re-saved.
        offering_user.refresh_from_db()
        self.assertEqual(offering_user.offering_id, self.offering.id)
        self.assertEqual(offering_user.username, "bob")
        self.assertEqual(offering_user.modified, offering_user_modified_before)

        # Counts: no rows added or removed.
        self.assertEqual(models.Order.objects.filter(offering=self.offering).count(), 1)
        self.assertEqual(
            models.Resource.objects.filter(offering=self.offering).count(), 1
        )
        self.assertEqual(
            models.OfferingUser.objects.filter(offering=self.offering).count(), 1
        )


@ddt
class OfferingPartialUpdateTest(test.APITestCase):
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
            .not_valid_before(datetime.datetime.now(datetime.UTC))
            .not_valid_after(
                datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=365)
            )
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

    def test_update_options_generates_audit_log(self):
        Event.objects.all().delete()
        url = factories.OfferingFactory.get_url(self.offering, "update_options")
        self.client.force_authenticate(self.fixture.owner)
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

        event = Event.objects.filter(
            event_type=EventType.MARKETPLACE_OFFERING_OPTIONS_UPDATED
        ).first()
        self.assertIsNotNone(event)
        self.assertIn(self.offering.name, event.message)
        self.assertIn("email", event.message)
        self.assertIn("Details:", event.message)
        self.assertEqual(event.context.get("offering_uuid"), self.offering.uuid.hex)
        self.assertEqual(
            event.context.get("user_username"), self.fixture.owner.username
        )

    def test_update_resource_options_generates_audit_log(self):
        Event.objects.all().delete()
        url = factories.OfferingFactory.get_url(
            self.offering, "update_resource_options"
        )
        self.client.force_authenticate(self.fixture.owner)
        resource_options = {
            "order": ["storageRequest"],
            "options": {
                "storageRequest": {
                    "type": "integer",
                    "label": "Storage request (GB)",
                    "required": True,
                    "min": 0,
                    "max": 102400,
                }
            },
        }
        response = self.client.post(url, {"resource_options": resource_options})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        event = Event.objects.filter(
            event_type=EventType.MARKETPLACE_OFFERING_RESOURCE_OPTIONS_UPDATED
        ).first()
        self.assertIsNotNone(event)
        self.assertIn(self.offering.name, event.message)
        self.assertIn("storageRequest", event.message)
        self.assertIn("Details:", event.message)

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

    def test_update_attributes_image_count_total_limit(self):
        self.offering.type = "OpenStack.Tenant"
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering, "update_attributes")

        response = self.client.post(
            url,
            {"image_count_total_limit": 10},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.attributes.get("image_count_total_limit"), 10)


@ddt
class OfferingOrganizationGroupsTest(test.APITestCase):
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
class OfferingDeleteTest(test.APITestCase):
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

    def test_staff_can_delete_offering(self):
        response = self.delete_offering("staff")
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Offering.objects.filter(customer=self.customer).exists()
        )

    def test_draft_offering_without_a_plan_can_be_deleted(self):
        # Regression: validate_offering_has_plans previously leaked into
        # destroy_validators via list aliasing, so a plan-less offering could
        # not be deleted ("Offering does not have any billing plans").
        offering = factories.OfferingFactory(
            customer=self.customer,
            project=self.fixture.project,
            shared=True,
            state=OfferingStates.DRAFT,
        )
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        response = self.client.delete(url)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(models.Offering.objects.filter(id=offering.id).exists())

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_owner_can_delete_offering_when_management_enabled(self):
        response = self.delete_offering("owner")
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

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_offering_deleting_is_not_available_for_blocked_organization(self):
        self.customer.blocked = True
        self.customer.save()
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_customer_owner_can_not_delete_offering_if_it_is_not_in_draft_state(self):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data["detail"],
            "Offering was not deleted since offering is not in draft state.",
        )

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_customer_owner_can_not_delete_offering_if_it_has_resources(self):
        self.offering.state = OfferingStates.DRAFT
        factories.ResourceFactory(offering=self.offering)
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(
            response.data["detail"], "Offering was not deleted since it has resources."
        )

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=False)
    def test_sp_can_not_delete_offering_when_management_disabled(self):
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Offering.objects.filter(customer=self.customer).exists())

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_sp_can_delete_offering_when_management_enabled(self):
        response = self.delete_offering("owner")
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )
        self.assertFalse(
            models.Offering.objects.filter(customer=self.customer).exists()
        )

    def test_staff_cannot_delete_offering_with_active_resources_when_restriction_enabled(
        self,
    ):
        self.offering.plugin_options["restrict_deletion_with_active_resources"] = True
        self.offering.save()
        factories.ResourceFactory(offering=self.offering, state=ResourceStates.OK)
        response = self.delete_offering("staff")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_staff_can_delete_offering_without_active_resources_when_restriction_enabled(
        self,
    ):
        self.offering.plugin_options["restrict_deletion_with_active_resources"] = True
        self.offering.save()
        response = self.delete_offering("staff")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Offering.objects.filter(pk=self.offering.pk).exists())

    def test_staff_can_delete_offering_with_resources_when_restriction_disabled(self):
        factories.ResourceFactory(offering=self.offering, state=ResourceStates.OK)
        response = self.delete_offering("staff")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Offering.objects.filter(pk=self.offering.pk).exists())

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    def test_owner_cannot_delete_offering_with_active_resources_when_restriction_enabled(
        self,
    ):
        self.offering.plugin_options["restrict_deletion_with_active_resources"] = True
        self.offering.save()
        factories.ResourceFactory(offering=self.offering, state=ResourceStates.OK)
        response = self.delete_offering("owner")
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    def test_deletion_allowed_when_all_resources_terminated_and_restriction_enabled(
        self,
    ):
        self.offering.plugin_options["restrict_deletion_with_active_resources"] = True
        self.offering.save()
        factories.ResourceFactory(
            offering=self.offering, state=ResourceStates.TERMINATED
        )
        response = self.delete_offering("staff")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Offering.objects.filter(pk=self.offering.pk).exists())

    def delete_offering(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.delete(url)
        return response

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
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
class OfferingAttributesTest(test.APITestCase):
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


class OfferingQuotaTest(test.APITestCase):
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
class OfferingStateTest(test.APITestCase):
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

        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.CREATE_OFFERING)

        CustomerRole.OWNER.add_permission(PermissionEnum.ARCHIVE_OFFERING)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.ARCHIVE_OFFERING)

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

    @data("owner", "customer_support", "admin", "manager", "service_manager")
    def test_unauthorized_user_can_not_activate_offering(self, user):
        response, offering = self.update_offering_state(user, "activate")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.DRAFT)

    def test_user_without_offering_access_can_not_activate_offering(self):
        response, offering = self.update_offering_state("user", "activate")
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(offering.state, OfferingStates.DRAFT)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("staff", "owner", "service_manager")
    def test_authorized_user_can_activate_offering_when_sp_management_enabled(
        self, user
    ):
        response, offering = self.update_offering_state(user, "activate")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_activate_offering_when_sp_management_enabled(
        self, user
    ):
        response, offering = self.update_offering_state(user, "activate")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.DRAFT)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=False)
    @data("owner", "service_manager")
    def test_sp_can_not_activate_offering_when_management_disabled(self, user):
        response, offering = self.update_offering_state(user, "activate")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.DRAFT)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=False)
    @data("owner", "service_manager")
    def test_sp_can_not_pause_offering_when_management_disabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "pause")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=False)
    @data("owner", "service_manager")
    def test_sp_can_not_unpause_offering_when_management_disabled(self, user):
        self.offering.state = OfferingStates.PAUSED
        self.offering.save()
        response, offering = self.update_offering_state(user, "unpause")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.PAUSED)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=False)
    @data("owner", "service_manager")
    def test_sp_can_not_archive_offering_when_management_disabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "archive")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=False)
    @data("owner", "service_manager")
    def test_sp_can_not_draft_offering_when_management_disabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "draft")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=False)
    @data("owner", "service_manager")
    def test_sp_can_not_make_unavailable_when_management_disabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "make_unavailable")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_sp_can_pause_offering_when_management_enabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "pause")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.PAUSED)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_sp_can_unpause_offering_when_management_enabled(self, user):
        self.offering.state = OfferingStates.PAUSED
        self.offering.save()
        response, offering = self.update_offering_state(user, "unpause")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_sp_can_archive_offering_when_management_enabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "archive")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.ARCHIVED)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_sp_can_draft_offering_when_management_enabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "draft")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.DRAFT)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_sp_can_make_unavailable_when_management_enabled(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()
        response, offering = self.update_offering_state(user, "make_unavailable")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.UNAVAILABLE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
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

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_authorized_user_can_mark_offering_unavailable(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        response, offering = self.update_offering_state(user, "make_unavailable")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.UNAVAILABLE)

    @data("customer_support", "admin", "manager")
    def test_unauthorized_user_can_not_mark_offering_unavailable(self, user):
        self.offering.state = OfferingStates.ACTIVE
        self.offering.save()

        response, offering = self.update_offering_state(user, "make_unavailable")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, response.data)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_authorized_user_can_unpause_offering(self, user):
        self.offering.state = OfferingStates.PAUSED
        self.offering.save()

        response, offering = self.update_offering_state(user, "unpause")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(offering.state, OfferingStates.ACTIVE)

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
    @data("owner", "service_manager")
    def test_authorized_user_can_make_unavailable_offering_active(self, user):
        self.offering.state = OfferingStates.UNAVAILABLE
        self.offering.save()

        response, offering = self.update_offering_state(user, "make_available")
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

    @override_config(ALLOW_SERVICE_PROVIDER_OFFERING_MANAGEMENT=True)
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
class OfferingPublicGetTest(test.APITestCase):
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


class OfferingExportImportTest(test.APITestCase):
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

    def test_import_offering_with_date_in_plugin_options(self):
        export_data = self._get_data()
        export_data["plugin_options"] = {
            "latest_date_for_resource_termination": "2026-12-31",
        }
        offering = create_offering(export_data, self.fixture.customer)

        offering.refresh_from_db()
        self.assertEqual(
            offering.plugin_options["latest_date_for_resource_termination"],
            "2026-12-31",
        )

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
        data = load_json_resource("offering.json", __name__)
        category = factories.CategoryFactory()
        data["category_id"] = category.id

        thumbnail = data.get("thumbnail")
        if thumbnail:
            # Materialize a real thumbnail in a short-path temp dir: the
            # importer copies the file's content into storage, so the file must
            # actually exist and its (basename-derived) stored name must stay
            # within the field's varchar(100).
            GIF = "R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7"
            thumbnail_path = os.path.join(self.temp_dir, thumbnail)
            with open(thumbnail_path, "wb") as pic:
                pic.write(base64.b64decode(GIF))
            data["thumbnail"] = thumbnail_path

        return data


class OfferingDoiTest(test.APITestCase):
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
class OfferingThumbnailTest(test.APITestCase):
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

    def update_thumbnail_svg(self):
        url = factories.OfferingFactory.get_url(
            offering=self.offering, action="update_thumbnail"
        )
        return self.client.post(url, {"thumbnail": dummy_svg()}, format="multipart")

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
        response = self.update_thumbnail_svg()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.thumbnail)

    def _test_negative(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.update_thumbnail()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.offering.refresh_from_db()
        self.assertFalse(self.offering.thumbnail)

        response = self.delete_thumbnail()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        response = self.update_thumbnail_svg()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_can_upload_svg_thumbnail(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.update_thumbnail_svg()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertTrue(self.offering.thumbnail)


@ddt
class OfferingMarkdownImageUploadTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.state = OfferingStates.DRAFT
        self.offering.save()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_DESCRIPTION)
        self.url = factories.OfferingFactory.get_url(
            self.offering, action="upload_markdown_image"
        )

    def upload_markdown_image(self):
        return self.client.post(self.url, {"image": dummy_image()}, format="multipart")

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=False)
    def test_upload_rejected_when_disabled(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.upload_markdown_image()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    @data("offering_admin", "offering_manager")
    def test_user_without_permission_cannot_upload(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.upload_markdown_image()
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    def test_unrelated_user_cannot_upload(self):
        user = UserFactory()
        self.client.force_authenticate(user)
        response = self.upload_markdown_image()
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    def test_authorized_user_can_upload_markdown_image(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.upload_markdown_image()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("url", response.data)
        self.assertIn("/api/media/", response.data["url"])

        file_uuid = response.data["url"].rstrip("/").split("/")[-1]
        stored_file = File.objects.get(uuid=file_uuid)
        self.assertTrue(stored_file.name.startswith("markdown_images/"))

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True, MARKDOWN_IMAGE_MAX_SIZE_MB=0)
    def test_oversized_image_rejected(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.upload_markdown_image()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    def test_anonymous_user_can_view_uploaded_markdown_image(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.upload_markdown_image()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        self.client.logout()
        media_response = self.client.get(response.data["url"])
        self.assertEqual(media_response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            media_response.headers["Content-Disposition"].startswith("inline")
        )

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    def test_upload_rejected_for_archived_offering(self):
        self.offering.state = OfferingStates.ARCHIVED
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.upload_markdown_image()
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    def test_non_image_upload_rejected(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {
                "image": SimpleUploadedFile(
                    "notes.txt",
                    b"plain text content",
                    content_type="text/plain",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    def test_pdf_upload_rejected(self):
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {
                "image": SimpleUploadedFile(
                    "document.pdf",
                    b"%PDF-1.4 not really a pdf",
                    content_type="application/pdf",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(ENABLE_MARKDOWN_IMAGE_UPLOAD=True)
    def test_svg_upload_rejected(self):
        svg_content = b"""<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg">
  <circle cx="50" cy="50" r="40" fill="red"/>
</svg>"""
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(
            self.url,
            {
                "image": SimpleUploadedFile(
                    "chart.svg",
                    svg_content,
                    content_type="image/svg+xml",
                )
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class OfferingCreateComponentsTest(test.APITestCase):
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
            "billing_type": BillingTypes.USAGE,
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
        self.assertEqual(BillingTypes.USAGE, offering_component.billing_type)

    @data("offering_owner", "service_manager")
    def test_offering_components_create_with_invalid_payload_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "billing_type": BillingTypes.USAGE,
            "name": "updated_name",
            "measured_unit": "cpu_k_hours",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(400, response.status_code)

    @data("offering_owner", "service_manager")
    def test_offering_components_create_to_builtin_type_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        self.offering.type = VMWARE_VM_OFFERING
        self.offering.save()

        payload = {
            "billing_type": BillingTypes.USAGE,
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
class OfferingUpdateComponentsTest(test.APITestCase):
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
            "billing_type": BillingTypes.USAGE,
            "type": "updated_type",
            "name": "updated_name",
            "measured_unit": "cpu_k_hours",
        }

        self.assertEqual("CPU", self.offering_component.name)
        self.assertEqual("cpu", self.offering_component.type)
        self.assertEqual(
            BillingTypes.FIXED,
            self.offering_component.billing_type,
        )

        response = self.client.post(self.url, payload)
        self.assertEqual(200, response.status_code)
        offering_component = models.OfferingComponent.objects.get(
            offering=self.offering, uuid=self.offering_component.uuid
        )

        self.assertEqual("updated_name", offering_component.name)
        self.assertEqual("updated_type", offering_component.type)
        self.assertEqual(BillingTypes.USAGE, offering_component.billing_type)

    @data("offering_owner", "service_manager")
    def test_offering_components_update_with_invalid_payload_failed(self, user):
        self.client.force_login(getattr(self.fixture, user))
        payload = {
            "billing_type": BillingTypes.USAGE,
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
        self.offering.type = VMWARE_VM_OFFERING
        self.offering.save()

        payload = {
            "billing_type": BillingTypes.USAGE,
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
class OfferingRemoveComponentsTest(test.APITestCase):
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
            "billing_type": BillingTypes.USAGE,
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
            "billing_type": BillingTypes.USAGE,
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
class OfferingBackendMetadataTest(test.APITestCase):
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
class ListCustomerProjectsTest(test.APITestCase):
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

    @data("staff", "offering_owner")
    def test_list_customer_projects_pagination(self, user):
        import uuid

        from waldur_core.structure.tests.factories import ProjectFactory

        for i in range(15):
            project = ProjectFactory(customer=self.fixture.project.customer)
            self.fixture.resource.pk = None
            self.fixture.resource.project = project
            self.fixture.resource.uuid = uuid.uuid4()  # Ensure unique UUID
            self.fixture.resource.save()
        response = self.get_list_customer_projects(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        # Waldur pagination: response body is a list, pagination info is in headers
        self.assertIsInstance(data, list)
        self.assertLessEqual(len(data), 10)  # default page size is 10
        headers = dict(response.headers)
        self.assertIn(RESULT_COUNT_HEADER, headers)
        self.assertGreaterEqual(int(headers[RESULT_COUNT_HEADER]), 15)
        self.assertIn("Link", headers)

    def test_list_customer_projects_does_not_have_n_plus_one_queries(self):
        """Query count should stay constant when more projects are added."""
        user = self.fixture.staff
        self.client.force_authenticate(user)
        url = factories.OfferingFactory.get_url(
            self.fixture.offering, "list_customer_projects"
        )

        # Measure queries with 1 project
        with CaptureQueriesContext(db_connection) as ctx_before:
            self.client.get(url)
        num_queries_1 = len(ctx_before)

        # Add more projects with resources
        for _ in range(5):
            project = structure_factories.ProjectFactory(
                customer=self.fixture.project.customer
            )
            factories.ResourceFactory(
                offering=self.fixture.offering,
                project=project,
                state=ResourceStates.OK,
            )

        # Measure queries with 6 projects
        with CaptureQueriesContext(db_connection) as ctx_after:
            self.client.get(url)
        num_queries_6 = len(ctx_after)

        self.assertEqual(num_queries_1, num_queries_6)


@ddt
class ListCustomerUsersTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.fixture.resource.state = ResourceStates.OK
        self.fixture.resource.save()
        self.fixture.admin
        # Create consent so user is visible on request
        models.UserOfferingConsent.objects.create(
            user=self.fixture.admin,
            offering=self.fixture.offering,
            version="1.0",
        )

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

    @data("staff", "offering_owner")
    def test_list_customer_users_pagination(self, user):
        from waldur_core.structure.tests.factories import UserFactory

        project = self.fixture.project
        for i in range(15):
            user_obj = UserFactory()
            project.add_user(user_obj, ProjectRole.ADMIN)
            # Create consent so user is visible
            models.UserOfferingConsent.objects.create(
                user=user_obj,
                offering=self.fixture.offering,
                version="1.0",
            )
        response = self.get_list_customer_users(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        # Waldur pagination: response body is a list, pagination info is in headers
        self.assertIsInstance(data, list)
        self.assertLessEqual(len(data), 10)  # default page size is 10
        headers = dict(response.headers)
        self.assertIn("X-Result-Count", headers)
        self.assertGreaterEqual(int(headers["X-Result-Count"]), 15)
        self.assertIn("Link", headers)

    @data("staff", "offering_owner")
    def test_users_with_requires_reconsent_are_still_visible_to_sp(self, user):
        """Test that users are still visible to SPs when ToS is updated with requires_reconsent=True."""

        initial_tos = models.OfferingTermsOfService.objects.create(
            offering=self.fixture.offering,
            terms_of_service="Initial Terms of Service",
            version="1.0",
            requires_reconsent=False,
            is_active=True,
        )

        # Step 2: User consents to the  ToS
        test_user = UserFactory()
        self.fixture.project.add_user(test_user, ProjectRole.ADMIN)
        models.UserOfferingConsent.objects.create(
            user=test_user,
            offering=self.fixture.offering,
            version="1.0",
        )

        # Step 3: Admin updates ToS and sets requires_reconsent=True
        initial_tos.terms_of_service = "Updated Terms requiring re-acceptance"
        initial_tos.version = "2.0"
        initial_tos.requires_reconsent = True
        initial_tos.save()

        # Step 4: User should still be visible to SP despite requires_reconsent=True
        response = self.get_list_customer_users(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user_uuids = [user_data["uuid"] for user_data in response.data]
        self.assertIn(test_user.uuid.hex, user_uuids)

        self.assertGreaterEqual(len(response.data), 2)


class ResourceOfferingsViewSetTest(test.APITestCase):
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
class RefreshOfferingUsernamesTest(test.APITestCase):
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


class OrderNotificationTest(test.APITestCase):
    def setUp(self):
        self.order = factories.OrderFactory(state=OrderStates.PENDING_PROVIDER)

    @mock.patch(
        "waldur_mastermind.marketplace.tasks.notify_user_that_order_been_rejected.delay"
    )
    def test_notify_user_when_order_rejected(self, mock_notify):
        self.order.state = OrderStates.REJECTED
        self.order.save()
        mock_notify.assert_called_once_with(self.order.uuid.hex)


class ProviderOfferingOrdersTest(test.APITestCase):
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


class OfferingMoveTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.OfferingFactory.get_url(self.offering, "move_offering")
        self.offering.customer = self.fixture.customer
        self.offering.save()
        self.valid_customer = structure_factories.CustomerFactory()
        factories.ServiceProviderFactory(customer=self.valid_customer)
        self.invalid_customer = structure_factories.CustomerFactory()

    def _move_offering(self, customer):
        self.client.force_authenticate(self.fixture.staff)
        payload = {
            "customer": structure_factories.CustomerFactory.get_url(customer),
            "preserve_permissions": True,
        }
        return self.client.post(self.url, payload)

    def test_move_offering_positive(self):
        response = self._move_offering(self.valid_customer)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.offering.refresh_from_db()
        self.assertEqual(self.offering.customer, self.valid_customer)

    def test_move_offering_fails_with_blocked_customer(self):
        self.valid_customer.blocked = True
        self.valid_customer.save()

        response = self._move_offering(self.valid_customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.offering.customer, self.fixture.customer)

    def test_move_offering_fails_when_target_customer_does_not_have_sp_profile(self):
        response = self._move_offering(self.invalid_customer)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.offering.customer, self.fixture.customer)


class OfferingComplianceChecklistSerializerTest(test.APITestCase):
    """Test that ProviderOfferingDetailsSerializer exposes compliance_checklist field."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer, shared=True, state=OfferingStates.ACTIVE
        )

    def test_offering_without_compliance_checklist_shows_null(self):
        """Test that offering without compliance checklist shows null in serializer."""
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_url(self.offering)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("compliance_checklist", response.data)
        self.assertIsNone(response.data["compliance_checklist"])
        self.assertIn("has_compliance_requirements", response.data)
        self.assertFalse(response.data["has_compliance_requirements"])

    def test_offering_with_compliance_checklist_shows_checklist_info(self):
        """Test that offering with compliance checklist shows checklist information."""
        from waldur_core.checklist.tests import factories as checklist_factories

        # Create checklist and assign to offering
        checklist = checklist_factories.ChecklistFactory(
            name="Test Compliance Checklist",
            checklist_type=checklist_enums.ChecklistTypes.OFFERING_COMPLIANCE,
        )
        self.offering.compliance_checklist = checklist
        self.offering.save()

        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_url(self.offering)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("compliance_checklist", response.data)
        self.assertIsNotNone(response.data["compliance_checklist"])

        # The compliance_checklist field returns a URL string according to HyperlinkedRelatedField
        # Check that it contains the expected URL pattern
        compliance_checklist_url = response.data["compliance_checklist"]
        expected_url_pattern = f"/api/checklists-admin/{checklist.uuid}/"
        self.assertIn(expected_url_pattern, compliance_checklist_url)

        # Check has_compliance_requirements field
        self.assertIn("has_compliance_requirements", response.data)
        self.assertTrue(response.data["has_compliance_requirements"])

    def test_offering_compliance_checklist_url_structure(self):
        """Test that compliance_checklist field follows proper URL structure."""
        from waldur_core.checklist.tests import factories as checklist_factories

        checklist = checklist_factories.ChecklistFactory(
            checklist_type=checklist_enums.ChecklistTypes.OFFERING_COMPLIANCE
        )
        self.offering.compliance_checklist = checklist
        self.offering.save()

        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_url(self.offering)

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check that the URL follows the expected pattern for checklists-admin-detail
        expected_url_pattern = f"/api/checklists-admin/{checklist.uuid}/"
        self.assertIn(expected_url_pattern, response.data["compliance_checklist"])

    def test_offering_list_includes_compliance_checklist_field(self):
        """Test that offering list endpoint includes compliance_checklist field."""
        from waldur_core.checklist.tests import factories as checklist_factories

        # Create offering with checklist
        checklist = checklist_factories.ChecklistFactory(
            name="List Test Checklist",
            checklist_type=checklist_enums.ChecklistTypes.OFFERING_COMPLIANCE,
        )
        self.offering.compliance_checklist = checklist
        self.offering.save()

        # Create offering without checklist
        offering_without_checklist = factories.OfferingFactory(
            customer=self.customer, shared=True, state=OfferingStates.ACTIVE
        )

        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_list_url()

        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 2)

        # Find our offerings in the response
        offering_with_checklist_data = None
        offering_without_checklist_data = None

        for item in response.data:
            if item["uuid"] == str(self.offering.uuid):
                offering_with_checklist_data = item
            elif item["uuid"] == str(offering_without_checklist.uuid):
                offering_without_checklist_data = item

        # Verify offering with checklist
        self.assertIsNotNone(offering_with_checklist_data)
        self.assertIn("compliance_checklist", offering_with_checklist_data)
        self.assertIsNotNone(offering_with_checklist_data["compliance_checklist"])
        # Check that URL contains the checklist UUID
        expected_url_pattern = f"/api/checklists-admin/{checklist.uuid}/"
        self.assertIn(
            expected_url_pattern, offering_with_checklist_data["compliance_checklist"]
        )
        self.assertTrue(offering_with_checklist_data["has_compliance_requirements"])

        # Verify offering without checklist
        self.assertIsNotNone(offering_without_checklist_data)
        self.assertIn("compliance_checklist", offering_without_checklist_data)
        self.assertIsNone(offering_without_checklist_data["compliance_checklist"])
        self.assertFalse(offering_without_checklist_data["has_compliance_requirements"])


class CheckUniqueBackendIDTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.url = factories.OfferingFactory.get_url(
            self.offering, "check_unique_backend_id"
        )

        # Create resources with backend_ids
        self.resource1 = factories.ResourceFactory(
            offering=self.offering, backend_id="backend_001"
        )
        self.resource2 = factories.ResourceFactory(
            offering=self.offering, backend_id="backend_002"
        )

        # Create another offering in same customer with resource
        self.other_offering = factories.OfferingFactory(customer=self.fixture.customer)
        self.other_resource = factories.ResourceFactory(
            offering=self.other_offering, backend_id="backend_003"
        )

        # Create resource in different customer
        self.different_customer = structure_factories.CustomerFactory()
        self.different_offering = factories.OfferingFactory(
            customer=self.different_customer
        )
        self.different_resource = factories.ResourceFactory(
            offering=self.different_offering,
            backend_id="backend_001",  # Same backend_id but different customer
        )

    def test_staff_can_check_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"backend_id": "new_backend_id"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_offering_manager_can_check_backend_id(self):
        self.offering.add_user(self.fixture.owner, OfferingRole.MANAGER)

        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"backend_id": "new_backend_id"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_check_unique_backend_id_within_offering(self):
        self.client.force_authenticate(self.fixture.staff)

        # Check existing backend_id in same offering
        response = self.client.post(self.url, {"backend_id": "backend_001"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

        # Check non-existing backend_id
        response = self.client.post(self.url, {"backend_id": "new_backend_id"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_check_unique_backend_id_across_customer_offerings(self):
        self.client.force_authenticate(self.fixture.staff)

        # Check with check_all_offerings=True - should find conflicts in other offerings
        response = self.client.post(
            self.url, {"backend_id": "backend_003", "check_all_offerings": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

        # Check with check_all_offerings=False - should not find conflicts in other offerings
        response = self.client.post(
            self.url, {"backend_id": "backend_003", "check_all_offerings": False}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_backend_id_isolation_between_customers(self):
        self.client.force_authenticate(self.fixture.staff)

        # backend_001 exists in different customer, should not conflict
        response = self.client.post(
            self.url, {"backend_id": "backend_001", "check_all_offerings": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should find conflict only in same customer, not in different customer
        self.assertFalse(response.data["is_unique"])

    def test_terminated_resources_are_included(self):
        # Terminate a resource
        self.resource1.state = ResourceStates.TERMINATED
        self.resource1.save()

        self.client.force_authenticate(self.fixture.staff)

        # Should still find the terminated resource as a conflict
        response = self.client.post(self.url, {"backend_id": "backend_001"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

    def test_invalid_data_validation(self):
        self.client.force_authenticate(self.fixture.staff)

        # Missing backend_id
        response = self.client.post(self.url, {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

        # Too long backend_id
        response = self.client.post(self.url, {"backend_id": "a" * 256})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_permission_denied_for_unauthorized_user(self):
        unauthorized_user = UserFactory()
        self.client.force_authenticate(unauthorized_user)

        response = self.client.post(self.url, {"backend_id": "test"})
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_empty_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)

        # Empty backend_id should be rejected by validation
        response = self.client.post(self.url, {"backend_id": ""})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_whitespace_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)

        # Create resource with whitespace backend_id
        factories.ResourceFactory(
            offering=self.offering, backend_id="spaces_with_suffix"
        )

        # Check exact match
        response = self.client.post(self.url, {"backend_id": "spaces_with_suffix"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

        # Check different value should be unique
        response = self.client.post(
            self.url, {"backend_id": "spaces_with_different_suffix"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_case_sensitivity(self):
        self.client.force_authenticate(self.fixture.staff)

        # backend_id is case-sensitive
        response = self.client.post(self.url, {"backend_id": "BACKEND_001"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(
            response.data["is_unique"]
        )  # backend_001 exists, but BACKEND_001 doesn't

        response = self.client.post(self.url, {"backend_id": "backend_001"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])  # exact match exists

    def test_different_resource_states(self):
        self.client.force_authenticate(self.fixture.staff)

        # Create resources in different states with same backend_id
        backend_id = "test_state_backend"

        # Active resource
        active_resource = factories.ResourceFactory(
            offering=self.offering, backend_id=backend_id, state=ResourceStates.OK
        )

        response = self.client.post(self.url, {"backend_id": backend_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

        # Change to erred state - should still not be unique
        active_resource.state = ResourceStates.ERRED
        active_resource.save()

        response = self.client.post(self.url, {"backend_id": backend_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

        # Change to creating state - should still not be unique
        active_resource.state = ResourceStates.CREATING
        active_resource.save()

        response = self.client.post(self.url, {"backend_id": backend_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

    def test_customer_owner_permissions(self):
        # Customer owner should have access through UPDATE_OFFERING permission
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.post(self.url, {"backend_id": "new_backend_id"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_unicode_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)

        # Create resource with Unicode backend_id
        unicode_backend_id = "тест_бэкенд_123_🚀"
        factories.ResourceFactory(offering=self.offering, backend_id=unicode_backend_id)

        # Check Unicode backend_id
        response = self.client.post(self.url, {"backend_id": unicode_backend_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

        # Check similar but different Unicode
        response = self.client.post(self.url, {"backend_id": "тест_бэкенд_124_🚀"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_special_characters_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)

        special_chars = "!@#$%^&*()_+-=[]{}|;':\",./<>?"
        factories.ResourceFactory(offering=self.offering, backend_id=special_chars)

        # Check exact match
        response = self.client.post(self.url, {"backend_id": special_chars})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

    def test_maximum_length_backend_id(self):
        self.client.force_authenticate(self.fixture.staff)

        # Create resource with maximum length backend_id (255 chars)
        max_length_backend_id = "a" * 255
        factories.ResourceFactory(
            offering=self.offering, backend_id=max_length_backend_id
        )

        # Check exact match
        response = self.client.post(self.url, {"backend_id": max_length_backend_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

        # Check one character shorter
        response = self.client.post(self.url, {"backend_id": "a" * 254})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

    def test_multiple_resources_same_backend_id_same_offering(self):
        self.client.force_authenticate(self.fixture.staff)

        backend_id = "duplicate_backend"

        # Create multiple resources with same backend_id in same offering
        # Note: This might not be allowed by database constraints, but testing the check behavior
        try:
            factories.ResourceFactory(offering=self.offering, backend_id=backend_id)
            factories.ResourceFactory(offering=self.offering, backend_id=backend_id)
        except Exception:
            # If database prevents duplicates, create just one
            factories.ResourceFactory(offering=self.offering, backend_id=backend_id)

        response = self.client.post(self.url, {"backend_id": backend_id})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])

    def test_performance_with_many_resources(self):
        self.client.force_authenticate(self.fixture.staff)

        # Create many resources with different backend_ids
        import time

        start_time = time.time()

        for i in range(100):
            factories.ResourceFactory(
                offering=self.offering, backend_id=f"bulk_backend_{i}"
            )

        # Check uniqueness for new backend_id - should be fast
        response = self.client.post(self.url, {"backend_id": "unique_new_backend"})
        end_time = time.time()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

        # Should complete in reasonable time (less than 10 seconds)
        self.assertLess(end_time - start_time, 10.0)

    def test_check_all_offerings_with_mixed_customers(self):
        self.client.force_authenticate(self.fixture.staff)

        # Create multiple customers with offerings and resources
        customer2 = structure_factories.CustomerFactory()
        structure_factories.CustomerFactory()

        offering2 = factories.OfferingFactory(
            customer=self.fixture.customer
        )  # Same customer
        offering3 = factories.OfferingFactory(customer=customer2)  # Different customer

        shared_backend_id = "shared_across_customers"

        # Create resources in different customers with same backend_id
        factories.ResourceFactory(offering=offering2, backend_id=shared_backend_id)
        factories.ResourceFactory(offering=offering3, backend_id=shared_backend_id)

        # Check within customer scope - should find conflict in same customer
        response = self.client.post(
            self.url, {"backend_id": shared_backend_id, "check_all_offerings": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(
            response.data["is_unique"]
        )  # Found in same customer's other offering

        # Check offering scope - should not find conflict (not in current offering)
        response = self.client.post(
            self.url, {"backend_id": shared_backend_id, "check_all_offerings": False}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])  # Not in current offering

    def test_null_and_none_handling(self):
        self.client.force_authenticate(self.fixture.staff)

        # Backend_id field doesn't allow null values, so test string "None"
        # Check for string "None" - should be unique since no resource has this exact string
        response = self.client.post(self.url, {"backend_id": "None"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])

        # Create resource with string "null"
        factories.ResourceFactory(offering=self.offering, backend_id="null")

        # Check for string "null" - should not be unique now
        response = self.client.post(self.url, {"backend_id": "null"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])


class BackendIdRulesConfigurationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_OPTIONS)
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        self.url = factories.OfferingFactory.get_url(
            self.offering, "update_backend_id_rules"
        )

    def test_staff_can_set_valid_format_rules(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {
            "format": {
                "regex": r"^[A-Z]{2}-\d{6}$",
                "description": "Must be 2 uppercase letters, dash, 6 digits",
            }
        }
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.backend_id_rules["format"]["regex"], r"^[A-Z]{2}-\d{6}$"
        )

    def test_owner_can_set_rules(self):
        self.client.force_authenticate(self.fixture.owner)
        rules = {"uniqueness": {"scope": "offering"}}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.offering.backend_id_rules["uniqueness"]["scope"], "offering"
        )

    def test_set_combined_format_and_uniqueness_rules(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {
            "format": {"regex": r"^RES-\d+$"},
            "uniqueness": {"scope": "service_provider"},
        }
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.backend_id_rules, rules)

    def test_set_empty_rules_clears_validation(self):
        self.offering.backend_id_rules = {"format": {"regex": r"^[A-Z]+$"}}
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"backend_id_rules": {}}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.backend_id_rules, {})

    def test_reject_invalid_regex(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {"format": {"regex": "[invalid("}}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_dangerous_regex(self):
        self.client.force_authenticate(self.fixture.staff)
        # Adjacent quantifiers pattern: detected by UserDetailsMatchMixin
        rules = {"format": {"regex": "a+?+"}}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_too_long_regex(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {"format": {"regex": "a" * 201}}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_invalid_uniqueness_scope(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {"uniqueness": {"scope": "invalid_scope"}}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_unknown_top_level_keys(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {"format": {"regex": "^ok$"}, "unknown_key": True}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_unknown_format_keys(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {"format": {"regex": "^ok$", "extra_key": "value"}}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_unknown_uniqueness_keys(self):
        self.client.force_authenticate(self.fixture.staff)
        rules = {"uniqueness": {"scope": "offering", "extra": True}}
        response = self.client.post(
            self.url, {"backend_id_rules": rules}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_all_valid_uniqueness_scopes_accepted(self):
        self.client.force_authenticate(self.fixture.staff)
        for scope in [
            "offering",
            "offering_group",
            "service_provider",
            "service_provider_category",
        ]:
            rules = {"uniqueness": {"scope": scope}}
            response = self.client.post(
                self.url, {"backend_id_rules": rules}, format="json"
            )
            self.assertEqual(
                response.status_code, status.HTTP_200_OK, f"Scope {scope} rejected"
            )

    def test_rules_visible_in_provider_offering_detail(self):
        rules = {
            "format": {"regex": r"^[A-Z]{2}-\d{6}$"},
            "uniqueness": {"scope": "offering"},
        }
        self.offering.backend_id_rules = rules
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(self.offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["backend_id_rules"], rules)


class BackendIdFormatValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_ID)
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
            backend_id_rules={
                "format": {
                    "regex": r"^[A-Z]{2}-\d{6}$",
                    "description": "Must be 2 uppercase letters, dash, 6 digits",
                }
            },
        )
        self.resource = factories.ResourceFactory(
            offering=self.offering,
            project=self.fixture.project,
            state=ResourceStates.OK,
        )
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="set_backend_id"
        )

    def test_set_backend_id_valid_format(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"backend_id": "AB-123456"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.backend_id, "AB-123456")

    def test_set_backend_id_invalid_format_rejected(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"backend_id": "invalid"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("backend_id", response.data)

    def test_set_backend_id_empty_bypasses_validation(self):
        self.resource.backend_id = "AB-111111"
        self.resource.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"backend_id": ""})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_no_rules_allows_any_format(self):
        self.offering.backend_id_rules = {}
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"backend_id": "anything-goes"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_format_validation_error_includes_description(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, {"backend_id": "bad"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        error_msg = str(response.data["backend_id"])
        self.assertIn("2 uppercase letters", error_msg)


class BackendIdUniquenessValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.SET_RESOURCE_BACKEND_ID)

        self.category = factories.CategoryFactory()
        self.other_category = factories.CategoryFactory()

        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
            category=self.category,
        )
        self.offering2 = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
            category=self.category,
        )
        self.offering_other_category = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
            category=self.other_category,
        )

        other_customer = structure_factories.CustomerFactory()
        self.offering_other_provider = factories.OfferingFactory(
            customer=other_customer,
            state=OfferingStates.ACTIVE,
            category=self.category,
        )

    def _create_resource(self, offering, backend_id, state=ResourceStates.OK):
        return factories.ResourceFactory(
            offering=offering,
            project=self.fixture.project,
            backend_id=backend_id,
            state=state,
        )

    def _set_rules(self, offering, scope, include_terminated=True):
        offering.backend_id_rules = {
            "uniqueness": {"scope": scope, "include_terminated": include_terminated}
        }
        offering.save()

    def _set_backend_id(self, resource, backend_id):
        url = factories.ResourceFactory.get_provider_resource_url(
            resource, action="set_backend_id"
        )
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(url, {"backend_id": backend_id})

    # --- offering scope ---
    def test_offering_scope_rejects_duplicate_in_same_offering(self):
        self._set_rules(self.offering, "offering")
        self._create_resource(self.offering, "DUP-001")
        resource2 = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource2, "DUP-001")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_scope_allows_same_id_different_offering(self):
        self._set_rules(self.offering, "offering")
        self._create_resource(self.offering2, "DUP-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "DUP-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_offering_scope_includes_terminated_resources(self):
        self._set_rules(self.offering, "offering")
        self._create_resource(
            self.offering, "TERM-001", state=ResourceStates.TERMINATED
        )
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "TERM-001")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- include_terminated=false ---
    def test_include_terminated_false_excludes_terminated(self):
        self._set_rules(self.offering, "offering", include_terminated=False)
        self._create_resource(
            self.offering, "TERM-001", state=ResourceStates.TERMINATED
        )
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "TERM-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_include_terminated_false_rejects_active_duplicate(self):
        self._set_rules(self.offering, "offering", include_terminated=False)
        self._create_resource(self.offering, "ACT-001", state=ResourceStates.OK)
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "ACT-001")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    # --- offering_group scope ---
    def test_offering_group_scope_rejects_duplicate_across_offerings_with_same_backend_id(
        self,
    ):
        self.offering.backend_id = "vcluster-errigal"
        self.offering.save()
        self.offering2.backend_id = "vcluster-errigal"
        self.offering2.save()
        self._set_rules(self.offering, "offering_group")
        self._create_resource(self.offering2, "RES-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "RES-001")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_offering_group_scope_allows_different_offering_backend_id(self):
        self.offering.backend_id = "vcluster-errigal"
        self.offering.save()
        self.offering2.backend_id = "vcluster-kay"
        self.offering2.save()
        self._set_rules(self.offering, "offering_group")
        self._create_resource(self.offering2, "RES-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "RES-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_offering_group_scope_falls_back_to_offering_when_no_backend_id(self):
        self.offering.backend_id = ""
        self.offering.save()
        self._set_rules(self.offering, "offering_group")
        self._create_resource(self.offering2, "RES-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "RES-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- service_provider scope ---
    def test_service_provider_scope_rejects_duplicate_across_offerings(self):
        self._set_rules(self.offering, "service_provider")
        self._create_resource(self.offering2, "SP-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "SP-001")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_service_provider_scope_allows_different_provider(self):
        self._set_rules(self.offering, "service_provider")
        self._create_resource(self.offering_other_provider, "SP-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "SP-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- service_provider_category scope ---
    def test_service_provider_category_scope_rejects_same_category(self):
        self._set_rules(self.offering, "service_provider_category")
        self._create_resource(self.offering2, "SPC-001")  # same customer, same category
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "SPC-001")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_service_provider_category_scope_allows_different_category(self):
        self._set_rules(self.offering, "service_provider_category")
        self._create_resource(self.offering_other_category, "SPC-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "SPC-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- exclude_resource (updating own ID) ---
    def test_updating_own_backend_id_to_same_value_is_allowed(self):
        self._set_rules(self.offering, "offering")
        resource = self._create_resource(self.offering, "MY-001")
        response = self._set_backend_id(resource, "MY-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    # --- no rules configured ---
    def test_no_uniqueness_rules_allows_duplicates(self):
        self.offering.backend_id_rules = {}
        self.offering.save()
        self._create_resource(self.offering, "DUP-001")
        resource = self._create_resource(self.offering, "")
        response = self._set_backend_id(resource, "DUP-001")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class CheckUniqueBackendIdWithRulesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
            backend_id_rules={
                "format": {
                    "regex": r"^[A-Z]{2}-\d{6}$",
                    "description": "Must be 2 uppercase letters, dash, 6 digits",
                },
                "uniqueness": {"scope": "offering"},
            },
        )
        self.url = factories.OfferingFactory.get_url(
            self.offering, "check_unique_backend_id"
        )
        factories.ResourceFactory(
            offering=self.offering,
            backend_id="AB-000001",
            state=ResourceStates.OK,
        )

    def test_use_offering_rules_valid_and_unique(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, {"backend_id": "CD-999999", "use_offering_rules": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])
        self.assertTrue(response.data["is_valid_format"])
        self.assertEqual(response.data["errors"], [])

    def test_use_offering_rules_invalid_format(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, {"backend_id": "bad-format", "use_offering_rules": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_valid_format"])
        self.assertGreater(len(response.data["errors"]), 0)

    def test_use_offering_rules_not_unique(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, {"backend_id": "AB-000001", "use_offering_rules": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])
        self.assertTrue(response.data["is_valid_format"])

    def test_use_offering_rules_invalid_format_and_not_unique(self):
        # Create a resource with a specific backend_id that doesn't match format
        factories.ResourceFactory(
            offering=self.offering,
            backend_id="bad",
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, {"backend_id": "bad", "use_offering_rules": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])
        self.assertFalse(response.data["is_valid_format"])

    def test_without_use_offering_rules_returns_original_response(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, {"backend_id": "bad-format", "use_offering_rules": False}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Original behavior: only is_unique field
        self.assertIn("is_unique", response.data)
        self.assertTrue(response.data["is_unique"])

    def test_no_format_rules_returns_null_is_valid_format(self):
        self.offering.backend_id_rules = {"uniqueness": {"scope": "offering"}}
        self.offering.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.url, {"backend_id": "anything", "use_offering_rules": True}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["is_valid_format"])
        self.assertTrue(response.data["is_unique"])

    def test_use_offering_rules_falls_back_to_check_all_offerings_when_no_scope(self):
        """When use_offering_rules=True but no uniqueness scope is configured,
        check_all_offerings should still control the uniqueness check."""
        self.offering.backend_id_rules = {
            "format": {
                "regex": r"^[A-Z]{2}-\d{6}$",
                "description": "Must be 2 uppercase letters, dash, 6 digits",
            }
            # No uniqueness scope configured
        }
        self.offering.save()

        # Create resource in another offering of the same customer
        other_offering = factories.OfferingFactory(customer=self.fixture.customer)
        factories.ResourceFactory(offering=other_offering, backend_id="XY-123456")

        self.client.force_authenticate(self.fixture.staff)

        # check_all_offerings=False: unique within this offering
        response = self.client.post(
            self.url,
            {
                "backend_id": "XY-123456",
                "use_offering_rules": True,
                "check_all_offerings": False,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_unique"])
        self.assertTrue(response.data["is_valid_format"])

        # check_all_offerings=True: not unique across customer
        response = self.client.post(
            self.url,
            {
                "backend_id": "XY-123456",
                "use_offering_rules": True,
                "check_all_offerings": True,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_unique"])
        self.assertTrue(response.data["is_valid_format"])


class BackendIdRulesNotInPublicAPITest(test.APITestCase):
    def test_backend_id_rules_not_exposed_in_public_offering(self):
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(
            customer=fixture.customer,
            state=OfferingStates.ACTIVE,
            shared=True,
            backend_id_rules={"format": {"regex": r"^[A-Z]+$"}},
        )
        self.client.force_authenticate(fixture.staff)
        url = factories.OfferingFactory.get_public_url(offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("backend_id_rules", response.data)

    def test_backend_id_rules_exposed_in_provider_offering(self):
        fixture = fixtures.ProjectFixture()
        offering = factories.OfferingFactory(
            customer=fixture.customer,
            state=OfferingStates.ACTIVE,
            backend_id_rules={"format": {"regex": r"^[A-Z]+$"}},
        )
        self.client.force_authenticate(fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("backend_id_rules", response.data)
        self.assertEqual(
            response.data["backend_id_rules"]["format"]["regex"], r"^[A-Z]+$"
        )


class OfferingBillingTypeClassificationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_offering_with_no_components_returns_mixed(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["billing_type_classification"], "mixed")

    def test_offering_with_only_limit_components_returns_limit_only(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )

        # Add limit-based components
        factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
        )
        factories.OfferingComponentFactory(
            offering=offering,
            type="ram",
            billing_type=BillingTypes.LIMIT,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["billing_type_classification"], "limit_only")

    def test_offering_with_only_usage_components_returns_usage_only(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )

        # Add usage-based components
        factories.OfferingComponentFactory(
            offering=offering,
            type="storage",
            billing_type=BillingTypes.USAGE,
        )
        factories.OfferingComponentFactory(
            offering=offering,
            type="network_traffic",
            billing_type=BillingTypes.USAGE,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["billing_type_classification"], "usage_only")

    def test_offering_with_mixed_components_returns_mixed(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )

        # Add mixed components
        factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
        )
        factories.OfferingComponentFactory(
            offering=offering,
            type="storage",
            billing_type=BillingTypes.USAGE,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["billing_type_classification"], "mixed")

    def test_offering_with_fixed_components_returns_mixed(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )

        # Add fixed-price components
        factories.OfferingComponentFactory(
            offering=offering,
            type="monthly_fee",
            billing_type=BillingTypes.FIXED,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_url(offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["billing_type_classification"], "mixed")

    def test_billing_type_classification_in_public_offering_endpoint(self):
        offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
            shared=True,
        )

        factories.OfferingComponentFactory(
            offering=offering,
            type="cpu",
            billing_type=BillingTypes.LIMIT,
        )

        url = factories.OfferingFactory.get_public_url(offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["billing_type_classification"], "limit_only")

    def test_billing_type_classification_in_offering_list(self):
        # Create offerings with different billing types
        offering_limit = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        factories.OfferingComponentFactory(
            offering=offering_limit,
            billing_type=BillingTypes.LIMIT,
        )

        offering_usage = factories.OfferingFactory(
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        factories.OfferingComponentFactory(
            offering=offering_usage,
            billing_type=BillingTypes.USAGE,
        )

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offerings = response.data

        # Find the offerings and verify their classifications
        limit_offering = next(
            o for o in offerings if o["uuid"] == str(offering_limit.uuid)
        )
        usage_offering = next(
            o for o in offerings if o["uuid"] == str(offering_usage.uuid)
        )

        self.assertEqual(limit_offering["billing_type_classification"], "limit_only")
        self.assertEqual(usage_offering["billing_type_classification"], "usage_only")


class RestrictedOfferingVisibilityModeTest(test.APITestCase):
    """Tests for RESTRICTED_OFFERING_VISIBILITY_MODE setting."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        # Create a shared offering with a public plan
        self.shared_offering = factories.OfferingFactory(
            shared=True,
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        self.public_plan = factories.PlanFactory(
            offering=self.shared_offering,
            archived=False,
        )

        # Create a shared offering with a restricted plan (org group)
        self.org_group = structure_factories.OrganizationGroupFactory()
        self.restricted_offering = factories.OfferingFactory(
            shared=True,
            customer=self.fixture.customer,
            state=OfferingStates.ACTIVE,
        )
        self.restricted_plan = factories.PlanFactory(
            offering=self.restricted_offering,
            archived=False,
        )
        self.restricted_plan.organization_groups.add(self.org_group)

        # Create an unrelated user without memberships
        self.unrelated_user = UserFactory()

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="show_all")
    def test_show_all_mode_shows_all_shared_offerings(self):
        """In show_all mode, regular users see all shared offerings."""
        self.client.force_authenticate(self.unrelated_user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="show_restricted_disabled")
    def test_show_restricted_disabled_mode_shows_all_with_is_accessible(self):
        """In show_restricted_disabled mode, all offerings shown with is_accessible field."""
        self.client.force_authenticate(self.unrelated_user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="show_restricted_disabled")
    def test_is_accessible_true_for_accessible_offering(self):
        """is_accessible should be True for offering with public plans."""
        self.client.force_authenticate(self.unrelated_user)
        url = factories.OfferingFactory.get_public_url(self.shared_offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_accessible"])

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="show_restricted_disabled")
    def test_is_accessible_false_for_restricted_offering(self):
        """is_accessible should be False for offering with only restricted plans."""
        self.client.force_authenticate(self.unrelated_user)
        url = factories.OfferingFactory.get_public_url(self.restricted_offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.data["is_accessible"])

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="hide_inaccessible")
    def test_hide_inaccessible_mode_hides_restricted_offerings(self):
        """In hide_inaccessible mode, offerings without accessible plans are hidden."""
        self.client.force_authenticate(self.unrelated_user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertNotIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="hide_inaccessible")
    def test_hide_inaccessible_mode_shows_offerings_when_user_in_org_group(self):
        """In hide_inaccessible mode, user in org group sees restricted offerings."""
        # Add user's customer to the org group
        self.fixture.customer.organization_groups.add(self.org_group)

        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="require_membership")
    def test_require_membership_mode_shows_nothing_for_user_without_membership(self):
        """In require_membership mode, user without membership sees no offerings."""
        self.client.force_authenticate(self.unrelated_user)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="require_membership")
    def test_require_membership_mode_shows_offerings_for_user_with_membership(self):
        """In require_membership mode, user with membership sees accessible offerings."""
        self.client.force_authenticate(self.fixture.owner)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        # Owner should see public offering, but not restricted (not in org group)
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertNotIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="hide_inaccessible")
    def test_staff_always_sees_all_offerings(self):
        """Staff should always see all offerings regardless of visibility mode."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="require_membership")
    def test_staff_sees_all_even_in_require_membership_mode(self):
        """Staff should see all offerings even in require_membership mode."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="hide_inaccessible")
    def test_support_always_sees_all_offerings(self):
        """Support user should always see all offerings regardless of visibility mode."""
        self.client.force_authenticate(self.fixture.global_support)
        url = factories.OfferingFactory.get_public_list_url()
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        offering_uuids = [o["uuid"] for o in response.data]
        self.assertIn(str(self.shared_offering.uuid), offering_uuids)
        self.assertIn(str(self.restricted_offering.uuid), offering_uuids)

    @override_config(RESTRICTED_OFFERING_VISIBILITY_MODE="show_all")
    def test_is_accessible_true_for_staff(self):
        """Staff should always have is_accessible=True."""
        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingFactory.get_public_url(self.restricted_offering)
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(response.data["is_accessible"])
