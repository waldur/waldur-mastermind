"""An order, and its ``resource`` route, are hidden with the resource.

The organization's portal network restriction and an offering's subnet
concealment hide a resource from consumers outside the allowed networks. The
order's ``resource`` action must not hand out the resource those rules hide
from the consumer resource views (#985), and the order itself, which names the
resource, is hidden with it: from the order list and detail, the dashboard's
own-orders list and its attachment downloads (#1001). Staff, support, callers
inside the allowed networks and the offering's provider keep both.
"""

from ddt import data, ddt
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import test

from waldur_core.core import access
from waldur_core.media import models as media_models
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace import models, views
from waldur_mastermind.marketplace.enums import OrderStates
from waldur_mastermind.marketplace.tests import fixtures

ALLOWED_IP = "100.100.100.100"
DENIED_IP = "203.0.113.7"

VALID_PDF = (
    b"%PDF-1.4\n1 0 obj\n<<\n/Type /Catalog\n>>\nendobj\n"
    b"xref\n0 1\n0000000000 65535 f \ntrailer\n<<\n/Size 1\n"
    b"/Root 1 0 R\n>>\nstartxref\n9\n%%EOF"
)


class OrderResourceVisibilityMixin:
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.order = self.fixture.order
        self.resource = self.fixture.resource
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_RESOURCES)
        CustomerRole.OWNER.add_permission(PermissionEnum.LIST_ORDERS)
        OfferingRole.MANAGER.add_permission(PermissionEnum.LIST_ORDERS)

    def get(self, url, user, ip):
        self.client.force_authenticate(user)
        if ip is None:
            # No X-Forwarded-For and no peer: the request has no client address.
            return self.client.get(url, REMOTE_ADDR="")
        return self.client.get(url, HTTP_X_FORWARDED_FOR=ip)

    def get_order_resource(self, user, ip):
        url = reverse(
            "marketplace-order-resource", kwargs={"uuid": self.order.uuid.hex}
        )
        return self.get(url, user, ip)

    def get_consumer_resource(self, user, ip):
        url = reverse(
            "marketplace-resource-detail", kwargs={"uuid": self.resource.uuid.hex}
        )
        return self.get(url, user, ip)

    def get_order(self, user, ip):
        url = reverse("marketplace-order-detail", kwargs={"uuid": self.order.uuid.hex})
        return self.get(url, user, ip)

    def listed_order_uuids(self, url_name, user, ip):
        response = self.get(reverse(url_name), user, ip)
        self.assertEqual(response.status_code, 200, response.data)
        return {str(row["uuid"]).replace("-", "") for row in response.data}

    def assert_order_visible(self, user, ip):
        response = self.get_order(user, ip)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn(
            self.order.uuid.hex,
            self.listed_order_uuids("marketplace-order-list", user, ip),
        )

    def assert_order_hidden(self, user, ip):
        self.assertEqual(self.get_order(user, ip).status_code, 404)
        self.assertNotIn(
            self.order.uuid.hex,
            self.listed_order_uuids("marketplace-order-list", user, ip),
        )

    def assert_hidden(self, user, ip):
        self.assert_order_hidden(user, ip)
        self.assertEqual(self.get_consumer_resource(user, ip).status_code, 404)
        self.assertEqual(self.get_order_resource(user, ip).status_code, 404)

    def assert_shown(self, user, ip):
        self.assert_order_visible(user, ip)
        response = self.get_order_resource(user, ip)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["uuid"], self.resource.uuid.hex)


class RestrictedOrderResourceTests(OrderResourceVisibilityMixin):
    """What every restriction must do; subclasses set the restriction up."""

    def test_hidden_from_consumer_outside_allowed_networks(self):
        self.assert_hidden(self.fixture.owner, DENIED_IP)

    def test_shown_to_consumer_inside_allowed_networks(self):
        self.assertEqual(
            self.get_consumer_resource(self.fixture.owner, ALLOWED_IP).status_code,
            200,
        )
        self.assert_shown(self.fixture.owner, ALLOWED_IP)

    def test_shown_to_staff_and_support(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            self.assert_shown(user, DENIED_IP)

    def test_shown_to_provider_side(self):
        # The provider resource view applies neither rule, and the order page
        # is the provider's page too.
        for user in (
            self.fixture.offering_owner,
            self.fixture.offering_manager,
            self.fixture.provider_manager,
        ):
            self.assert_shown(user, DENIED_IP)

    def test_shown_without_client_address(self):
        # As the consumer resource view does: no address, no network rule.
        self.assertEqual(
            self.get_consumer_resource(self.fixture.owner, None).status_code, 200
        )
        self.assert_shown(self.fixture.owner, None)

    def test_address_is_matched_in_canonical_form(self):
        # A host:port hop (Azure Application Gateway) or an IPv4-mapped IPv6
        # address is matched by its address, not rejected by the inet lookup.
        for ip in (f"{ALLOWED_IP}:5678", f"::ffff:{ALLOWED_IP}"):
            self.assertEqual(
                self.get_consumer_resource(self.fixture.owner, ip).status_code, 200
            )
            self.assert_shown(self.fixture.owner, ip)
        self.assert_hidden(self.fixture.owner, f"{DENIED_IP}:5678")

    def test_hidden_from_unrelated_user(self):
        outsider = fixtures.MarketplaceFixture().owner
        self.assertEqual(self.get_order_resource(outsider, ALLOWED_IP).status_code, 404)

    def test_detail_actions_do_not_find_the_order(self):
        self.order.state = OrderStates.PENDING_CONSUMER
        self.order.save(update_fields=["state"])
        url = reverse(
            "marketplace-order-approve-by-consumer",
            kwargs={"uuid": self.order.uuid.hex},
        )
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.post(url, HTTP_X_FORWARDED_FOR=DENIED_IP)
        self.assertEqual(response.status_code, 404)
        response = self.client.post(url, HTTP_X_FORWARDED_FOR=ALLOWED_IP)
        self.assertNotEqual(response.status_code, 404)

    def test_only_the_restricted_organization_orders_are_hidden(self):
        # The same user in an organization without restrictions keeps its
        # orders while the restricted organization's are hidden.
        other = fixtures.MarketplaceFixture()
        other.customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        other_order = other.order

        listed = self.listed_order_uuids(
            "marketplace-order-list", self.fixture.owner, DENIED_IP
        )
        self.assertIn(other_order.uuid.hex, listed)
        self.assertNotIn(self.order.uuid.hex, listed)

    def test_dashboard_own_orders_hide_the_order(self):
        self.order.created_by = self.fixture.owner
        self.order.state = OrderStates.PENDING_CONSUMER
        self.order.save(update_fields=["created_by", "state"])
        url_name = "marketplace-order-dashboard-my-orders"

        listed = self.listed_order_uuids(url_name, self.fixture.owner, DENIED_IP)
        self.assertNotIn(self.order.uuid.hex, listed)
        listed = self.listed_order_uuids(url_name, self.fixture.owner, ALLOWED_IP)
        self.assertIn(self.order.uuid.hex, listed)

    def test_attachment_download_follows_the_order(self):
        self.order.attachment = SimpleUploadedFile(
            "order.pdf", VALID_PDF, content_type="application/pdf"
        )
        self.order.save(update_fields=["attachment"])
        media_file = media_models.File.objects.get(name=self.order.attachment.name)
        url = reverse("media", kwargs={"uuid": media_file.uuid.hex})

        self.assertEqual(self.get(url, self.fixture.owner, DENIED_IP).status_code, 404)
        self.assertEqual(self.get(url, self.fixture.owner, ALLOWED_IP).status_code, 200)
        self.assertEqual(self.get(url, self.fixture.owner, None).status_code, 200)
        for user in (self.fixture.staff, self.fixture.offering_owner):
            self.assertEqual(self.get(url, user, DENIED_IP).status_code, 200)


class OrderResourceNetworkRestrictionTest(
    RestrictedOrderResourceTests, test.APITestCase
):
    """The organization limits portal access to some networks."""

    def setUp(self):
        super().setUp()
        structure_models.AccessSubnet.objects.create(
            customer=self.fixture.customer,
            inet=f"{ALLOWED_IP}/32",
            applies_to_portal=True,
        )


class OrderResourceSubnetConcealmentTest(
    RestrictedOrderResourceTests, test.APITestCase
):
    """The offering conceals resources outside their access subnets."""

    def setUp(self):
        super().setUp()
        self.set_concealment(True)
        subnet = structure_models.AccessSubnet.objects.create(
            customer=self.fixture.customer, inet=f"{ALLOWED_IP}/32"
        )
        models.AccessSubnetOfferingScope.objects.create(
            access_subnet=subnet, offering=self.fixture.offering
        )

    def set_concealment(self, value):
        self.fixture.offering.plugin_options = {
            "conceal_subnet_restricted_resources": value
        }
        self.fixture.offering.save()

    def test_shown_when_concealment_disabled(self):
        self.set_concealment(False)
        self.assert_shown(self.fixture.owner, DENIED_IP)


@ddt
class OrderResourceUnrestrictedTest(OrderResourceVisibilityMixin, test.APITestCase):
    @data(DENIED_IP, None)
    def test_shown_to_consumer(self, ip):
        self.assert_shown(self.fixture.owner, ip)


class OrderResourceAccessDeclarationTest(SimpleTestCase):
    def test_route_says_who_may_call_it(self):
        view = views.OrderViewSet(action="resource", detail=True, kwargs={"uuid": "x"})
        route = access.access_for(view, "GET")
        self.assertTrue(route.described)
        self.assertIn(views.CONNECTED_RESOURCE_ACCESS, route.schema_entries())
