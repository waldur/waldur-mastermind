import io
import json
import uuid
from unittest import mock, skip
from uuid import uuid4

import respx
from django.core.serializers.json import DjangoJSONEncoder
from django.db import transaction
from django.test import override_settings
from rest_framework import status, test
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from waldur_api_client.models.public_offering_details import PublicOfferingDetails

from waldur_core.core.fields import StringUUID
from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.factories import UserFactory
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace.enums import REMOTE_OFFERING, OfferingStates
from waldur_mastermind.marketplace.serializers import (
    OrderCreateSerializer,
    ScreenshotSerializer,
)
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests.factories import OfferingFactory
from waldur_mastermind.marketplace_remote.processors import (
    RemoteCreateResourceProcessor,
)
from waldur_mastermind.marketplace_remote.tasks import OfferingPullTask
from waldur_mastermind.marketplace_remote.tests.dns_utils import (
    create_selective_dns_mock,
)
from waldur_mastermind.marketplace_remote.utils import (
    import_offering_image,
    import_offering_screenshots,
)


class WaldurJsonEncoder(DjangoJSONEncoder):
    def default(self, o):
        if isinstance(o, StringUUID):
            return str(o)
        return super().default(o)


def serialize_data(serializer_class, instance):
    factory = APIRequestFactory()
    request = Request(factory.get("/api/marketplace-orders/"))
    view = mock.Mock(request=request)
    serialized_order = serializer_class(
        instance,
        context={
            "view": view,
            "request": request,
        },
    ).data
    return json.loads(json.dumps(serialized_order, cls=WaldurJsonEncoder))


class RemoteCustomersTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()

    def tearDown(self):
        self.dns_patcher.stop()
        super().tearDown()

    @respx.mock
    def test_remote_customers_are_listed_for_given_token_and_api_url(self):
        mock_customer = respx.get("https://remote-waldur.com/api/customers/").respond(
            200, json=[]
        )
        self.client.force_login(UserFactory())
        response = self.client.post(
            "/api/remote-waldur-api/remote_customers/",
            {
                "api_url": "https://remote-waldur.com",
                "token": "valid_token",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data, [])
        self.assertTrue(mock_customer.called)
        self.assertEqual(
            mock_customer.calls.last.request.headers["Authorization"],
            "Token valid_token",
        )


class RemoteСategoriesTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()

    def tearDown(self):
        self.dns_patcher.stop()
        super().tearDown()

    @respx.mock
    def test_remote_сategories_are_listed_for_given_token_and_api_url(self):
        categories_mock = respx.get(
            "https://remote-waldur.com/api/marketplace-categories/"
        ).respond(200, json=[])
        self.client.force_login(UserFactory())
        response = self.client.post(
            "/api/remote-waldur-api/remote_categories/",
            {
                "api_url": "https://remote-waldur.com/",
                "token": "valid_token",
            },
        )
        self.assertEqual(
            categories_mock.calls.last.request.headers["Authorization"],
            "Token valid_token",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])


class RemoteOfferingsListTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()

    def tearDown(self):
        self.dns_patcher.stop()
        super().tearDown()

    @respx.mock
    def test_remote_offerings_are_listed_and_serialized_correctly(self):
        customer_uuid = uuid4().hex
        mock_offerings = [
            {
                "uuid": uuid4().hex,
                "name": "Test Offering",
                "type": "Test.Type",
                "state": "Active",
                "category_title": "Test Category",
            }
        ]
        offerings_mock = respx.get(
            "https://remote-waldur.com/api/marketplace-public-offerings/"
        ).respond(200, json=mock_offerings)
        self.client.force_login(UserFactory())
        response = self.client.post(
            f"/api/remote-waldur-api/shared_offerings/?customer_uuid={customer_uuid}",
            {
                "api_url": "https://remote-waldur.com/",
                "token": "valid_token",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(offerings_mock.called)
        self.assertIsInstance(response.data, list)
        if response.data:
            self.assertIsInstance(response.data[0], dict)
            self.assertIn("uuid", response.data[0])
            self.assertIn("name", response.data[0])
            self.assertIn("type", response.data[0])
            self.assertIn("state", response.data[0])
            self.assertIn("category_title", response.data[0])

    @respx.mock
    def test_remote_offerings_returns_empty_list_when_no_offerings(self):
        customer_uuid = uuid4().hex
        offerings_mock = respx.get(
            "https://remote-waldur.com/api/marketplace-public-offerings/"
        ).respond(200, json=[])
        self.client.force_login(UserFactory())
        response = self.client.post(
            f"/api/remote-waldur-api/shared_offerings/?customer_uuid={customer_uuid}",
            {
                "api_url": "https://remote-waldur.com/",
                "token": "valid_token",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
        self.assertTrue(offerings_mock.called)


class OfferingDetailsPullTest(test.APITestCase):
    def setUp(self) -> None:
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()

        fixture = fixtures.MarketplaceFixture()
        self.offering = fixture.offering
        self.plan: models.Plan = fixture.plan
        self.plan_component: models.PlanComponent = fixture.plan_component
        self.component = fixture.offering_component
        self.offering.backend_id = uuid4().hex
        self.api_url = "https://remote-waldur.com"
        self.offering.secret_options = {
            "api_url": self.api_url,
            "token": uuid4().hex,
            "customer_uuid": uuid4().hex,
        }
        self.offering.save()  # Save the offering with backend_id and secret_options

        # Clean up any existing ToS objects for this offering to ensure test isolation
        # This is important for CI environments with database reuse
        marketplace_models.OfferingTermsOfService.objects.filter(
            offering=self.offering
        ).delete()

        self.task = OfferingPullTask()
        self.remote_plan_uuid = uuid4().hex
        self.plan.backend_id = self.remote_plan_uuid
        self.plan.save()

        self.remote_offering = {
            "uuid": self.offering.backend_id,
            "name": self.offering.name,
            "description": self.offering.description,
            "full_description": self.offering.full_description,
            "privacy_policy_link": self.offering.privacy_policy_link,
            "terms_of_service": "Remote Terms of Service",
            "terms_of_service_link": "https://example.com/tos",
            "country": self.offering.country,
            "getting_started": self.offering.getting_started,
            "integration_guide": self.offering.integration_guide,
            "options": self.offering.options,
            "resource_options": {},
            "thumbnail": None,
            "rating": None,
            "attributes": {},
            "geolocations": "[]",
            "plugin_options": {},
            "secret_options": {},
            "state": "Active",
            "vendor_details": "",
            "type": self.offering.type,
            "shared": True,
            "billable": True,
            "components": [
                {
                    "name": self.component.name,
                    "type": self.component.type,
                    "description": self.component.description,
                    "article_code": self.component.article_code,
                    "measured_unit": self.component.measured_unit,
                    "billing_type": self.component.billing_type,
                    "min_value": self.component.min_value,
                    "max_value": self.component.max_value,
                    "is_boolean": self.component.is_boolean,
                    "default_limit": self.component.default_limit,
                    "limit_period": self.component.limit_period,
                    "limit_amount": self.component.limit_amount,
                }
            ],
            "plans": [
                {
                    "uuid": self.remote_plan_uuid,
                    "name": self.plan.name,
                    "description": self.plan.description,
                    "article_code": self.plan.article_code,
                    "prices": {self.component.type: float(self.plan_component.price)},
                    "quotas": {self.component.type: self.plan_component.amount},
                    "max_amount": self.plan.max_amount,
                    "archived": False,
                    "is_active": True,
                    "unit_price": self.plan.unit_price,
                    "unit": self.plan.unit,
                }
            ],
            "endpoints": [
                {
                    "uuid": "9f33bb7cbb714271851f138942be578b",
                    "name": "New Endpoint",
                    "url": "https://new-endpoint.example.com/",
                },
                {
                    "uuid": "9f33bb7cbb714271851f138942be578b",
                    "name": "Updated existing Endpoint",
                    "url": "https://existing-endpoint.example.com/",
                },
            ],
            "access_url": "http://test-access-url.example.com/",
        }
        respx.start()

    def tearDown(self) -> None:
        self.dns_patcher.stop()
        respx.stop()
        return super().tearDown()

    def mock_offering_details(self, remote_offering, tos_response=None):
        respx.get(
            f"{self.api_url}/api/marketplace-public-offerings/{self.offering.backend_id}/"
        ).respond(200, json=remote_offering)
        # Mock the terms of service endpoint that the new sync code calls
        if tos_response is None:
            tos_response = []
        respx.get(f"{self.api_url}/api/marketplace-offering-terms-of-service/").respond(
            200, json=tos_response
        )

    @override_settings(task_always_eager=True)
    def test_update_component(self):
        new_billing_type = "usage"
        self.remote_offering["components"][0]["billing_type"] = new_billing_type
        self.mock_offering_details(self.remote_offering)
        self.task.pull(self.offering)
        self.component.refresh_from_db()
        self.assertEqual(new_billing_type, self.component.billing_type)
        self.assertEqual(1, self.offering.components.count())

    @override_settings(task_always_eager=True)
    def test_plugin_options_are_pulled(self):
        self.remote_offering["plugin_options"] = {
            "service_provider_can_create_offering_user": True,
            "username_generation_policy": "waldur_username",
        }
        self.mock_offering_details(self.remote_offering)
        self.task.pull(self.offering)
        self.offering.refresh_from_db()
        self.assertEqual(
            self.remote_offering["plugin_options"], self.offering.plugin_options
        )

    @override_settings(task_always_eager=True)
    def test_pull_is_skipped_when_backend_id_is_empty(self):
        # An unlinked remote offering has an empty backend_id. Pulling it would
        # build a request to /api/marketplace-public-offerings// which collapses
        # to the list endpoint and returns a JSON array the SDK cannot parse.
        self.offering.backend_id = ""
        self.offering.save()
        route = respx.get(f"{self.api_url}/api/marketplace-public-offerings//").respond(
            200, json=[self.remote_offering]
        )

        self.task.pull(self.offering)

        self.assertFalse(route.called)

    @override_settings(task_always_eager=True)
    def test_stale_and_new_components(self):
        new_type = "gpu"
        self.remote_offering["components"][0]["type"] = new_type
        self.remote_offering["plans"][0]["prices"] = {
            new_type: float(self.plan_component.price)
        }
        self.remote_offering["plans"][0]["quotas"] = {
            new_type: self.plan_component.amount
        }
        self.mock_offering_details(self.remote_offering)

        self.task.pull(self.offering)

        self.assertEqual(1, self.offering.components.count())
        new_component = self.offering.components.first()
        self.assertEqual(new_type, new_component.type)
        self.assertEqual(
            0, models.OfferingComponent.objects.filter(type=self.component.type).count()
        )
        self.plan.refresh_from_db()
        self.assertEqual(
            0, models.PlanComponent.objects.filter(pk=self.plan_component.pk).count()
        )
        self.assertEqual(
            1, self.plan.components.filter(component=new_component).count()
        )

    @skip("Unstable in CI/CD")
    @override_settings(task_always_eager=True)
    def test_update_plan(self):
        new_plan_name = "New plan"
        plan_component_new_price = 50.0
        new_plan_component_price = 100.0
        new_plan_component_amount = 1000
        new_component_type = "additional"
        new_component_data = self.remote_offering["components"][0].copy()
        new_component_data["type"] = new_component_type

        self.remote_offering["plans"][0]["name"] = new_plan_name
        self.remote_offering["plans"][0]["prices"][self.component.type] = (
            plan_component_new_price
        )

        self.remote_offering["components"].append(new_component_data)
        self.remote_offering["plans"][0]["prices"][new_component_type] = (
            new_plan_component_price
        )
        self.remote_offering["plans"][0]["quotas"][new_component_type] = (
            new_plan_component_amount
        )

        self.mock_offering_details(self.remote_offering)

        self.task.pull(self.offering)

        self.offering.refresh_from_db()
        self.assertEqual(1, self.offering.plans.count())

        self.plan.refresh_from_db()
        self.assertEqual(new_plan_name, self.plan.name)
        self.assertEqual(2, self.plan.components.count())
        self.assertEqual(self.remote_plan_uuid, self.plan.backend_id)

        self.plan_component.refresh_from_db()
        self.assertEqual(plan_component_new_price, self.plan_component.price)

        new_plan_component = self.plan.components.all()[1]
        self.assertEqual(new_component_type, new_plan_component.component.type)
        self.assertEqual(new_plan_component_price, new_plan_component.price)
        self.assertEqual(new_plan_component_amount, new_plan_component.amount)

    @override_settings(task_always_eager=True)
    def test_stale_and_new_plan(self):
        new_plan_uuid = uuid4().hex
        remote_plan = self.remote_offering["plans"][0]
        remote_plan["uuid"] = new_plan_uuid
        self.mock_offering_details(self.remote_offering)

        self.task.pull(self.offering)

        self.assertEqual(1, models.Plan.objects.filter(pk=self.plan.pk).count())

        self.offering.refresh_from_db()

        self.assertEqual(2, self.offering.plans.count())
        old_plan = self.offering.plans.get(backend_id=self.remote_plan_uuid)
        self.assertTrue(old_plan.archived)

        new_plan = self.offering.plans.get(backend_id=new_plan_uuid)

        self.assertEqual(1, new_plan.components.count())

        new_plan_component = new_plan.components.first()
        self.assertEqual(self.component, new_plan_component.component)

    @override_settings(task_always_eager=True)
    def test_endpoints_update(self):
        marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=self.offering,
            name="Stale Endpoint",
            url="https://stale-endpoint.example.com/",
        )

        existing_endpoint = marketplace_models.OfferingAccessEndpoint.objects.create(
            offering=self.offering,
            name="Existing endpoint",
            url="https://existing-endpoint.example.com/",
        )

        self.mock_offering_details(self.remote_offering)

        self.task.pull(self.offering)
        existing_endpoint.refresh_from_db()
        endpoints = self.offering.endpoints.all()
        self.assertEqual(2, len(endpoints))

        self.assertEqual("Updated existing Endpoint", existing_endpoint.name)

        new_endpoint = endpoints.filter(name="New Endpoint").first()
        self.assertIsNotNone(new_endpoint)
        self.assertEqual("https://new-endpoint.example.com/", new_endpoint.url)

        self.assertIsNone(endpoints.filter(name="Stale Endpoint").first())

    @override_settings(task_always_eager=True)
    @skip("Unstable in CI/CD")
    def test_sync_terms_of_service_from_remote_offering(self):
        """Test that old-style ToS fields from remote offerings create OfferingTermsOfService records"""
        # Ensure clean state - delete any existing ToS objects
        marketplace_models.OfferingTermsOfService.objects.filter(
            offering=self.offering
        ).delete()

        # Verify clean state
        self.assertEqual(
            marketplace_models.OfferingTermsOfService.objects.filter(
                offering=self.offering
            ).count(),
            0,
        )

        # Mock the remote offering details response
        self.mock_offering_details(self.remote_offering)

        # Execute the pull task in a transaction to ensure atomicity
        with transaction.atomic():
            self.task.pull(self.offering)

        # Force a database commit and refresh
        self.offering.refresh_from_db()

        # Verify the OfferingTermsOfService object was created
        tos_objects = marketplace_models.OfferingTermsOfService.objects.filter(
            offering=self.offering
        )

        self.assertEqual(
            tos_objects.count(),
            1,
            f"Expected exactly 1 OfferingTermsOfService object for offering {self.offering.uuid.hex}, "
            f"but found {tos_objects.count()}. "
            f"All ToS objects in DB: {marketplace_models.OfferingTermsOfService.objects.count()}",
        )

        # Verify the content is correct
        tos = tos_objects.first()
        self.assertEqual(tos.terms_of_service, "Remote Terms of Service")
        self.assertEqual(tos.terms_of_service_link, "https://example.com/tos")
        self.assertEqual(tos.version, "1.0")
        self.assertTrue(tos.is_active)

    @override_settings(task_always_eager=True)
    def test_sync_terms_of_service_with_empty_fields(self):
        """Test that no OfferingTermsOfService records are created if ToS fields are empty"""
        self.remote_offering["terms_of_service"] = ""
        self.remote_offering["terms_of_service_link"] = ""

        self.mock_offering_details(self.remote_offering)

        self.task.pull(self.offering)

        self.assertEqual(
            marketplace_models.OfferingTermsOfService.objects.filter(
                offering=self.offering
            ).count(),
            0,
        )

    @override_settings(task_always_eager=True)
    def test_sync_terms_of_service_deletes_local_when_remote_empty(self):
        """Test that existing local ToS records are deleted when remote offering has no ToS"""
        marketplace_models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Local Terms of Service",
            terms_of_service_link="https://local.com/tos",
            version="1.0",
            is_active=True,
        )

        self.assertEqual(
            marketplace_models.OfferingTermsOfService.objects.filter(
                offering=self.offering
            ).count(),
            1,
        )

        self.remote_offering["terms_of_service"] = ""
        self.remote_offering["terms_of_service_link"] = ""
        self.mock_offering_details(self.remote_offering)

        self.task.pull(self.offering)

        self.assertEqual(
            marketplace_models.OfferingTermsOfService.objects.filter(
                offering=self.offering
            ).count(),
            0,
        )

    @override_settings(task_always_eager=True)
    def test_sync_terms_of_service_from_additional_properties(self):
        """Test that ToS from additional_properties creates OfferingTermsOfService records"""
        self.remote_offering["terms_of_service"] = ""
        self.remote_offering["terms_of_service_link"] = ""
        self.remote_offering["additional_properties"] = {
            "terms_of_service": "Additional Properties ToS",
            "terms_of_service_link": "https://additional.com/tos",
        }

        self.mock_offering_details(self.remote_offering)
        self.task.pull(self.offering)

        tos_objects = marketplace_models.OfferingTermsOfService.objects.filter(
            offering=self.offering
        )
        self.assertEqual(tos_objects.count(), 1)

        tos = tos_objects.first()
        self.assertEqual(tos.terms_of_service, "Additional Properties ToS")
        self.assertEqual(tos.terms_of_service_link, "https://additional.com/tos")
        self.assertEqual(tos.version, "1.0")
        self.assertTrue(tos.is_active)

    @override_settings(task_always_eager=True)
    def test_sync_terms_of_service_from_remote_endpoint(self):
        """Test that ToS from remote endpoint creates OfferingTermsOfService records"""
        # Mock remote offering with empty ToS fields
        self.remote_offering["terms_of_service"] = ""
        self.remote_offering["terms_of_service_link"] = ""
        self.remote_offering["additional_properties"] = {}

        # Mock remote ToS endpoint response
        tos_response = [
            {
                "uuid": uuid4().hex,
                "offering_uuid": self.offering.backend_id,
                "offering_name": self.offering.name,
                "terms_of_service": "Remote Endpoint ToS",
                "terms_of_service_link": "https://remote-endpoint.com/tos",
                "version": "2.0",
                "is_active": True,
                "requires_reconsent": True,
                "created": "2023-01-01T00:00:00Z",
                "modified": "2023-01-01T00:00:00Z",
                "user_consent": None,
                "has_user_consent": False,
            }
        ]

        self.mock_offering_details(self.remote_offering, tos_response)
        self.task.pull(self.offering)

        # Verify the OfferingTermsOfService object was created
        tos_objects = marketplace_models.OfferingTermsOfService.objects.filter(
            offering=self.offering
        )
        self.assertEqual(tos_objects.count(), 1)

        tos = tos_objects.first()
        self.assertEqual(tos.terms_of_service, "Remote Endpoint ToS")
        self.assertEqual(tos.terms_of_service_link, "https://remote-endpoint.com/tos")
        self.assertEqual(tos.version, "2.0")
        self.assertTrue(tos.is_active)
        self.assertTrue(tos.requires_reconsent)

    @override_settings(task_always_eager=True)
    def test_sync_terms_of_service_updates_existing_local(self):
        """Test that existing local ToS is updated when remote endpoint returns data"""
        # Create existing local ToS
        marketplace_models.OfferingTermsOfService.objects.create(
            offering=self.offering,
            terms_of_service="Old Local ToS",
            terms_of_service_link="https://old.com/tos",
            version="1.0",
            is_active=True,
        )

        self.remote_offering["terms_of_service"] = ""
        self.remote_offering["terms_of_service_link"] = ""
        self.remote_offering["additional_properties"] = {}

        tos_response = [
            {
                "uuid": uuid4().hex,
                "offering_uuid": self.offering.backend_id,
                "offering_name": self.offering.name,
                "terms_of_service": "Updated Remote ToS",
                "terms_of_service_link": "https://updated.com/tos",
                "version": "1.0",
                "is_active": False,
                "requires_reconsent": True,
                "created": "2023-01-01T00:00:00Z",
                "modified": "2023-01-01T00:00:00Z",
                "user_consent": None,
                "has_user_consent": False,
            }
        ]

        self.mock_offering_details(self.remote_offering, tos_response)
        self.task.pull(self.offering)

        tos_objects = marketplace_models.OfferingTermsOfService.objects.filter(
            offering=self.offering
        )
        self.assertEqual(tos_objects.count(), 1)

        tos = tos_objects.first()
        self.assertEqual(tos.terms_of_service, "Updated Remote ToS")
        self.assertEqual(tos.terms_of_service_link, "https://updated.com/tos")
        self.assertEqual(tos.version, "1.0")
        self.assertFalse(tos.is_active)
        self.assertTrue(tos.requires_reconsent)

    @override_settings(task_always_eager=True)
    def test_sync_terms_of_service_handles_endpoint_error(self):
        """Test that UnexpectedStatus from ToS endpoint is handled gracefully"""
        self.remote_offering["terms_of_service"] = ""
        self.remote_offering["terms_of_service_link"] = ""
        self.remote_offering["additional_properties"] = {}
        self.mock_offering_details(self.remote_offering)

        respx.get(f"{self.api_url}/api/marketplace-offering-terms-of-service/").respond(
            status_code=500, json={"error": "Internal server error"}
        )

        self.task.pull(self.offering)

        self.assertEqual(
            marketplace_models.OfferingTermsOfService.objects.filter(
                offering=self.offering
            ).count(),
            0,
        )


class OfferingUpdateTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = REMOTE_OFFERING
        self.offering.save()
        self.url = factories.OfferingFactory.get_url(self.offering, "update_overview")

    def test_edit_of_fields_that_are_being_pulled_from_remote_waldur_is_not_available(
        self,
    ):
        old_name = self.offering.name
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.url, {"name": "new_name"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.offering.refresh_from_db()
        self.assertEqual(self.offering.name, old_name)


@override_waldur_core_settings(MASTERMIND_URL="http://localhost")
class OfferingRemoteVersionTest(test.APITestCase):
    def setUp(self) -> None:
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = REMOTE_OFFERING
        self.offering.save()
        self.api_url = "http://example.com"

    @respx.mock
    def test_creating_remote_order(self):
        self.client.force_authenticate(user=self.fixture.staff)

        remote_offering = OfferingFactory(state=OfferingStates.ACTIVE)
        self.offering.secret_options = {
            "token": "0b67edfecdda37fe4b6e7d6c3e6360acb3a1f2bf",
            "api_url": self.api_url,
            "customer_uuid": remote_offering.customer.uuid.hex,
        }
        self.offering.plugin_options = {
            "service_provider_can_create_offering_user": False,
        }
        self.offering.backend_id = remote_offering.uuid.hex
        self.offering.save()

        order = marketplace_factories.OrderFactory(
            project=self.fixture.project,
            offering=self.offering,
            attributes={"name": "item_name", "description": "Description"},
            plan=self.fixture.plan,
        )

        serialized_order = serialize_data(OrderCreateSerializer, order)
        # Temproray field until the client is updated
        serialized_order["offering_terms_of_service"] = ""

        respx.get(f"{self.api_url}/api/projects/").respond(200, json=[])
        respx.post(f"{self.api_url}/api/projects/").respond(
            201, json={"uuid": uuid4().hex}
        )
        respx.get(f"{self.api_url}/api/marketplace-resources/").respond(200, json=[])
        respx.post(f"{self.api_url}/api/marketplace-orders/").respond(
            201, json=serialized_order
        )

        processor = RemoteCreateResourceProcessor(order)
        processor.process_order(self.fixture.staff)

        order.refresh_from_db()
        self.assertTrue(order.backend_id)


class OfferingCreateTest(test.APITestCase):
    def setUp(self) -> None:
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()
        mock.patch(
            "waldur_mastermind.marketplace_remote.utils.import_offering_thumbnail"
        ).start()
        mock.patch(
            "waldur_mastermind.marketplace_remote.utils.import_offering_components"
        ).start()
        mock.patch("waldur_mastermind.marketplace_remote.utils.import_plans").start()
        self.user = UserFactory()
        self.customer = structure_factories.CustomerFactory()
        self.customer.add_user(self.user, CustomerRole.OWNER)
        self.remote_offering_uuid = uuid4().hex
        self.api_url = "https://remote-waldur.com"
        self.payload = {
            "api_url": self.api_url,
            "token": uuid4().hex,
            "remote_offering_uuid": self.remote_offering_uuid,
            "remote_customer_uuid": self.customer.uuid.hex,
            "local_customer_uuid": self.customer.uuid.hex,
            "local_category_uuid": factories.CategoryFactory().uuid.hex,
        }
        self.url = "/api/remote-waldur-api/import_offering/"

    def tearDown(self):
        self.dns_patcher.stop()
        super().tearDown()
        respx.stop()

    def screenshot_update_mock_response(self):
        # Mock for remote-waldur.com
        respx.get("https://remote-waldur.com/api/marketplace-screenshots/").respond(
            json=[]
        )
        # Mock for other-remote-waldur.com
        respx.get(
            "https://other-remote-waldur.com/api/marketplace-screenshots/"
        ).respond(json=[])

    def mock_public_offering_retrieve(self, api_url, offering_uuid):
        return respx.get(
            f"{api_url}/api/marketplace-public-offerings/{offering_uuid}/"
        ).respond(
            200,
            json={
                "uuid": offering_uuid,
                "name": "Offering",
                "description": "Description",
                "full_description": "",
                "privacy_policy_link": "",
                "terms_of_service": "",
                "terms_of_service_link": "",
                "getting_started": "",
                "integration_guide": "",
                "country": "",
                "options": {},
                "resource_options": {},
                "access_url": "",
            },
        )

    def test_offering_with_incorrect_permissions(self) -> None:
        self.client.force_authenticate(self.user)
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_OFFERING_PERMISSION)

        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_offering_with_correct_permissions(self) -> None:
        self.screenshot_update_mock_response()
        public_offering_retrieve = self.mock_public_offering_retrieve(
            self.api_url, self.remote_offering_uuid
        )

        self.client.force_authenticate(self.user)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(public_offering_retrieve.called)

    def test_multiple_remote_offerings_can_be_mapped_to_single_local_category(
        self,
    ) -> None:
        self.screenshot_update_mock_response()
        self.mock_public_offering_retrieve(self.api_url, self.remote_offering_uuid)
        self.client.force_authenticate(self.user)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING)

        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                uuid=response.data["uuid"]
            ).exists()
        )
        offering = marketplace_models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.category.uuid.hex, self.payload["local_category_uuid"]
        )

        new_payload = {
            "api_url": "https://other-remote-waldur.com",
            "token": uuid4().hex,
            "remote_offering_uuid": uuid4().hex,
            "remote_customer_uuid": uuid4().hex,
            "local_customer_uuid": self.customer.uuid.hex,
            "local_category_uuid": self.payload["local_category_uuid"],
        }
        self.mock_public_offering_retrieve(
            new_payload["api_url"], new_payload["remote_offering_uuid"]
        )
        response = self.client.post(self.url, new_payload)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertTrue(
            marketplace_models.Offering.objects.filter(
                uuid=response.data["uuid"]
            ).exists()
        )
        offering = marketplace_models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering.category.uuid.hex, new_payload["local_category_uuid"])


class OfferingImageTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = REMOTE_OFFERING
        self.offering.save()

        respx.start()

        self.remote_offering_screenshot = factories.ScreenshotFactory(
            name="Screenshot 1",
            description="Description 1",
            offering=OfferingFactory(
                state=OfferingStates.ACTIVE,
                name="Test Offering",
            ),
        )
        self.new_uuid = uuid.uuid4()
        self.remote_offering_image_url = f"https://example.com/{self.new_uuid}/"
        self.remote_offering = PublicOfferingDetails(
            name="Test Offering", image=self.remote_offering_image_url
        )

    def tearDown(self):
        respx.stop()

    def test_import_offering_image_success(self):
        # Mock the HTTP request with real image content
        image_content = b"test-image-data"
        respx.get(self.remote_offering_image_url).respond(200, content=image_content)

        # Act
        import_offering_image(self.offering, self.remote_offering)

        # Verify image was saved
        self.offering.refresh_from_db()
        self.assertIsNotNone(self.offering.image)

    def test_import_offering_image_http_error(self):
        # Mock the HTTP request to fail
        respx.get(self.remote_offering_image_url).respond(404)
        # Act
        import_offering_image(self.offering, self.remote_offering)

        # Verify that no image was saved in this case
        self.offering.refresh_from_db()
        self.assertFalse(self.offering.image.name)

    def test_import_offering_image_no_image_url(self):
        # Remove image URL from the remote offering
        remote_offering_without_image = self.remote_offering
        remote_offering_without_image.image = None

        # Act
        import_offering_image(self.offering, remote_offering_without_image)

        # Verify that no image was saved in this case
        self.offering.refresh_from_db()
        self.assertFalse(self.offering.image.name)

    def test_import_offering_does_not_update_existing_image_uuid(self):
        old_filename = f"old-{self.offering.uuid.hex}"
        self.remote_offering.image = self.remote_offering_image_url

        self.offering.image.name = old_filename
        self.offering.image.save(
            name=old_filename, content=io.BytesIO(b"old-image-data")
        )
        self.offering.remote_image_uuid = self.new_uuid
        self.offering.save(update_fields=["remote_image_uuid"])
        # Act
        import_offering_image(self.offering, self.remote_offering)

        self.offering.refresh_from_db()
        self.assertIsNotNone(self.offering.image)

        self.assertEqual(self.offering.remote_image_uuid, self.new_uuid)


class OfferingScreenshotsTest(test.APITestCase):
    def setUp(self):
        self.dns_patcher = create_selective_dns_mock()
        self.dns_patcher.start()
        respx.start()
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.offering.type = REMOTE_OFFERING
        self.api_url = "https://remote-waldur.com"
        self.offering.secret_options = {
            "api_url": self.api_url,
            "token": uuid4().hex,
        }
        self.offering.save()

        # Create a remote offering dictionary with screenshots
        self.remote_offering = OfferingFactory(
            state=OfferingStates.ACTIVE,
            name="Test Offering",
        )
        self.offering.backend_id = self.remote_offering.uuid.hex
        self.offering.save()
        self.remote_offering_screenshots = [
            factories.ScreenshotFactory(
                name="Screenshot 1",
                description="Description 1",
                offering=self.offering,
            )
        ]
        self.screenshot = self.remote_offering_screenshots[0]
        self.serialized_screenshot = serialize_data(
            ScreenshotSerializer, self.screenshot
        )

    def tearDown(self):
        self.dns_patcher.stop()
        respx.stop()

    def mock_screenshots_list(self):
        """Mock the marketplace-screenshots-list endpoint"""
        return respx.get(f"{self.api_url}/api/marketplace-screenshots/").respond(
            json=[self.serialized_screenshot]
        )

    def mock_image_download(self):
        return respx.get(self.serialized_screenshot["image"]).respond(
            200,
            content=b"test-image-data",
        )

    def test_import_screenshots_success(self):
        """Test that screenshots are imported successfully"""
        # Mock the API endpoints
        mock_screenshots = self.mock_screenshots_list()

        # Mock the image downloads with real content
        image_content = b"test-image-data"
        mock_image = respx.get(self.serialized_screenshot["image"]).respond(
            200,
            content=image_content,
        )

        # Act
        import_offering_screenshots(self.offering)

        # Verify API calls
        self.assertTrue(mock_screenshots.called)
        self.assertTrue(mock_image.called)

        # Verify screenshot was created
        screenshots = marketplace_models.Screenshot.objects.filter(
            offering=self.offering
        )
        self.assertEqual(screenshots.count(), 1)
        self.assertEqual(screenshots.first().name, "Screenshot 1")
        self.assertEqual(screenshots.first().description, "Description 1")

    def test_import_screenshots_http_error(self):
        """Test that HTTP error is handled"""
        # Mock the API endpoints
        mock_screenshots = self.mock_screenshots_list()
        mock_image = respx.get(self.serialized_screenshot["image"]).respond(404)

        # Act
        import_offering_screenshots(self.offering)

        # Verify API calls
        self.assertTrue(mock_screenshots.called)
        self.assertTrue(mock_image.called)

        # Verify no screenshots were created
        self.assertFalse(
            marketplace_models.Screenshot.objects.filter(
                offering=self.offering
            ).exists()
        )

    def test_import_screenshots_delete_stale(self):
        """Test that stale screenshots are deleted"""
        self.mock_image_download()
        # Create a screenshot that won't exist in remote data
        stale_screenshot = marketplace_models.Screenshot.objects.create(
            offering=self.offering,
            name="Stale Screenshot",
            description="Will be deleted",
        )
        stale_screenshot.image.save("stale.png", io.BytesIO(b"stale-image-data"))
        stale_screenshot.save()
        self.mock_screenshots_list()
        # Act
        import_offering_screenshots(self.offering)

        # Verify the stale screenshot was deleted
        self.assertFalse(
            marketplace_models.Screenshot.objects.filter(
                uuid=stale_screenshot.uuid
            ).exists()
        )
