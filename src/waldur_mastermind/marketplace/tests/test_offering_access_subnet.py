from ddt import data, ddt
from django.core.management import call_command
from django.urls import reverse
from rest_framework import test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import fixtures


def _enable(offering):
    offering.plugin_options = {"enable_resource_access_subnets": True}
    offering.save()


def _offering_url(offering):
    return reverse(
        "marketplace-provider-offering-detail", kwargs={"uuid": offering.uuid.hex}
    )


def _grant_provider_permissions():
    for role in (OfferingRole.MANAGER, CustomerRole.OWNER):
        role.add_permission(PermissionEnum.CREATE_OFFERING_ACCESS_SUBNET)
        role.add_permission(PermissionEnum.UPDATE_OFFERING_ACCESS_SUBNET)
        role.add_permission(PermissionEnum.DELETE_OFFERING_ACCESS_SUBNET)


class _CommandCapture:
    def __init__(self):
        self.value = ""

    def write(self, msg, *args, **kwargs):
        self.value += str(msg) + "\n"


@ddt
class OfferingAccessSubnetCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable(self.fixture.offering)
        _grant_provider_permissions()

    def create(self, user, inet="10.0.0.0/24"):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(
            reverse("marketplace-offering-access-subnet-list"),
            {
                "offering": _offering_url(self.fixture.offering),
                "inet": inet,
                "description": "corp network",
            },
        )

    @data("staff", "offering_owner", "offering_manager")
    def test_provider_can_create(self, user):
        self.assertEqual(self.create(user).status_code, 201)

    @data("owner", "admin", "manager", "member")
    def test_consumer_cannot_create(self, user):
        self.assertEqual(self.create(user).status_code, 403)

    def test_any_cidr_width_accepted(self):
        response = self.create("offering_owner", inet="192.168.0.0/16")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["inet"], "192.168.0.0/16")

    def test_host_bits_rejected(self):
        response = self.create("offering_owner", inet="10.0.0.5/24")
        self.assertEqual(response.status_code, 400, response.data)

    def test_rejected_when_feature_disabled(self):
        self.fixture.offering.plugin_options = {}
        self.fixture.offering.save()
        self.assertEqual(self.create("offering_owner").status_code, 400)


@ddt
class OfferingAccessSubnetManageTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable(self.fixture.offering)
        _grant_provider_permissions()
        self.subnet = models.OfferingAccessSubnet.objects.create(
            offering=self.fixture.offering, inet="10.0.0.0/24"
        )

    def _detail(self):
        return reverse(
            "marketplace-offering-access-subnet-detail",
            kwargs={"uuid": self.subnet.uuid.hex},
        )

    @data("staff", "offering_owner")
    def test_provider_can_update(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self._detail(), {"description": "updated"})
        self.assertEqual(response.status_code, 200, response.data)

    @data("staff", "offering_owner")
    def test_provider_can_delete(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        self.assertEqual(self.client.delete(self._detail()).status_code, 204)

    def test_outsider_cannot_see(self):
        self.client.force_authenticate(self.fixture.owner)
        self.assertEqual(self.client.get(self._detail()).status_code, 404)


class OfferingDefaultSubnetsInlineTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable(self.fixture.offering)
        models.OfferingAccessSubnet.objects.create(
            offering=self.fixture.offering, inet="10.0.0.0/24", description="corp"
        )

    def test_consumer_sees_defaults_inline_on_offering(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(
            reverse(
                "marketplace-public-offering-detail",
                kwargs={"uuid": self.fixture.offering.uuid.hex},
            )
        )
        self.assertEqual(response.status_code, 200, response.data)
        inets = [s["inet"] for s in response.data["default_access_subnets"]]
        self.assertIn("10.0.0.0/24", inets)


class OfferingDefaultSubnetsExportTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable(self.fixture.offering)
        models.OfferingAccessSubnet.objects.create(
            offering=self.fixture.offering, inet="203.0.113.0/30"
        )
        models.ResourceAccessSubnet.objects.create(
            resource=self.fixture.resource, inet="192.168.1.5/32"
        )

    def test_aggregation_packed_includes_defaults(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            reverse(
                "marketplace-provider-offering-access-subnets",
                kwargs={"uuid": self.fixture.offering.uuid.hex},
            )
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("203.0.113.0/30", response.data["defaults"])
        self.assertIn("203.0.113.0/30", response.data["packed"])
        self.assertIn("192.168.1.5/32", response.data["packed"])

    def test_command_dump_includes_defaults(self):
        out = _CommandCapture()
        call_command(
            "resource_access_subnets",
            offering=self.fixture.offering.uuid.hex,
            stdout=out,
        )
        self.assertIn("203.0.113.0/30", out.value)
