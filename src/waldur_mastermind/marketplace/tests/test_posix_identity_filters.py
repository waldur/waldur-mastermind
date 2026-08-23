"""Server-side narrowing of the POSIX identity audit list.

A pool covers a whole numeric range and the list is paginated, so filtering has
to happen in the query: narrowing the page the client happens to hold would
report "not found" for a value that exists further down the range.
"""

from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.tests import factories


class PosixIdentityFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.service_provider = factories.ServiceProviderFactory(customer=self.customer)
        self.pool = factories.PosixIdPoolFactory(
            service_provider=self.service_provider,
            min_uid=100000,
            max_uid=100999,
            next_uid=100000,
            min_gid=200000,
            max_gid=200999,
            next_gid=200000,
        )
        self.offering = factories.OfferingFactory(
            customer=self.customer, name="Cluster A"
        )
        self.alice = structure_factories.UserFactory(
            username="alice", first_name="Alice", last_name="Anderson"
        )
        self.bob = structure_factories.UserFactory(
            username="bob", first_name="Bob", last_name="Brown"
        )
        self.alice_identity = self.identity(user=self.alice, uid=100000, gid=200000)
        self.bob_identity = self.identity(user=self.bob, uid=100500, gid=200500)

    def identity(self, **kwargs):
        return models.PosixIdentity.objects.create(
            pool=self.pool, offering=self.offering, **kwargs
        )

    def robot_identity(self, username, uid):
        resource = factories.ResourceFactory(offering=self.offering)
        robot = factories.RobotAccountFactory(resource=resource, username=username)
        return self.identity(consumer=robot, uid=uid)

    def get(self, **params):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.PosixIdentityFactory.get_list_url(), params
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        return response

    def uids(self, response):
        return [row["uid"] for row in response.data]

    def test_keyword_matches_the_account_username(self):
        self.assertEqual(self.uids(self.get(keyword="alice")), [100000])

    def test_keyword_matches_the_users_name(self):
        self.assertEqual(self.uids(self.get(keyword="Anderson")), [100000])

    def test_keyword_matches_a_robot_account(self):
        self.robot_identity("backup-runner", 100700)
        self.assertEqual(self.uids(self.get(keyword="backup")), [100700])

    def test_keyword_does_not_match_unrelated_rows(self):
        self.assertEqual(self.get(keyword="carol").data, [])

    def test_exact_uid(self):
        self.assertEqual(self.uids(self.get(uid=100500)), [100500])

    def test_uid_band(self):
        self.identity(user=structure_factories.UserFactory(), uid=100900, gid=200900)
        self.assertEqual(
            sorted(self.uids(self.get(uid_min=100400, uid_max=100600))), [100500]
        )

    def test_gid_band(self):
        self.assertEqual(sorted(self.uids(self.get(gid_min=200400))), [100500])

    def test_consumer_type_user_excludes_robot_accounts(self):
        self.robot_identity("robot-1", 100700)
        self.assertEqual(
            sorted(self.uids(self.get(consumer_type="user"))), [100000, 100500]
        )

    def test_consumer_type_robot_account(self):
        self.robot_identity("robot-1", 100700)
        self.assertEqual(self.uids(self.get(consumer_type="robotaccount")), [100700])

    def test_recyclable_flag_isolates_withheld_values(self):
        withheld = self.identity(
            user=structure_factories.UserFactory(), uid=100800, gid=200800
        )
        models.PosixIdentity.objects.filter(pk=withheld.pk).update(
            released_at="2026-01-01T00:00:00Z", recyclable=False
        )
        self.assertEqual(self.uids(self.get(recyclable=False)), [100800])

    def test_ordering_by_uid_descending(self):
        self.assertEqual(self.uids(self.get(o="-uid")), [100500, 100000])

    def test_filtering_reaches_beyond_the_first_page(self):
        # The point of server-side filtering: the match sits outside the page the
        # client would hold, so a client-side filter would miss it.
        for offset in range(1, 30):
            self.identity(
                user=structure_factories.UserFactory(),
                uid=100000 + offset,
                gid=200000 + offset,
            )
        carol = structure_factories.UserFactory(username="carol")
        self.identity(user=carol, uid=100950, gid=200950)

        first_page = self.get(page_size=10)
        self.assertNotIn(100950, self.uids(first_page))
        self.assertEqual(self.uids(self.get(keyword="carol")), [100950])
