"""Floating IPs resolve the external network as tenant creation does.

Tenant creation connects the default router to the tenant's own external
network, then the organization's on the provider, then the provider default.
Floating-IP validation and allocation read only the provider default, so a
provider that configures external networks per organization had every floating
IP refused with "Please specify tenant external network".
"""

import uuid

from rest_framework import status, test

from waldur_openstack import models
from waldur_openstack.serializers import _connect_floating_ip_to_instance

from . import factories, fixtures


class OrganizationExternalNetworkTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.fixture.settings.options.pop("external_network_id", None)
        self.fixture.settings.save()
        # Only the organization's network: neither the tenant nor the provider
        # names one.
        tenant = self.fixture.tenant
        tenant.external_network_id = ""
        tenant.external_network_ref = None
        tenant.save()
        self.external_network_id = str(uuid.uuid4())
        factories.CustomerOpenStackFactory(
            settings=self.fixture.settings,
            customer=self.fixture.customer,
            external_network_id=self.external_network_id,
        )
        self.instance = self.fixture.instance
        factories.PortFactory(
            instance=self.instance,
            subnet=self.fixture.subnet,
            network=self.fixture.network,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            service_settings=self.fixture.settings,
        )
        self.client.force_authenticate(self.fixture.admin)

    def test_a_floating_ip_can_be_requested(self):
        response = self.client.post(
            factories.InstanceFactory.get_url(
                self.instance, action="update_floating_ips"
            ),
            {
                "floating_ips": [
                    {"subnet": factories.SubNetFactory.get_url(self.fixture.subnet)}
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

    def test_the_new_floating_ip_is_on_the_organizations_network(self):
        floating_ip = _connect_floating_ip_to_instance(
            None, self.fixture.subnet, self.instance
        )

        self.assertEqual(floating_ip.backend_network_id, self.external_network_id)

    def test_without_any_external_network_it_is_still_refused(self):
        models.CustomerOpenStack.objects.all().delete()

        response = self.client.post(
            factories.InstanceFactory.get_url(
                self.instance, action="update_floating_ips"
            ),
            {
                "floating_ips": [
                    {"subnet": factories.SubNetFactory.get_url(self.fixture.subnet)}
                ]
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
