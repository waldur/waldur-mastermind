from ddt import data, ddt
from django.core.management import call_command
from django.urls import reverse
from rest_framework import test

from waldur_core.logging import models as logging_models
from waldur_core.logging.enums import EventType
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories, fixtures


def _list_url():
    return reverse("marketplace-resource-access-subnet-list")


def _detail_url(subnet):
    return reverse(
        "marketplace-resource-access-subnet-detail", kwargs={"uuid": subnet.uuid.hex}
    )


def _enable_subnets(offering):
    offering.plugin_options = {"enable_resource_access_subnets": True}
    offering.save()


def _grant_consumer_permissions():
    CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_RESOURCE_ACCESS_SUBNET)
    CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_ACCESS_SUBNET)
    CustomerRole.OWNER.add_permission(PermissionEnum.DELETE_RESOURCE_ACCESS_SUBNET)
    ProjectRole.ADMIN.add_permission(PermissionEnum.CREATE_RESOURCE_ACCESS_SUBNET)
    ProjectRole.ADMIN.add_permission(PermissionEnum.UPDATE_RESOURCE_ACCESS_SUBNET)
    ProjectRole.ADMIN.add_permission(PermissionEnum.DELETE_RESOURCE_ACCESS_SUBNET)
    ProjectRole.MANAGER.add_permission(PermissionEnum.CREATE_RESOURCE_ACCESS_SUBNET)
    ProjectRole.MANAGER.add_permission(PermissionEnum.UPDATE_RESOURCE_ACCESS_SUBNET)
    ProjectRole.MANAGER.add_permission(PermissionEnum.DELETE_RESOURCE_ACCESS_SUBNET)


@ddt
class ResourceAccessSubnetCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        _grant_consumer_permissions()

    def create(self, user, inet="192.168.1.5/32"):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.post(
            _list_url(),
            {
                "resource": factories.ResourceFactory.get_url(self.fixture.resource),
                "inet": inet,
                "description": "Test subnet",
            },
        )

    @data("staff", "owner", "admin", "manager")
    def test_consumer_can_create_access_subnet(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, 201, response.data)

    @data("member", "user")
    def test_unprivileged_user_cannot_create_access_subnet(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, 403, response.data)

    @data("offering_owner", "service_manager")
    def test_provider_cannot_create_access_subnet(self, user):
        response = self.create(user)
        self.assertEqual(response.status_code, 403, response.data)

    def test_cannot_create_when_offering_flag_disabled(self):
        self.fixture.offering.plugin_options = {}
        self.fixture.offering.save()
        response = self.create("owner")
        self.assertEqual(response.status_code, 400, response.data)

    def test_bare_ip_is_accepted_as_single_host(self):
        response = self.create("owner", inet="192.168.1.5")
        self.assertEqual(response.status_code, 201, response.data)

    def test_non_single_host_cidr_is_rejected(self):
        response = self.create("owner", inet="192.168.1.0/24")
        self.assertEqual(response.status_code, 400, response.data)


@ddt
class ResourceAccessSubnetManageTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        _grant_consumer_permissions()
        self.subnet = models.ResourceAccessSubnet.objects.create(
            resource=self.fixture.resource, inet="192.168.1.5/32"
        )

    @data("staff", "owner")
    def test_consumer_can_update_access_subnet(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(
            _detail_url(self.subnet), {"description": "updated"}
        )
        self.assertEqual(response.status_code, 200, response.data)

    @data("staff", "owner")
    def test_consumer_can_delete_access_subnet(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(_detail_url(self.subnet))
        self.assertEqual(response.status_code, 204, response.data)

    def test_outsider_cannot_see_access_subnet(self):
        # Filtered out of the queryset -> 404 on detail, empty list.
        self.client.force_authenticate(self.fixture.user)
        self.assertEqual(self.client.get(_detail_url(self.subnet)).status_code, 404)
        self.assertEqual(len(self.client.get(_list_url()).data), 0)

    @data("staff", "owner")
    def test_consumer_can_list_access_subnet(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(_list_url())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(
            response.data[0]["resource_backend_id"], self.fixture.resource.backend_id
        )

    def test_filter_by_offering_uuid(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            _list_url(), {"offering_uuid": self.fixture.offering.uuid.hex}
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 1)


class ResourceAccessSubnetCommandTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        models.ResourceAccessSubnet.objects.create(
            resource=self.fixture.resource, inet="192.168.1.5/32"
        )
        models.ResourceAccessSubnet.objects.create(
            resource=self.fixture.resource, inet="192.168.1.6/32"
        )

    def test_command_dumps_subnets(self):
        out = _CommandCapture()
        call_command("resource_access_subnets", stdout=out)
        self.assertIn("192.168.1.5/32", out.value)
        self.assertIn("192.168.1.6/32", out.value)

    def test_command_filters_by_offering(self):
        out = _CommandCapture()
        call_command(
            "resource_access_subnets",
            offering=self.fixture.offering.uuid.hex,
            stdout=out,
        )
        self.assertIn("192.168.1.5/32", out.value)


class ResourceAccessSubnetAuditLogTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        self.subnet = models.ResourceAccessSubnet.objects.create(
            resource=self.fixture.resource,
            inet="192.168.1.5/32",
            description="asd",
        )
        # Reload so inet becomes an IPNetwork object, reproducing the
        # string-vs-object mismatch the serializer triggers on update.
        self.subnet.refresh_from_db()

    def _update_events(self):
        return logging_models.Event.objects.filter(
            event_type=EventType.RESOURCE_ACCESS_SUBNET_UPDATE_SUCCEEDED.value
        )

    def test_update_logs_only_real_changes(self):
        self.subnet.inet = "192.168.1.5/32"  # same value, assigned as a string
        self.subnet.description = "bar"
        self.subnet.save()
        events = self._update_events()
        self.assertEqual(events.count(), 1)
        message = events.first().message
        self.assertIn("description", message)
        self.assertNotIn("inet has been changed", message)

    def test_no_op_save_does_not_log(self):
        self.subnet.inet = "192.168.1.5/32"  # re-assign identical value only
        self.subnet.save()
        self.assertFalse(self._update_events().exists())


@ddt
class OfferingAccessSubnetsEndpointTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        _enable_subnets(self.fixture.offering)
        # Two adjacent single hosts that collapse into one /31.
        models.ResourceAccessSubnet.objects.create(
            resource=self.fixture.resource,
            inet="192.168.1.0/32",
            description="host a",
        )
        models.ResourceAccessSubnet.objects.create(
            resource=self.fixture.resource,
            inet="192.168.1.1/32",
            description="host b",
        )

    def url(self):
        return reverse(
            "marketplace-provider-offering-access-subnets",
            kwargs={"uuid": self.fixture.offering.uuid.hex},
        )

    @data("staff", "offering_owner", "offering_manager")
    def test_provider_can_list_access_subnets(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 200, response.data)
        # Expanded: one row per subnet, with resource/project/customer context.
        self.assertEqual(len(response.data["expanded"]), 2)
        row = response.data["expanded"][0]
        self.assertEqual(row["resource_uuid"], self.fixture.resource.uuid.hex)
        self.assertEqual(
            row["customer_uuid"], self.fixture.resource.project.customer.uuid.hex
        )
        self.assertIn("project_name", row)
        # Packed: adjacent /32s collapse into a single /31.
        self.assertEqual(response.data["packed"], ["192.168.1.0/31"])

    @data("owner", "admin", "manager", "member")
    def test_consumer_cannot_list_access_subnets(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url())
        self.assertEqual(response.status_code, 403, response.data)


ALLOWED_IP = "100.100.100.100"
DENIED_IP = "203.0.113.7"


@ddt
class ResourceAccessSubnetConcealmentTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self._enable_conceal(self.fixture.offering)
        self.resource = self.fixture.resource
        models.ResourceAccessSubnet.objects.create(
            resource=self.resource, inet=f"{ALLOWED_IP}/32"
        )
        # The consumer owner needs LIST_RESOURCES to see resources at all.
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        self.consumer = self.fixture.owner

    def _enable_conceal(self, offering, value=True):
        offering.plugin_options = {
            "conceal_subnet_restricted_resources": value,
        }
        offering.save()

    def _consumer_list_uuids(self, user, ip):
        self.client.force_authenticate(user)
        response = self.client.get(
            reverse("marketplace-resource-list"), HTTP_X_FORWARDED_FOR=ip
        )
        self.assertEqual(response.status_code, 200, response.data)
        return [item["uuid"] for item in response.data]

    def test_hidden_from_consumer_when_ip_not_allowed(self):
        uuids = self._consumer_list_uuids(self.consumer, DENIED_IP)
        self.assertNotIn(self.resource.uuid.hex, uuids)
        # Detail lookup is filtered too -> 404.
        detail = self.client.get(
            factories.ResourceFactory.get_url(self.resource),
            HTTP_X_FORWARDED_FOR=DENIED_IP,
        )
        self.assertEqual(detail.status_code, 404)

    def test_visible_to_consumer_when_ip_allowed(self):
        uuids = self._consumer_list_uuids(self.consumer, ALLOWED_IP)
        self.assertIn(self.resource.uuid.hex, uuids)

    def test_visible_when_concealment_disabled(self):
        self._enable_conceal(self.fixture.offering, value=False)
        uuids = self._consumer_list_uuids(self.consumer, DENIED_IP)
        self.assertIn(self.resource.uuid.hex, uuids)

    def test_visible_when_resource_has_no_subnets(self):
        self.resource.access_subnet_set.all().delete()
        uuids = self._consumer_list_uuids(self.consumer, DENIED_IP)
        self.assertIn(self.resource.uuid.hex, uuids)

    @data("staff", "global_support")
    def test_staff_and_support_bypass(self, user):
        uuids = self._consumer_list_uuids(getattr(self.fixture, user), DENIED_IP)
        self.assertIn(self.resource.uuid.hex, uuids)

    def test_provider_api_not_concealed(self):
        # Concealment is consumer-only: the provider still sees the resource
        # from a non-allowed IP.
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.get(
            reverse("marketplace-provider-resource-list"),
            HTTP_X_FORWARDED_FOR=DENIED_IP,
        )
        self.assertEqual(response.status_code, 200, response.data)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.resource.uuid.hex, uuids)


class _CommandCapture:
    def __init__(self):
        self.value = ""

    def write(self, msg, *args, **kwargs):
        self.value += str(msg) + "\n"
