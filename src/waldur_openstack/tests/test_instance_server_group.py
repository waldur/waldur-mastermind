from rest_framework import status, test

from waldur_mastermind.common import utils as common_utils
from waldur_openstack import models, views
from waldur_openstack.tests._instance_data import get_instance_data

from . import factories, fixtures


class InstanceServerGroupTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.admin = self.fixture.admin
        self.client.force_authenticate(self.admin)

        self.server_group = self.fixture.server_group
        self.instance.server_group = self.server_group
        self.instance.save()

    def create_instance(self, post_data=None):
        user = self.admin
        view = views.MarketplaceInstanceViewSet.as_view({"post": "create"})
        response = common_utils.create_request(view, user, post_data)
        return response

    def test_server_group_in_instance_response(self):
        response = self.client.get(factories.InstanceFactory.get_url(self.instance))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        expected = getattr(self.server_group, "name")
        actual = response.data["server_group"]["name"]
        self.assertEqual(expected, actual)

    def test_add_instance_with_server_group(self):
        data = get_instance_data(self.fixture)
        data["server_group"] = self._get_valid_server_group_payload(self.server_group)

        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_server_group = reread_instance.server_group
        self.assertEqual(reread_server_group, self.server_group)

    def test_server_group_is_not_required(self):
        data = get_instance_data(self.fixture)
        self.assertNotIn("server_group", data)
        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def _get_valid_server_group_payload(self, server_group=None):
        return {"url": factories.ServerGroupFactory.get_url(server_group)}
