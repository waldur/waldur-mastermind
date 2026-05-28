from unittest.mock import patch

from ddt import data, ddt
from rest_framework import status, test

from waldur_core.core.enums import CoreStates
from waldur_openstack import models

from . import factories, fixtures


class BaseSecurityGroupTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()


@ddt
class SecurityGroupCreateTest(BaseSecurityGroupTest):
    def setUp(self):
        super().setUp()
        self.valid_data = {
            "name": "https",
            "rules": [
                {
                    "protocol": "tcp",
                    "from_port": 100,
                    "to_port": 8001,
                    "cidr": "11.11.1.2/24",
                }
            ],
        }
        self.url = factories.TenantFactory.get_url(
            self.fixture.tenant, "create_security_group"
        )

    @data("staff", "owner", "admin", "manager")
    def test_user_with_access_can_create_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.post(self.url, data=self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.SecurityGroup.objects.count(), 1)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 1)

    def test_security_group_name_should_be_unique(self):
        self.client.force_authenticate(self.fixture.admin)
        payload = self.valid_data
        payload["name"] = self.fixture.security_group.name
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_security_group_can_not_be_created_if_quota_is_over_limit(self):
        self.fixture.tenant.set_quota_limit("security_group_count", 0)

        self.client.force_authenticate(self.fixture.admin)
        response = self.client.post(self.url, self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.SecurityGroup.objects.filter(name=self.valid_data["name"]).exists()
        )

    def test_security_group_quota_increases_on_security_group_creation(self):
        self.client.force_authenticate(self.fixture.admin)
        response = self.client.post(self.url, self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(self.fixture.tenant.get_quota_usage("security_group_count"), 1)
        self.assertEqual(
            self.fixture.tenant.get_quota_usage("security_group_rule_count"), 1
        )

    def test_security_group_can_not_be_created_if_rules_quota_is_over_limit(self):
        self.fixture.tenant.set_quota_limit("security_group_rule_count", 0)

        self.client.force_authenticate(self.fixture.admin)
        response = self.client.post(self.url, self.valid_data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertFalse(
            models.SecurityGroup.objects.filter(name=self.valid_data["name"]).exists()
        )

    def test_security_group_creation_starts_sync_task(self):
        self.client.force_authenticate(self.fixture.admin)

        with patch(
            "waldur_openstack.executors.SecurityGroupCreateExecutor.execute"
        ) as mocked_execute:
            response = self.client.post(self.url, data=self.valid_data)

            self.assertEqual(
                response.status_code, status.HTTP_201_CREATED, response.data
            )
            security_group = models.SecurityGroup.objects.get(
                name=self.valid_data["name"]
            )

            mocked_execute.assert_called_once_with(security_group)

    def test_user_can_create_security_group_rule_for_any_protocol(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "allow-all",
                "rules": [
                    {
                        "protocol": "",
                        "from_port": -1,
                        "to_port": -1,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.SecurityGroup.objects.count(), 1)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 1)

    def test_user_can_not_create_security_group_rule_for_any_protocol_with_non_null_range(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "allow-all",
                "rules": [
                    {
                        "protocol": "",
                        "from_port": 80,
                        "to_port": 80,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_can_create_security_group_rule_for_tcp_protocol_with_any_range(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "allow-all",
                "rules": [
                    {
                        "protocol": "tcp",
                        "from_port": -1,
                        "to_port": -1,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_can_not_create_security_group_with_invalid_protocol(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {
            "name": "https",
            "rules": [
                {
                    "protocol": "invalid",
                    "from_port": 100,
                    "to_port": 8001,
                    "cidr": "11.11.1.2/24",
                }
            ],
        }
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)

    def test_user_can_create_security_group_rule_for_numeric_protocol(self):
        # IANA protocol number 112 (VRRP) — required for HA load-balancer VIP.
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "vrrp",
                "rules": [
                    {
                        "protocol": "112",
                        "from_port": -1,
                        "to_port": -1,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(models.SecurityGroup.objects.count(), 1)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 1)
        self.assertEqual(models.SecurityGroupRule.objects.get().protocol, "112")

    def test_can_not_create_security_group_rule_for_numeric_protocol_with_port_range(
        self,
    ):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "vrrp",
                "rules": [
                    {
                        "protocol": "112",
                        "from_port": 80,
                        "to_port": 80,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)

    def test_can_not_create_security_group_rule_with_out_of_range_protocol(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "bad",
                "rules": [
                    {
                        "protocol": "999",
                        "from_port": -1,
                        "to_port": -1,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)

    def test_can_not_create_security_group_rule_with_negative_protocol(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "bad",
                "rules": [
                    {
                        "protocol": "-1",
                        "from_port": -1,
                        "to_port": -1,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)

    def test_can_not_create_security_group_with_invalid_port(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {
            "name": "https",
            "rules": [
                {
                    "protocol": "icmp",
                    "from_port": 8001,
                    "to_port": 8001,
                    "cidr": "11.11.1.2/24",
                }
            ],
        }
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)

    def test_can_not_create_security_group_with_invalid_cidr(self):
        self.client.force_authenticate(self.fixture.staff)

        data = {
            "name": "https",
            "rules": [
                {
                    "protocol": "tcp",
                    "from_port": 8001,
                    "to_port": 8001,
                    "cidr": "300.300.300.300/100",
                }
            ],
        }
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)

    def test_can_not_create_security_group_with_duplicate_rules(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data={
                "name": "https",
                "rules": [
                    {
                        "direction": "ingress",
                        "protocol": "tcp",
                        "from_port": 8001,
                        "to_port": 8001,
                        "cidr": "1.1.1.1/1",
                        "remote_group": "https://example.com/api/openstack-security-groups/45754c360acd4982b79aa6830c9e86cc/",
                    },
                    {
                        "direction": "ingress",
                        "protocol": "tcp",
                        "from_port": 8001,
                        "to_port": 8001,
                        "cidr": "1.1.1.1/1",
                        "remote_group": "https://example.com/api/openstack-security-groups/45754c360acd4982b79aa6830c9e86cc/",
                    },
                ],
            },
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroup.objects.count(), 0)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)


@ddt
class SecurityGroupUpdateTest(BaseSecurityGroupTest):
    def setUp(self):
        super().setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        self.url = factories.SecurityGroupFactory.get_url(self.security_group)

    @data("staff", "owner", "admin", "manager")
    def test_user_with_access_can_update_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        data = {"name": "new_name"}
        response = self.client.patch(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.security_group.refresh_from_db()
        self.assertEqual(self.security_group.name, data["name"])

    @data("user")
    def test_user_without_access_cannot_update_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.patch(self.url, data={"name": "new_name"})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_security_group_can_not_be_updated_in_unstable_state(self):
        self.client.force_authenticate(self.fixture.admin)
        self.security_group.state = CoreStates.ERRED
        self.security_group.save()

        response = self.client.patch(self.url, data={"name": "new_name"})

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data("patch", "put")
    def test_default_security_group_name_can_not_be_updated(self, method):
        self.client.force_authenticate(self.fixture.staff)
        self.security_group.name = "default"
        self.security_group.save()

        update = getattr(self.client, method)
        response = update(self.url, data={"name": "new_name"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("patch", "put")
    def test_security_group_name_can_not_become_default(self, method):
        self.client.force_authenticate(self.fixture.staff)
        self.security_group.name = "ssh"
        self.security_group.save()

        update = getattr(self.client, method)
        response = update(self.url, data={"name": "default"})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertTrue("name" in response.data)

    def test_security_group_name_should_be_unique(self):
        existing_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, data={"name": existing_group.name})

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class SecurityGroupSetRulesTest(BaseSecurityGroupTest):
    def setUp(self):
        super().setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        self.url = factories.SecurityGroupFactory.get_url(
            self.security_group, action="set_rules"
        )

    def test_security_group_rules_can_not_be_added_if_quota_is_over_limit(self):
        self.client.force_authenticate(self.fixture.admin)
        self.fixture.tenant.set_quota_limit("security_group_rule_count", 0)

        data = [
            {
                "protocol": "udp",
                "from_port": 100,
                "to_port": 8001,
                "cidr": "11.11.1.2/24",
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.security_group.refresh_from_db()
        self.assertEqual(self.security_group.rules.count(), 0)

    def test_security_group_update_starts_calls_executor(self):
        self.client.force_authenticate(self.fixture.admin)

        execute_method = (
            "waldur_openstack.executors.PushSecurityGroupRulesExecutor.execute"
        )
        with patch(execute_method) as mocked_execute:
            response = self.client.post(self.url, data=[])

            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
            mocked_execute.assert_called_once_with(self.security_group)

    def test_user_can_remove_rule_from_security_group(self):
        rule_to_remain = factories.SecurityGroupRuleFactory(
            security_group=self.security_group
        )
        rule_to_delete = factories.SecurityGroupRuleFactory(
            security_group=self.security_group
        )
        self.client.force_authenticate(self.fixture.admin)

        response = self.client.post(self.url, data=[{"id": rule_to_remain.id}])

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        exist_rules = self.security_group.rules.all()
        self.assertIn(rule_to_remain, exist_rules)
        self.assertNotIn(rule_to_delete, exist_rules)

    def test_user_can_add_new_security_group_rule_and_leave_existing(self):
        exist_rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group
        )
        self.client.force_authenticate(self.fixture.admin)
        new_rule_data = {
            "protocol": "udp",
            "from_port": 100,
            "to_port": 8001,
            "cidr": "11.11.1.2/24",
        }

        response = self.client.post(
            self.url, data=[{"id": exist_rule.id}, new_rule_data]
        )

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertEqual(self.security_group.rules.count(), 2)
        self.assertTrue(self.security_group.rules.filter(id=exist_rule.id).exists())
        self.assertTrue(self.security_group.rules.filter(**new_rule_data).exists())

    def test_rule_is_not_created_if_port_range_is_invalid(self):
        self.client.force_authenticate(self.fixture.admin)

        data = [
            {
                "protocol": "udp",
                "from_port": 125,
                "to_port": 25,
                "cidr": "11.11.1.2/24",
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.security_group.refresh_from_db()
        self.assertEqual(self.security_group.rules.count(), 0)

    def test_rule_is_not_updated_if_port_range_is_invalid(self):
        rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group, from_port=2222, to_port=2222
        )
        self.client.force_authenticate(self.fixture.admin)

        data = [
            {
                "protocol": rule.protocol,
                "from_port": 125,
                "to_port": 25,
                "cidr": rule.cidr,
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        rule.refresh_from_db()
        self.assertEqual(rule.from_port, 2222)
        self.assertEqual(rule.to_port, 2222)

    def test_rule_is_updated_if_port_range_is_valid(self):
        rule = factories.SecurityGroupRuleFactory(
            security_group=self.security_group, from_port=2222, to_port=2222
        )
        self.client.force_authenticate(self.fixture.admin)

        data = [
            {
                "id": rule.id,
                "protocol": rule.protocol,
                "from_port": 125,
                "to_port": 225,
                "cidr": rule.cidr,
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        rule.refresh_from_db()
        self.assertEqual(rule.from_port, 125)
        self.assertEqual(rule.to_port, 225)

    def test_can_not_update_security_group_with_duplicate_rules(self):
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            self.url,
            data=[
                {
                    "protocol": "tcp",
                    "from_port": 8001,
                    "to_port": 8001,
                    "cidr": "1.1.1.1/1",
                },
                {
                    "protocol": "tcp",
                    "from_port": 8001,
                    "to_port": 8001,
                    "cidr": "1.1.1.1/1",
                },
            ],
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.SecurityGroupRule.objects.count(), 0)

    def test_user_can_add_rule_with_remote_group_as_url(self):
        self.client.force_authenticate(self.fixture.admin)

        remote_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        remote_group_url = factories.SecurityGroupFactory.get_url(remote_group)

        data = [
            {
                "protocol": "tcp",
                "from_port": 1,
                "to_port": 65535,
                "remote_group": remote_group_url,
            }
        ]
        response = self.client.post(self.url, data=data)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED, response.data)
        self.assertEqual(self.security_group.rules.count(), 1)
        created_rule = self.security_group.rules.first()
        self.assertEqual(created_rule.remote_group, remote_group)


@ddt
class SecurityGroupDeleteTest(BaseSecurityGroupTest):
    def setUp(self):
        super().setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
            state=CoreStates.OK,
        )
        self.url = factories.SecurityGroupFactory.get_url(self.security_group)

    @data("admin", "manager", "staff", "owner")
    def test_project_administrator_can_delete_security_group(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        with patch(
            "waldur_openstack.executors.SecurityGroupDeleteExecutor.execute"
        ) as mocked_execute:
            response = self.client.delete(self.url)
            self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

            mocked_execute.assert_called_once_with(
                self.security_group, force=False, is_async=True
            )

    def test_security_group_can_be_deleted_from_erred_state(self):
        self.security_group.state = CoreStates.ERRED
        self.security_group.save()

        self.client.force_authenticate(self.fixture.admin)
        response = self.client.delete(self.url)

        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    def test_default_security_group_name_can_not_be_deleted(self):
        self.client.force_authenticate(self.fixture.staff)
        self.security_group.name = "default"
        self.security_group.save()

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class SecurityGroupRetrieveTest(BaseSecurityGroupTest):
    def setUp(self):
        super().setUp()
        self.security_group = factories.SecurityGroupFactory(
            service_settings=self.fixture.settings,
            project=self.fixture.project,
            tenant=self.fixture.tenant,
        )
        self.url = factories.SecurityGroupFactory.get_url(self.security_group)

    @data("admin", "manager", "staff", "owner")
    def test_user_can_access_security_groups_of_project_instances_he_has_role_in(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("user")
    def test_user_cannot_access_security_groups_of_instances_not_connected_to_him(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class TenantPushSecurityGroupsTest(BaseSecurityGroupTest):
    def setUp(self):
        super().setUp()
        self.tenant = self.fixture.tenant
        self.tenant.state = CoreStates.OK
        self.tenant.save()
        self.url = factories.TenantFactory.get_url(
            self.tenant, action="push_security_groups"
        )
        self.client.force_authenticate(self.fixture.admin)

    @patch("waldur_openstack.executors.TenantPushSecurityGroupsExecutor.execute")
    def test_create_new_security_group(self, mock_executor):
        payload = [{"name": "new-sg", "description": "New SG"}]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertTrue(self.tenant.security_groups.filter(name="new-sg").exists())
        mock_executor.assert_called_once_with(self.tenant)

    @patch("waldur_openstack.executors.TenantPushSecurityGroupsExecutor.execute")
    def test_delete_existing_security_group(self, mock_executor):
        sg_to_delete = factories.SecurityGroupFactory(tenant=self.tenant)
        payload = []
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        self.assertFalse(
            self.tenant.security_groups.filter(id=sg_to_delete.id).exists()
        )
        mock_executor.assert_called_once_with(self.tenant)

    @patch("waldur_openstack.executors.TenantPushSecurityGroupsExecutor.execute")
    def test_update_existing_security_group(self, mock_executor):
        sg_to_update = factories.SecurityGroupFactory(tenant=self.tenant)
        payload = [
            {
                "uuid": sg_to_update.uuid.hex,
                "name": "updated-name",
                "description": "updated-desc",
            }
        ]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        sg_to_update.refresh_from_db()
        self.assertEqual(sg_to_update.name, "updated-name")
        self.assertEqual(sg_to_update.description, "updated-desc")
        mock_executor.assert_called_once_with(self.tenant)

    @patch("waldur_openstack.executors.TenantPushSecurityGroupsExecutor.execute")
    def test_update_rules_for_existing_group(self, mock_executor):
        sg_to_update = factories.SecurityGroupFactory(tenant=self.tenant)
        factories.SecurityGroupRuleFactory(security_group=sg_to_update)
        self.assertEqual(sg_to_update.rules.count(), 1)

        payload = [
            {
                "uuid": sg_to_update.uuid.hex,
                "name": sg_to_update.name,
                "rules": [
                    {
                        "protocol": "tcp",
                        "from_port": 80,
                        "to_port": 80,
                        "cidr": "0.0.0.0/0",
                    }
                ],
            }
        ]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)
        sg_to_update.refresh_from_db()
        self.assertEqual(sg_to_update.rules.count(), 1)
        self.assertEqual(sg_to_update.rules.first().from_port, 80)
        mock_executor.assert_called_once_with(self.tenant)

    @patch("waldur_openstack.executors.TenantPushSecurityGroupsExecutor.execute")
    def test_mixed_operation(self, mock_executor):
        sg_to_delete = factories.SecurityGroupFactory(tenant=self.tenant, name="delete")
        sg_to_update = factories.SecurityGroupFactory(tenant=self.tenant, name="update")

        payload = [
            {
                "uuid": sg_to_update.uuid.hex,
                "name": "updated-name",
            },
            {"name": "new-sg"},
        ]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        self.assertFalse(
            self.tenant.security_groups.filter(id=sg_to_delete.id).exists()
        )
        self.assertTrue(
            self.tenant.security_groups.filter(name="updated-name").exists()
        )
        self.assertTrue(self.tenant.security_groups.filter(name="new-sg").exists())

        mock_executor.assert_called_once_with(self.tenant)

    def test_create_group_with_remote_group_rule_by_name(self):
        payload = [
            {
                "name": "sg-A",
                "rules": [
                    {
                        "protocol": "tcp",
                        "from_port": 1,
                        "to_port": 1,
                        "remote_group_name": "sg-B",
                    }
                ],
            },
            {"name": "sg-B"},
        ]
        response = self.client.post(self.url, payload)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

        sg_a = self.tenant.security_groups.get(name="sg-A")
        sg_b = self.tenant.security_groups.get(name="sg-B")

        self.assertEqual(sg_a.rules.count(), 1)
        self.assertEqual(sg_a.rules.first().remote_group, sg_b)
