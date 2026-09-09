"""One protocol, one message for a subnet's gateway IP.

`SubNet.gateway_ip` is a `GenericIPAddressField(protocol="IPv4")`, and the
serializer used to let ModelSerializer derive the field: DRF then built an
`IPAddressField` with its own default protocol ("both") *and* copied the model's
IPv4 validator, so a bad address came back with two messages that disagree --
"Enter a valid IPv4 address." from the model and "Enter a valid IPv4 or IPv6
address." from the field. An IPv6 literal passed the field and was rejected by
the model, which is the same contradiction seen from the other side.
"""

from rest_framework import status, test

from . import factories, fixtures


class SubnetGatewayIpValidationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.client.force_authenticate(self.fixture.owner)
        self.url = factories.NetworkFactory.get_url(
            network=self.fixture.network, action="create_subnet"
        )

    def _post(self, **extra):
        return self.client.post(self.url, {"name": "test-subnet", **extra})

    def test_a_bad_address_is_reported_once(self):
        response = self._post(gateway_ip="999.999.1.1")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            [str(message) for message in response.data["gateway_ip"]],
            ["Enter a valid IPv4 address."],
        )

    def test_an_ipv6_address_is_rejected_by_the_field_itself(self):
        response = self._post(gateway_ip="2001:db8::1")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("gateway_ip", response.data)

    def test_a_valid_address_is_still_accepted(self):
        from unittest import mock

        with mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute"):
            response = self._post(gateway_ip="192.168.42.1")

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["gateway_ip"], "192.168.42.1")

    def test_it_remains_optional(self):
        from unittest import mock

        with mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute"):
            response = self._post()

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_disable_gateway_still_clears_it(self):
        """`validate()` sets gateway_ip to None for a gateway-less subnet, so the
        field has to accept null."""
        from unittest import mock

        with mock.patch("waldur_openstack.executors.SubNetCreateExecutor.execute"):
            response = self._post(disable_gateway=True)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIsNone(response.data["gateway_ip"])
