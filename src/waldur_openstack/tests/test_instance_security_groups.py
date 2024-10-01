from unittest.mock import patch

from rest_framework import status, test

from waldur_mastermind.common import utils as common_utils
from waldur_openstack import models, views
from waldur_openstack.tests._instance_data import get_instance_data

from . import factories, fixtures


class InstanceSecurityGroupsTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.admin = self.fixture.admin
        self.client.force_authenticate(self.admin)

        self.security_groups = factories.SecurityGroupFactory.create_batch(
            2, tenant=self.fixture.tenant
        )
        self.instance.security_groups.add(*self.security_groups)

    def create_instance(self, post_data=None):
        user = self.admin
        view = views.MarketplaceInstanceViewSet.as_view({"post": "create"})
        response = common_utils.create_request(view, user, post_data)
        return response

    def test_groups_list_in_instance_response(self):
        response = self.client.get(factories.InstanceFactory.get_url(self.instance))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        fields = ("name",)
        for field in fields:
            expected = sorted([getattr(g, field) for g in self.security_groups])
            actual = sorted([g[field] for g in response.data["security_groups"]])
            self.assertEqual(expected, actual)

    def test_add_instance_with_security_groups(self):
        data = get_instance_data(self.admin, self.instance)
        data["security_groups"] = [
            self._get_valid_security_group_payload(sg) for sg in self.security_groups
        ]

        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_security_groups = list(reread_instance.security_groups.order_by("name"))
        self.assertEqual(reread_security_groups, self.security_groups)

    @patch("waldur_openstack.executors.InstanceUpdateSecurityGroupsExecutor.execute")
    def test_change_instance_security_groups_single_field(self, mocked_execute_method):
        new_security_group = factories.SecurityGroupFactory(
            name="test-group",
            tenant=self.fixture.tenant,
        )

        data = {
            "security_groups": [
                self._get_valid_security_group_payload(new_security_group),
            ]
        }

        response = self.client.post(
            factories.InstanceFactory.get_url(
                self.instance, action="update_security_groups"
            ),
            data=data,
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_security_groups = list(reread_instance.security_groups.all())

        self.assertEqual(
            reread_security_groups,
            [new_security_group],
            "Security groups should have changed",
        )
        mocked_execute_method.assert_called_once()

    @patch("waldur_openstack.executors.InstanceUpdateSecurityGroupsExecutor.execute")
    def test_change_instance_security_groups(self, mocked_execute_method):
        response = self.client.get(factories.InstanceFactory.get_url(self.instance))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        security_group = factories.SecurityGroupFactory(tenant=self.fixture.tenant)
        data = {
            "security_groups": [self._get_valid_security_group_payload(security_group)]
        }

        response = self.client.post(
            factories.InstanceFactory.get_url(
                self.instance, action="update_security_groups"
            ),
            data=data,
        )
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        reread_instance = models.Instance.objects.get(pk=self.instance.pk)
        reread_security_groups = list(reread_instance.security_groups.all())

        self.assertEqual(reread_security_groups, [security_group])
        mocked_execute_method.assert_called_once()

    def test_security_groups_is_not_required(self):
        data = get_instance_data(self.admin, self.instance)
        self.assertNotIn("security_groups", data)
        response = self.create_instance(data)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    # Helper methods
    def _get_valid_security_group_payload(self, security_group=None):
        return {"url": factories.SecurityGroupFactory.get_url(security_group)}
