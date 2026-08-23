"""Re-pointing existing accounts onto a late override pool.

Adding a pool to an offering that already has accounts must not move anyone
implicitly: the numbers are already stamped on files in the provider's
filesystem. The move is previewed, then applied on explicit confirmation.
"""

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, utils
from waldur_mastermind.marketplace.tests import factories


class PosixIdPoolRepointTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.provider_pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        self.offering = factories.OfferingFactory(
            customer=self.customer, name="Cluster A"
        )
        self.other_offering = factories.OfferingFactory(
            customer=self.customer, name="Cluster B"
        )
        self.user = structure_factories.UserFactory()
        self.account = self.make_account(self.offering, self.user)
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_POSIX_ID_POOL)

    def make_account(self, offering, user):
        offering_user = factories.OfferingUserFactory(offering=offering, user=user)
        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save()
        return offering_user

    def add_override_pool(self):
        return factories.PosixIdPoolFactory(
            offering=self.offering,
            min_uid=500000,
            max_uid=500099,
            next_uid=500000,
            min_gid=600000,
            max_gid=600099,
            next_gid=600000,
        )

    def get_url(self, pool, action):
        return factories.PosixIdPoolFactory.get_url(pool, action=action)

    def test_adding_an_override_pool_changes_nothing(self):
        self.add_override_pool()
        self.account.refresh_from_db()
        self.assertEqual(self.account.backend_metadata["uidnumber"], 100000)

    def test_preview_reports_the_move_without_writing(self):
        pool = self.add_override_pool()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url(pool, "repoint_preview"))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        changes = {row["namespace"]: row for row in response.data["changes"]}
        self.assertEqual(changes["uid"]["old_value"], 100000)
        self.assertEqual(changes["uid"]["new_value"], 500000)
        self.assertEqual(changes["gid"]["new_value"], 600000)

        self.account.refresh_from_db()
        self.assertEqual(self.account.backend_metadata["uidnumber"], 100000)
        self.assertFalse(models.PosixIdentity.objects.filter(pool=pool).exists())
        pool.refresh_from_db()
        self.assertEqual(pool.next_uid, 500000)

    def test_apply_moves_the_accounts(self):
        pool = self.add_override_pool()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url(pool, "repoint"), {"confirm": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.account.refresh_from_db()
        self.assertEqual(self.account.backend_metadata["uidnumber"], 500000)
        self.assertEqual(self.account.backend_metadata["primarygroup"], 600000)

        identity = models.PosixIdentity.objects.get(pool=pool, user=self.user)
        self.assertIsNone(identity.released_at)

    def test_the_freed_value_is_withheld_from_recycling(self):
        pool = self.add_override_pool()
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.get_url(pool, "repoint"), {"confirm": True})

        old = models.PosixIdentity.objects.get(pool=self.provider_pool, user=self.user)
        self.assertIsNotNone(old.released_at)
        self.assertFalse(old.recyclable)

    def test_value_stays_reserved_while_another_offering_uses_it(self):
        self.make_account(self.other_offering, self.user)
        pool = self.add_override_pool()
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(self.get_url(pool, "repoint"), {"confirm": True})

        old = models.PosixIdentity.objects.get(pool=self.provider_pool, user=self.user)
        self.assertIsNone(old.released_at)

    def test_confirmation_is_required(self):
        pool = self.add_override_pool()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url(pool, "repoint"), {})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.account.refresh_from_db()
        self.assertEqual(self.account.backend_metadata["uidnumber"], 100000)

    def test_provider_pool_cannot_be_repointed(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.get_url(self.provider_pool, "repoint"), {"confirm": True}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_identity_is_kept_when_the_new_pool_cannot_take_a_namespace(self):
        # A GID-only override: the account keeps sourcing its UID from the
        # provider pool, so that identity must stay active and keep the UID
        # reserved.
        pool = factories.PosixIdPoolFactory(
            offering=self.offering,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=600000,
            max_gid=600099,
            next_gid=600000,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.get_url(pool, "repoint"), {"confirm": True})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["retained"], 1)
        old = models.PosixIdentity.objects.get(pool=self.provider_pool, user=self.user)
        self.assertIsNone(old.released_at)
        self.account.refresh_from_db()
        self.assertEqual(self.account.backend_metadata["uidnumber"], old.uid)
        self.assertEqual(self.account.backend_metadata["primarygroup"], 600000)

    def test_consumers_left_behind_are_reported(self):
        resource = factories.ResourceFactory(offering=self.offering)
        robot = factories.RobotAccountFactory(resource=resource, username="robot-1")
        utils.setup_linux_related_data(robot, self.offering)
        robot.save()

        pool = self.add_override_pool()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.get_url(pool, "repoint_preview"))

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        kinds = {row["kind"] for row in response.data["other_consumers"]}
        self.assertIn("robot account", kinds)
