"""One POSIX identity per user per pool.

A user with accounts on several offerings of one service provider draws a single
UID and a single primary GID from the provider's pool: the provider runs one
LDAP tree, one username and one home directory per user, so two different
uidNumbers put the site agents in conflict over the same entry. An offering with
its own pool resolves elsewhere and keeps its own values.
"""

from django.contrib.contenttypes.models import ContentType
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models, posix_ids, utils
from waldur_mastermind.marketplace.tests import factories


class SharedPosixIdentityTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=100099,
            next_uid=100000,
            min_gid=200000,
            max_gid=200099,
            next_gid=200000,
        )
        self.offering_a = factories.OfferingFactory(
            customer=self.customer, name="Cluster A"
        )
        self.offering_b = factories.OfferingFactory(
            customer=self.customer, name="Cluster B"
        )
        self.user = structure_factories.UserFactory()

    def account(self, offering, user=None):
        offering_user = factories.OfferingUserFactory(
            offering=offering, user=user or self.user
        )
        utils.setup_linux_related_data(offering_user, offering)
        offering_user.save()
        return offering_user

    def override_pool(self, offering):
        return factories.PosixIdPoolFactory(
            offering=offering,
            min_uid=500000,
            max_uid=500099,
            next_uid=500000,
            min_gid=600000,
            max_gid=600099,
            next_gid=600000,
        )

    def test_same_user_shares_one_uid_and_gid_across_the_provider(self):
        account_a = self.account(self.offering_a)
        account_b = self.account(self.offering_b)

        self.assertEqual(
            account_a.backend_metadata["uidnumber"],
            account_b.backend_metadata["uidnumber"],
        )
        self.assertEqual(
            account_a.backend_metadata["primarygroup"],
            account_b.backend_metadata["primarygroup"],
        )
        self.assertEqual(
            models.PosixIdentity.objects.filter(
                user=self.user, released_at__isnull=True
            ).count(),
            1,
        )

    def test_another_user_gets_the_next_value(self):
        first = self.account(self.offering_a)
        second = self.account(self.offering_a, user=structure_factories.UserFactory())
        self.assertEqual(first.backend_metadata["uidnumber"], 100000)
        self.assertEqual(second.backend_metadata["uidnumber"], 100001)

    def test_identity_is_user_scoped_not_consumer_scoped(self):
        self.account(self.offering_a)
        identity = models.PosixIdentity.objects.get(user=self.user)
        self.assertIsNone(identity.content_type_id)
        self.assertIsNone(identity.object_id)

    def test_offering_with_own_pool_gets_its_own_values(self):
        account_a = self.account(self.offering_a)
        self.override_pool(self.offering_b)
        account_b = self.account(self.offering_b)

        self.assertEqual(account_a.backend_metadata["uidnumber"], 100000)
        self.assertEqual(account_b.backend_metadata["uidnumber"], 500000)
        account_a.refresh_from_db()
        self.assertEqual(account_a.backend_metadata["uidnumber"], 100000)

    def test_existing_accounts_are_not_moved_when_an_override_appears(self):
        account_a = self.account(self.offering_a)
        self.override_pool(self.offering_a)
        # A second pass over the same account must not re-point it: only the
        # explicit re-point action moves pre-existing accounts.
        utils.setup_linux_related_data(account_a, self.offering_a)
        self.assertEqual(account_a.backend_metadata["uidnumber"], 100000)

    def test_shared_value_survives_deletion_of_one_account(self):
        self.account(self.offering_a)
        account_b = self.account(self.offering_b)
        account_b.delete()

        identity = models.PosixIdentity.objects.get(user=self.user)
        self.assertIsNone(identity.released_at)
        self.assertEqual(identity.uid, 100000)

    def test_last_account_releases_the_shared_value(self):
        account_a = self.account(self.offering_a)
        account_b = self.account(self.offering_b)
        account_a.delete()
        account_b.delete()

        identity = models.PosixIdentity.objects.get(user=self.user)
        self.assertIsNotNone(identity.released_at)
        self.assertTrue(identity.recyclable)
        # The freed value is offered to the next user.
        newcomer = self.account(self.offering_a, user=structure_factories.UserFactory())
        self.assertEqual(newcomer.backend_metadata["uidnumber"], 100000)

    def test_account_in_another_pool_does_not_hold_the_value(self):
        self.account(self.offering_a)
        self.override_pool(self.offering_b)
        account_b = self.account(self.offering_b)
        # Deleting the provider-pool account releases the provider-pool identity
        # even though the user still has an account elsewhere.
        models.OfferingUser.objects.filter(offering=self.offering_a).delete()

        provider_identity = models.PosixIdentity.objects.get(
            user=self.user, pool=self.pool
        )
        self.assertIsNotNone(provider_identity.released_at)
        override_identity = models.PosixIdentity.objects.get(
            user=self.user, pool=self.offering_b.posix_pool
        )
        self.assertIsNone(override_identity.released_at)
        self.assertEqual(account_b.backend_metadata["uidnumber"], override_identity.uid)

    def test_robot_account_keeps_a_per_account_identity(self):
        resource = factories.ResourceFactory(offering=self.offering_a)
        first = factories.RobotAccountFactory(
            resource=resource, username="robot-1", type="cicd"
        )
        second = factories.RobotAccountFactory(
            resource=resource, username="robot-2", type="bkp"
        )
        utils.setup_linux_related_data(first, self.offering_a)
        utils.setup_linux_related_data(second, self.offering_a)

        self.assertNotEqual(
            first.backend_metadata["uidnumber"], second.backend_metadata["uidnumber"]
        )
        robot_ct = ContentType.objects.get_for_model(models.RobotAccount)
        self.assertEqual(
            models.PosixIdentity.objects.filter(content_type=robot_ct).count(), 2
        )

    def test_manual_override_applies_across_the_pool(self):
        account_a = self.account(self.offering_a)
        account_b = self.account(self.offering_b)
        posix_ids.set_value(account_a, posix_ids.UID, 100050, self.offering_a)

        identity = models.PosixIdentity.objects.get(user=self.user)
        self.assertEqual(identity.uid, 100050)
        # The other account resolves to the same identity, so a re-read of the
        # allocator hands it the pinned value too.
        self.assertEqual(
            posix_ids.allocate(self.offering_b, posix_ids.UID, account_b), 100050
        )

    def test_pool_stats_count_principals(self):
        self.account(self.offering_a)
        self.account(self.offering_b)
        stats = posix_ids.get_pool_stats(self.pool)
        self.assertEqual(stats["uid"]["used"], 1)
        self.assertEqual(stats["gid"]["used"], 1)

    def test_pin_reaches_the_other_accounts_projection(self):
        account_a = self.account(self.offering_a)
        account_b = self.account(self.offering_b)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingUserFactory.get_url(
            account_a, action="set-posix-attributes"
        )
        response = self.client.post(url, {"uidnumber": 100050})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

        account_a.refresh_from_db()
        account_b.refresh_from_db()
        self.assertEqual(account_a.backend_metadata["uidnumber"], 100050)
        # The identity is shared, so the sibling account's projection has to
        # follow it — otherwise its directory entry keeps the stale number.
        self.assertEqual(account_b.backend_metadata["uidnumber"], 100050)

    def test_pin_leaves_an_offering_with_its_own_pool_alone(self):
        account_a = self.account(self.offering_a)
        self.override_pool(self.offering_b)
        account_b = self.account(self.offering_b)

        self.client.force_authenticate(self.fixture.staff)
        url = factories.OfferingUserFactory.get_url(
            account_a, action="set-posix-attributes"
        )
        self.client.post(url, {"uidnumber": 100050})

        account_b.refresh_from_db()
        self.assertEqual(account_b.backend_metadata["uidnumber"], 500000)

    def test_account_on_an_offering_without_posix_does_not_hold_the_value(self):
        self.account(self.offering_a)
        plain_offering = factories.OfferingFactory(
            customer=self.customer,
            name="Object storage",
            plugin_options={"enable_posix_account": False},
        )
        factories.OfferingUserFactory(offering=plain_offering, user=self.user)
        models.OfferingUser.objects.filter(offering=self.offering_a).delete()

        identity = models.PosixIdentity.objects.get(user=self.user)
        self.assertIsNotNone(identity.released_at)

    def test_deleting_the_first_offering_keeps_the_shared_identity(self):
        self.account(self.offering_a)
        account_b = self.account(self.offering_b)
        # offering_a triggered the allocation, but the identity belongs to the
        # user in the pool: account_b still depends on the reservation.
        self.offering_a.delete()

        identity = models.PosixIdentity.objects.get(user=self.user, pool=self.pool)
        self.assertIsNone(identity.released_at)
        self.assertIsNone(identity.offering_id)
        account_b.refresh_from_db()
        self.assertEqual(account_b.backend_metadata["uidnumber"], identity.uid)

    def test_robot_account_can_draw_from_a_second_pool(self):
        # A UID-only provider pool leaves robot accounts without a primary GID;
        # a later GID-managing override must be able to allocate one for them.
        models.PosixIdPool.objects.filter(pk=self.pool.pk).update(
            min_gid=None, max_gid=None, next_gid=None
        )
        resource = factories.ResourceFactory(offering=self.offering_a)
        robot = factories.RobotAccountFactory(resource=resource, username="robot-1")
        utils.setup_linux_related_data(robot, self.offering_a)
        self.assertEqual(robot.backend_metadata["uidnumber"], 100000)

        factories.PosixIdPoolFactory(
            offering=self.offering_a,
            min_uid=None,
            max_uid=None,
            next_uid=None,
            min_gid=600000,
            max_gid=600099,
            next_gid=600000,
        )
        gid = posix_ids.allocate(self.offering_a, posix_ids.GID, robot)
        self.assertEqual(gid, 600000)
