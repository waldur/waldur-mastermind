from rest_framework import status, test

from waldur_openstack import models

from . import factories, fixtures


class PortSecurityGroupsTest(test.APITransactionTestCase):
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
        reread_security_groups = list(reread_port.security_groups.order_by("name"))
        self.assertEqual(reread_security_groups, self.security_groups)
