from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace.tests import factories, fixtures

# Schemes BackendURLValidator allows, and values it must refuse. ftp/ftps are
# valid to Django's stock URLValidator, so accepting them would mean the field
# is not using BackendURLValidator.
VALID_BACKEND_URLS = (
    "http://keystone.example.com:5000/v3",
    "http://controller:5000/v3",
    "http://litellm:4000/v1",
    "https://example.com:5000/v3",
    "ssh://user@bastion.example.com:22",
    "ssh://login01",
    "ldap://directory.example.com",
    "ldaps://directory.example.com",
    "rdp://desktop.example.com",
)
INVALID_BACKEND_URLS = (
    "not a url",
    "localhost",
    "example.com",
    "ftp://files.example.com",
    "ftps://files.example.com",
    "http://keystone_1.example.com",
    "http://999.999.999.999",
    "http://256.0.0.1:5000",
    "http://1.2.3",
    "http://1234",
)

SERIALIZER_CASES = (
    (
        marketplace_serializers.RancherSecretOptionsSerializer,
        "backend_url",
        {},
    ),
    (
        marketplace_serializers.MergedSecretOptionsSerializer,
        "backend_url",
        {},
    ),
    (
        marketplace_serializers.NestedEndpointSerializer,
        "url",
        {"name": "API"},
    ),
    (
        marketplace_serializers.ResourceEndpointSerializer,
        "url",
        {"name": "API"},
    ),
)


class BackendURLSerializerValidationTest(test.APITestCase):
    def test_optional_backend_url_may_be_omitted(self):
        for serializer_class in (
            marketplace_serializers.RancherSecretOptionsSerializer,
            marketplace_serializers.MergedSecretOptionsSerializer,
        ):
            with self.subTest(serializer=serializer_class.__name__):
                serializer = serializer_class(data={})
                self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_each_serializer_accepts_supported_backend_urls(self):
        for serializer_class, field, base in SERIALIZER_CASES:
            for value in VALID_BACKEND_URLS:
                with self.subTest(serializer=serializer_class.__name__, value=value):
                    serializer = serializer_class(data={**base, field: value})
                    self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_endpoint_url_longer_than_the_column_is_rejected(self):
        # OfferingAccessEndpoint.url and ResourceAccessEndpoint.url are
        # varchar(200). Without max_length the serializer accepts the value
        # and the insert fails with a database error.
        url = "https://example.com/" + ("a" * 200)
        for serializer_class in (
            marketplace_serializers.NestedEndpointSerializer,
            marketplace_serializers.ResourceEndpointSerializer,
        ):
            with self.subTest(serializer=serializer_class.__name__):
                serializer = serializer_class(data={"name": "API", "url": url})
                self.assertFalse(serializer.is_valid())
                self.assertIn("200", str(serializer.errors["url"]))

    def test_each_serializer_rejects_malformed_backend_urls(self):
        for serializer_class, field, base in SERIALIZER_CASES:
            for value in INVALID_BACKEND_URLS:
                with self.subTest(serializer=serializer_class.__name__, value=value):
                    serializer = serializer_class(data={**base, field: value})
                    self.assertFalse(serializer.is_valid())
                    self.assertIn("Enter a valid URL", str(serializer.errors[field]))

    def test_resource_endpoint_list_rejects_one_bad_url_among_valid_ones(self):
        serializer = marketplace_serializers.ResourceEndpointsSerializer(
            data={
                "endpoints": [
                    {"name": "API", "url": "https://example.com/v1"},
                    {"name": "Shell", "url": "not a url"},
                ]
            }
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("Enter a valid URL", str(serializer.errors))

    def test_resource_endpoint_list_accepts_supported_schemes(self):
        serializer = marketplace_serializers.ResourceEndpointsSerializer(
            data={
                "endpoints": [
                    {"name": name, "url": url}
                    for name, url in (
                        ("HTTPS", "https://example.com/v1"),
                        ("SSH", "ssh://bastion.example.com"),
                        ("LDAP", "ldaps://directory.example.com"),
                        ("RDP", "rdp://desktop.example.com"),
                    )
                ]
            }
        )
        self.assertTrue(serializer.is_valid(), serializer.errors)


class OfferingEndpointURLValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        CustomerRole.OWNER.add_permission(PermissionEnum.ADD_OFFERING_ENDPOINT)
        self.url = factories.OfferingFactory.get_url(self.offering, "add_endpoint")
        self.fixture.service_provider.allowed_domains = []
        self.fixture.service_provider.save()

    def _add(self, endpoint_url):
        self.client.force_authenticate(self.fixture.service_owner)
        return self.client.post(
            self.url,
            {"name": "Test Endpoint", "url": endpoint_url},
            format="json",
        )

    def test_supported_schemes_are_stored(self):
        for endpoint_url in (
            "https://api.example.com/scim",
            "http://litellm:4000/v1",
            "ssh://bastion.example.com",
            "ldaps://directory.example.com",
        ):
            with self.subTest(endpoint_url=endpoint_url):
                response = self._add(endpoint_url)
                self.assertEqual(
                    response.status_code, status.HTTP_201_CREATED, response.data
                )
        self.assertEqual(
            set(self.offering.endpoints.values_list("url", flat=True)),
            {
                "https://api.example.com/scim",
                "http://litellm:4000/v1",
                "ssh://bastion.example.com",
                "ldaps://directory.example.com",
            },
        )

    def test_malformed_url_is_rejected_and_not_stored(self):
        for endpoint_url in INVALID_BACKEND_URLS:
            with self.subTest(endpoint_url=endpoint_url):
                response = self._add(endpoint_url)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("Enter a valid URL", str(response.data))
        self.assertEqual(self.offering.endpoints.count(), 0)


class ResourceEndpointURLValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.resource = self.fixture.resource
        self.url = factories.ResourceFactory.get_provider_resource_url(
            self.resource, action="set_endpoints"
        )
        models.ResourceAccessEndpoint.objects.create(
            resource=self.resource,
            name="existing",
            url="https://existing.example.com",
        )

    def _set(self, endpoints):
        self.client.force_authenticate(self.fixture.staff)
        return self.client.post(self.url, {"endpoints": endpoints}, format="json")

    def test_supported_schemes_replace_the_previous_set(self):
        response = self._set(
            [
                {"name": "API", "url": "https://example.com/v1"},
                {"name": "Inference", "url": "http://litellm:4000/v1"},
                {"name": "Shell", "url": "ssh://bastion.example.com"},
                {"name": "Desktop", "url": "rdp://desktop.example.com"},
            ]
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            {endpoint.name: endpoint.url for endpoint in self.resource.endpoints.all()},
            {
                "API": "https://example.com/v1",
                "Inference": "http://litellm:4000/v1",
                "Shell": "ssh://bastion.example.com",
                "Desktop": "rdp://desktop.example.com",
            },
        )

    def test_malformed_url_is_rejected_and_leaves_existing_endpoints(self):
        for endpoint_url in INVALID_BACKEND_URLS:
            with self.subTest(endpoint_url=endpoint_url):
                response = self._set(
                    [
                        {"name": "API", "url": "https://example.com/v1"},
                        {"name": "Bad", "url": endpoint_url},
                    ]
                )
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )
                self.assertIn("Enter a valid URL", str(response.data))
                self.assertEqual(
                    set(self.resource.endpoints.values_list("name", flat=True)),
                    {"existing"},
                )
