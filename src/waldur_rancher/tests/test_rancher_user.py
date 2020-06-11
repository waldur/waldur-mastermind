from rest_framework import status, test

from . import factories, fixtures


class RancherUserGetTest(test.APITransactionTestCase):
    def setUp(self):
        super(RancherUserGetTest, self).setUp()
        self.fixture = fixtures.RancherFixture()
        self.fixture_2 = fixtures.RancherFixture()
        self.url = factories.RancherUserFactory.get_list_url()
        rancher_user_1 = factories.RancherUserFactory(
            user=self.fixture.owner, settings=self.fixture.settings
        )
        factories.RancherUserClusterLinkFactory(
            user=rancher_user_1, cluster=self.fixture.cluster
        )
        rancher_user_2 = factories.RancherUserFactory(
            user=self.fixture_2.owner, settings=self.fixture_2.settings
        )
        factories.RancherUserClusterLinkFactory(
            user=rancher_user_2, cluster=self.fixture_2.cluster
        )

    def test_get_rancher_user_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 2)

    def test_user_cannot_get_strangers_rancher_users(self):
        self.client.force_authenticate(self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(list(response.data)), 1)
