"""Actions on a VM and port that sit on a subnet another tenant shared over RBAC.

Found by running a VM on a network the cloud's admin project shared with a
tenant (waldur/waldur-mastermind#601): the floating IP was created for the
subnet's owner rather than for the VM, and Neutron's refusal to let the consumer
set allowed address pairs on the owner's network surfaced as a 500.
"""

from unittest import mock

from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack import models
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.serializers import _connect_floating_ip_to_instance

from . import factories, fixtures

EXTERNAL_NETWORK_ID = "0c6b1b4c-7d6a-4a7e-9f7b-3b1d4f2e8a10"


class SharedSubnetMixin:
    def setUp(self):
        super().setUp()
        self.owner = fixtures.OpenStackFixture()
        self.consumer = fixtures.OpenStackFixture()
        self.consumer.tenant.service_settings = self.owner.settings
        self.consumer.tenant.save()
        self.subnet = self.owner.subnet
        models.NetworkRBACPolicy.objects.create(
            network=self.subnet.network,
            target_tenant=self.consumer.tenant,
            policy_type=models.NetworkRBACPolicy.NetworkShareType.SHARED,
        )


class FloatingIpOnSharedSubnetTest(SharedSubnetMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.owner.settings.options["external_network_id"] = EXTERNAL_NETWORK_ID
        self.owner.settings.save()
        self.instance = factories.InstanceFactory(
            service_settings=self.owner.settings,
            project=self.consumer.project,
            tenant=self.consumer.tenant,
        )
        factories.PortFactory(
            instance=self.instance,
            subnet=self.subnet,
            network=self.subnet.network,
            tenant=self.consumer.tenant,
            project=self.consumer.project,
            service_settings=self.owner.settings,
        )

    def test_a_new_floating_ip_is_the_instance_tenants(self):
        floating_ip = _connect_floating_ip_to_instance(None, self.subnet, self.instance)

        self.assertEqual(floating_ip.tenant, self.consumer.tenant)
        self.assertEqual(floating_ip.project, self.consumer.project)

    def test_a_free_floating_ip_of_the_owner_is_not_taken(self):
        owners = factories.FloatingIPFactory(
            tenant=self.owner.tenant,
            service_settings=self.owner.settings,
            project=self.owner.project,
            backend_network_id=EXTERNAL_NETWORK_ID,
            backend_id="owners-fip",
            port=None,
        )

        floating_ip = _connect_floating_ip_to_instance(None, self.subnet, self.instance)

        self.assertNotEqual(floating_ip.pk, owners.pk)
        self.assertEqual(floating_ip.tenant, self.consumer.tenant)


class AllowedAddressPairsOnSharedNetworkTest(SharedSubnetMixin, test.APITestCase):
    def setUp(self):
        super().setUp()
        self.port = factories.PortFactory(
            service_settings=self.owner.settings,
            project=self.consumer.project,
            tenant=self.consumer.tenant,
            network=self.subnet.network,
            fixed_ips=[
                {"subnet_id": self.subnet.backend_id, "ip_address": "192.168.42.10"}
            ],
            state=CoreStates.OK,
        )
        backend = mock.patch(
            "waldur_openstack.backend.OpenStackBackend.set_port_allowed_address_pairs",
            side_effect=OpenStackBackendError(
                "(rule:update_port:allowed_address_pairs) is disallowed by policy"
            ),
        )
        backend.start()
        self.addCleanup(backend.stop)
        self.client.force_authenticate(self.consumer.staff)

    def test_a_policy_refusal_is_a_bad_request_that_says_why(self):
        response = self.client.post(
            factories.PortFactory.get_url(self.port, "set_allowed_address_pairs"),
            {"allowed_address_pairs": [{"ip_address": "10.250.0.0/24"}]},
            format="json",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("shared by another project", str(response.data))
        self.port.refresh_from_db()
        self.assertFalse(self.port.allowed_address_pairs)
