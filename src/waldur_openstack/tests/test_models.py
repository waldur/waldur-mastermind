from django.test import TestCase

from . import factories, fixtures


class InstanceTest(TestCase):
    def test_instance_size_is_sum_of_volumes_size(self):
        fixture = fixtures.OpenStackFixture()
        expected_size = sum(
            fixture.instance.volumes.all().values_list("size", flat=True)
        )
        self.assertEqual(fixture.instance.size, expected_size)

    def test_external_address_excludes_floating_ips_without_one(self):
        # A floating IP can exist without an external_address set (it's
        # nullable on the model). The property's return type is declared as
        # set[str], so a floating IP without one must be excluded rather
        # than included as a None element -- otherwise it serializes as
        # `null` in a JSON array the OpenAPI schema declares as items of
        # type string, which breaks strictly-typed SDK clients.
        fixture = fixtures.OpenStackFixture()
        factories.FloatingIPFactory(
            tenant=fixture.tenant,
            port=fixture.port,
            external_address="203.0.113.10",
        )
        factories.FloatingIPFactory(
            tenant=fixture.tenant,
            port=fixture.port,
            external_address=None,
        )

        self.assertEqual(fixture.instance.external_address, {"203.0.113.10"})


class TenantAccessUrlTest(TestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.settings = self.fixture.settings
        self.settings.options.pop("access_url", None)

    def _access_url(self, backend_url):
        self.settings.backend_url = backend_url
        self.settings.save()
        return self.fixture.tenant.get_access_url()

    def test_ipv6_keystone_host_keeps_its_brackets(self):
        self.assertEqual(
            self._access_url("https://[2001:db8::10]/identity/v3"),
            "https://[2001:db8::10]/dashboard",
        )

    def test_ipv6_keystone_port_is_not_the_dashboard_port(self):
        self.assertEqual(
            self._access_url("https://[2001:db8::10]:5000/identity/v3"),
            "https://[2001:db8::10]/dashboard",
        )

    def test_ipv4_keystone_host(self):
        self.assertEqual(
            self._access_url("https://192.0.2.10/identity/v3"),
            "https://192.0.2.10/dashboard",
        )

    def test_hostname_keystone_host(self):
        self.assertEqual(
            self._access_url("https://keystone.example.com/identity/v3"),
            "https://keystone.example.com/dashboard",
        )

    def test_keystone_port_is_not_the_dashboard_port(self):
        # A classic deployment serves Keystone on :5000 and Horizon on the
        # default port of the same host.
        self.assertEqual(
            self._access_url("https://keystone.example.com:5000/v3"),
            "https://keystone.example.com/dashboard",
        )

    def test_credentials_in_keystone_url_are_not_exposed(self):
        self.assertEqual(
            self._access_url("https://user:secret@[2001:db8::10]/identity/v3"),
            "https://[2001:db8::10]/dashboard",
        )

    def test_access_url_option_overrides_keystone_url(self):
        self.settings.options["access_url"] = "https://horizon.example.com/"
        self.assertEqual(
            self._access_url("https://[2001:db8::10]/identity/v3"),
            "https://horizon.example.com/",
        )
