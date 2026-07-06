"""Tests for sourcing an offering user's UID / primary GID from the Waldur user's
attributes (e.g. an OIDC-provided uid_number) instead of the POSIX ID pool."""

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace import utils
from waldur_mastermind.marketplace.tests import factories


class PosixUserAttributeSourceTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={
                "enable_posix_account": True,
                "uid_source": "user_attribute",
                "gid_source": "pool",
            },
        )
        # GID-only pool: UIDs come from the user attribute, GIDs from the pool.
        factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=2000,
            max_gid=2999,
            next_gid=2000,
        )

    def _provision(self, user):
        offering_user = factories.OfferingUserFactory(offering=self.offering, user=user)
        utils.setup_linux_related_data(offering_user, self.offering)
        offering_user.save()
        return offering_user

    def test_uid_from_user_attribute_gid_from_pool(self):
        user = structure_factories.UserFactory(uid_number=50000)
        offering_user = self._provision(user)
        self.assertEqual(offering_user.backend_metadata["uidnumber"], 50000)
        # The primary GID is still allocated from the pool.
        self.assertEqual(offering_user.backend_metadata["primarygroup"], 2000)

    def test_missing_user_attribute_leaves_uid_unset(self):
        user = structure_factories.UserFactory(uid_number=None)
        offering_user = self._provision(user)
        self.assertNotIn("uidnumber", offering_user.backend_metadata)
        self.assertEqual(offering_user.backend_metadata["primarygroup"], 2000)

    def test_primary_gid_from_user_attribute(self):
        offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={
                "enable_posix_account": True,
                "uid_source": "user_attribute",
                "gid_source": "user_attribute",
            },
        )
        user = structure_factories.UserFactory(uid_number=50000, primary_gid=60000)
        offering_user = factories.OfferingUserFactory(offering=offering, user=user)
        utils.setup_linux_related_data(offering_user, offering)
        self.assertEqual(offering_user.backend_metadata["uidnumber"], 50000)
        self.assertEqual(offering_user.backend_metadata["primarygroup"], 60000)

    def test_pool_source_ignores_user_attribute(self):
        # Default uid_source=pool: the pool allocates the UID even if the user
        # carries a uid_number attribute.
        offering = factories.OfferingFactory(
            customer=self.customer,
            plugin_options={"enable_posix_account": True},  # sources default to pool
        )
        factories.PosixIdPoolFactory(
            offering=offering,
            min_uid=10000,
            max_uid=10999,
            next_uid=10000,
            min_gid=20000,
            max_gid=20999,
            next_gid=20000,
        )
        user = structure_factories.UserFactory(uid_number=50000)
        offering_user = factories.OfferingUserFactory(offering=offering, user=user)
        utils.setup_linux_related_data(offering_user, offering)
        self.assertEqual(offering_user.backend_metadata["uidnumber"], 10000)
