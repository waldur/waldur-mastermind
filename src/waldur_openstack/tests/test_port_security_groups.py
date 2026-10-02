from unittest import mock

from neutronclient.client import exceptions as neutron_exceptions
from rest_framework import status, test

from waldur_openstack import models
from waldur_openstack.exceptions import OpenStackBackendError

from . import factories, fixtures


class PortSecurityGroupsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.port = self.fixture.port

        self.security_groups = factories.SecurityGroupFactory.create_batch(
            2, tenant=self.fixture.tenant
        )
        self.port.security_groups.add(*self.security_groups)

    def test_update_security_groups(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.PortFactory.get_url(self.port, "update_security_groups")

        response = self.client.post(
            url,
            {
                "security_groups": [
                    factories.SecurityGroupFactory.get_url(s)
                    for s in self.security_groups
                ]
            },
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)

        reread_port = models.Port.objects.get(pk=self.port.pk)
        # Compare as an unordered collection: the assertion is about which
        # groups the port ends up with, and ordering by name is not the same
        # as creation order once the factory sequence crosses a power of ten
        # ("security_group10" sorts before "security_group9").
        self.assertCountEqual(reread_port.security_groups.all(), self.security_groups)


class PortSecurityGroupsPushTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.port = self.fixture.port

    @mock.patch("waldur_openstack.backend.get_neutron_client")
    @mock.patch("waldur_openstack.backend.get_tenant_session")
    def test_neutron_refusal_is_a_backend_error(self, _session, get_neutron_client):
        get_neutron_client.return_value.update_port.side_effect = (
            neutron_exceptions.NeutronClientException()
        )

        with self.assertRaises(OpenStackBackendError):
            self.port.get_backend().push_port_security_groups(self.port)
