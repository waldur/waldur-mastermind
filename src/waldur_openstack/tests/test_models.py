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
