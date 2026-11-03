"""The plugin APIs conceal what the consumer resource view conceals (#1004).

An offering with ``conceal_subnet_restricted_resources`` hides its resources
from consumers outside their access subnets. The OpenStack views serve the
object behind such a resource, and the objects that belong to it, and must
hide them too: an instance and its ports, or a tenant and its networks,
subnets, security groups, floating IPs and routers. An instance or volume is a
resource of its own and follows its own resource, not its tenant's.
"""

from django.db.models.constants import LOOKUP_SEP
from django.test import SimpleTestCase
from django.urls import reverse
from rest_framework import test

from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.common import utils as common_utils
from waldur_mastermind.marketplace import filters as marketplace_filters
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories
from waldur_openstack import views as openstack_views
from waldur_openstack.tests import factories as openstack_factories
from waldur_openstack.tests import fixtures

ALLOWED_IP = "100.100.100.100"
DENIED_IP = "203.0.113.7"
FLAG = "conceal_subnet_restricted_resources"


class ConcealmentMixin:
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.offering = factories.OfferingFactory(plugin_options={FLAG: True})
        subnet = structure_models.AccessSubnet.objects.create(
            customer=self.fixture.customer, inet=f"{ALLOWED_IP}/32"
        )
        models.AccessSubnetOfferingScope.objects.create(
            access_subnet=subnet, offering=self.offering
        )

    def conceal(self, scope):
        return factories.ResourceFactory(
            scope=scope, project=self.fixture.project, offering=self.offering
        )

    def get(self, url, user, ip):
        self.client.force_authenticate(user)
        if ip is None:
            return self.client.get(url, REMOTE_ADDR="")
        return self.client.get(url, HTTP_X_FORWARDED_FOR=ip)

    def detail_status(self, basename, obj, user, ip):
        url = reverse(f"{basename}-detail", kwargs={"uuid": obj.uuid.hex})
        return self.get(url, user, ip).status_code

    def listed(self, basename, user, ip):
        response = self.get(reverse(f"{basename}-list"), user, ip)
        self.assertEqual(response.status_code, 200, response.data)
        return {row["uuid"] for row in response.data}

    def assert_hidden(self, basename, obj, user=None, ip=DENIED_IP):
        user = user or self.fixture.owner
        self.assertEqual(self.detail_status(basename, obj, user, ip), 404, basename)
        self.assertNotIn(obj.uuid.hex, self.listed(basename, user, ip), basename)

    def assert_shown(self, basename, obj, user=None, ip=DENIED_IP):
        user = user or self.fixture.owner
        self.assertEqual(self.detail_status(basename, obj, user, ip), 200, basename)
        self.assertIn(obj.uuid.hex, self.listed(basename, user, ip), basename)


class ConcealedInstanceTest(ConcealmentMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.instance = self.fixture.instance
        self.volume = self.fixture.volume
        self.volume.instance = self.instance
        self.volume.save(update_fields=["instance"])
        self.port = self.fixture.port
        self.port.instance = self.instance
        self.port.save(update_fields=["instance"])
        self.resource = self.conceal(self.instance)

    def objects(self):
        return (
            ("openstack-instance", self.instance),
            ("openstack-port", self.port),
        )

    def test_attached_volume_follows_its_own_resource(self):
        # A volume is a resource of its own: concealing the instance it is
        # attached to does not hide it.
        self.assert_shown("openstack-volume", self.volume)

    def test_internal_replay_is_exempt(self):
        # Processors replay plugin views for the user an order is processed
        # as; the replay has no client and carries the loopback address.
        view = openstack_views.InstanceViewSet.as_view({"get": "retrieve"})
        response = common_utils.get_request(
            view, self.fixture.owner, uuid=self.instance.uuid.hex
        )
        self.assertEqual(response.status_code, 200)
        # The exemption is the internal mark, not the loopback address.
        url = reverse(
            "openstack-instance-detail", kwargs={"uuid": self.instance.uuid.hex}
        )
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(url, REMOTE_ADDR="127.0.0.1")
        self.assertEqual(response.status_code, 404)

    def test_hidden_outside_the_access_subnets(self):
        for basename, obj in self.objects():
            self.assert_hidden(basename, obj)

    def test_marketplace_resource_is_hidden_likewise(self):
        url = reverse(
            "marketplace-resource-detail", kwargs={"uuid": self.resource.uuid.hex}
        )
        self.assertEqual(self.get(url, self.fixture.owner, DENIED_IP).status_code, 404)

    def test_shown_inside_the_access_subnets(self):
        for basename, obj in self.objects():
            self.assert_shown(basename, obj, ip=ALLOWED_IP)
        # Host:port and IPv4-mapped forms are matched by the address.
        for ip in (f"{ALLOWED_IP}:5678", f"::ffff:{ALLOWED_IP}"):
            self.assert_shown("openstack-instance", self.instance, ip=ip)

    def test_shown_without_client_address(self):
        for basename, obj in self.objects():
            self.assert_shown(basename, obj, ip=None)

    def test_shown_when_concealment_is_off(self):
        self.offering.plugin_options = {FLAG: False}
        self.offering.save(update_fields=["plugin_options"])
        for basename, obj in self.objects():
            self.assert_shown(basename, obj)

    def test_shown_to_staff_and_support(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            for basename, obj in self.objects():
                self.assert_shown(basename, obj, user=user)

    def test_shown_to_a_caller_who_provides_the_offering(self):
        # As with orders: the provider resource view applies no network rule.
        self.offering.customer.add_user(self.fixture.owner, CustomerRole.OWNER)
        for basename, obj in self.objects():
            self.assert_shown(basename, obj)

    def test_other_objects_of_the_tenant_stay_visible(self):
        # Only the instance's resource is concealed, not the tenant's.
        other = factories.ResourceFactory(
            scope=self.fixture.tenant, project=self.fixture.project
        )
        self.assertFalse(other.offering.plugin_options.get(FLAG))
        self.assert_shown("openstack-tenant", self.fixture.tenant)
        self.assert_shown("openstack-network", self.fixture.network)
        self.assert_shown("openstack-sgp", self.fixture.security_group)

    def test_unrelated_models_are_unaffected(self):
        url = reverse("project-detail", kwargs={"uuid": self.fixture.project.uuid.hex})
        self.assertEqual(self.get(url, self.fixture.owner, DENIED_IP).status_code, 200)


class ConcealedTenantTest(ConcealmentMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.conceal(self.fixture.tenant)

    def objects(self):
        return (
            ("openstack-tenant", self.fixture.tenant),
            ("openstack-network", self.fixture.network),
            ("openstack-subnet", self.fixture.subnet),
            ("openstack-sgp", self.fixture.security_group),
            ("openstack-fip", self.fixture.floating_ip),
            ("openstack-router", self.fixture.router),
        )

    def test_hidden_outside_the_access_subnets(self):
        for basename, obj in self.objects():
            self.assert_hidden(basename, obj)

    def test_shown_inside_the_access_subnets(self):
        for basename, obj in self.objects():
            self.assert_shown(basename, obj, ip=ALLOWED_IP)

    def test_instances_and_volumes_follow_their_own_resource(self):
        self.assert_shown("openstack-instance", self.fixture.instance)
        self.assert_shown("openstack-volume", self.fixture.volume)

    def test_shared_network_stays_visible_to_the_target_tenant(self):
        # Another organization's tenant reaches the network through its own
        # tenant; the owner's subnets do not hide it from that tenant's users.
        other = fixtures.OpenStackFixture()
        openstack_factories.NetworkRBACPolicyFactory(
            network=self.fixture.network, target_tenant=other.tenant
        )
        subnet = self.fixture.subnet
        self.assert_shown("openstack-network", self.fixture.network, user=other.owner)
        self.assert_shown("openstack-subnet", subnet, user=other.owner)
        # The owner's own users outside the subnets still do not see it.
        self.assert_hidden("openstack-network", self.fixture.network)

    def test_sharing_within_the_organization_does_not_undo_concealment(self):
        # Shared into another tenant of the same project: the network's own
        # users would reach it through the share and could edit it.
        target = openstack_factories.TenantFactory(
            project=self.fixture.project, service_settings=self.fixture.settings
        )
        openstack_factories.NetworkRBACPolicyFactory(
            network=self.fixture.network, target_tenant=target
        )
        self.assert_shown("openstack-tenant", target)
        self.assert_hidden("openstack-network", self.fixture.network)
        self.assert_hidden("openstack-subnet", self.fixture.subnet)

    def test_share_into_a_concealed_tenant_is_hidden_with_it(self):
        other = fixtures.OpenStackFixture()
        openstack_factories.NetworkRBACPolicyFactory(
            network=self.fixture.network, target_tenant=other.tenant
        )
        subnet = structure_models.AccessSubnet.objects.create(
            customer=other.customer, inet=f"{ALLOWED_IP}/32"
        )
        models.AccessSubnetOfferingScope.objects.create(
            access_subnet=subnet, offering=self.offering
        )
        factories.ResourceFactory(
            scope=other.tenant, project=other.project, offering=self.offering
        )
        self.assert_hidden("openstack-network", self.fixture.network, user=other.owner)
        self.assert_shown(
            "openstack-network", self.fixture.network, user=other.owner, ip=ALLOWED_IP
        )


class ConcealmentParentsTest(SimpleTestCase):
    def test_every_registered_path_reaches_its_scope_model(self):
        registered = marketplace_filters._concealment_parents
        self.assertTrue(registered)
        for model, parents in registered.items():
            for path, scope_model in parents:
                target = model
                for part in path.split(LOOKUP_SEP):
                    target = target._meta.get_field(part).related_model
                self.assertIs(target, scope_model, f"{model.__name__}.{path}")
