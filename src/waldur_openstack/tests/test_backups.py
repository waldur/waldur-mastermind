import uuid
from unittest.mock import patch

from ddt import data, ddt
from rest_framework import status, test
from rest_framework.test import APIRequestFactory

from waldur_core.core.enums import CoreStates
from waldur_core.structure.tests import factories as structure_factories
from waldur_openstack import models
from waldur_openstack.serializers import OpenStackBackupRestorationSerializer

from . import factories, fixtures


@ddt
class BackupDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()

    @data("staff", "owner", "manager", "admin")
    def test_user_can_delete_backup(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.BackupFactory.get_url(self.fixture.backup)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_202_ACCEPTED)

    @data("global_support", "customer_support")
    def test_user_can_not_delete_backup(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        url = factories.BackupFactory.get_url(self.fixture.backup)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class BackupListPermissionsTest(test.APITestCase):
    def get_users_and_expected_results(self):
        """
        Return list or generator of dictionaries with such keys:
         - user - user which we want to test
         - expected_results - list of dictionaries with fields which user has
                              to receive as answer from server
        """
        models.Backup.objects.all().delete()
        instance = factories.InstanceFactory()
        backup1 = factories.BackupFactory(instance=instance)
        backup2 = factories.BackupFactory(instance=instance)

        user_with_view_permission = structure_factories.UserFactory.create(
            is_staff=True, is_superuser=True
        )
        user_without_view_permission = structure_factories.UserFactory.create()

        return [
            {
                "user": user_with_view_permission,
                "expected_results": [
                    {"url": factories.BackupFactory.get_url(backup1)},
                    {"url": factories.BackupFactory.get_url(backup2)},
                ],
            },
            {"user": user_without_view_permission, "expected_results": []},
        ]

    def test_list_permissions(self):
        for user_and_expected_result in self.get_users_and_expected_results():
            user = user_and_expected_result["user"]
            expected_results = user_and_expected_result["expected_results"]

            self.client.force_authenticate(user=user)
            response = self.client.get(factories.BackupFactory.get_list_url())
            self.assertEqual(
                len(expected_results),
                len(response.data),
                f"User {user} receive wrong number of objects. Expected: {len(expected_results)}, received {len(response.data)}",
            )
            for actual, expected in zip(response.data, expected_results):
                for key, value in expected.items():
                    self.assertEqual(actual[key], value)


class BackupPermissionsTest(test.APITestCase):
    def setUp(self):
        super().setUp()
        self.fixture = fixtures.OpenStackFixture()
        self.instance = self.fixture.instance
        self.backup = factories.BackupFactory(
            tenant=self.fixture.tenant,
            project=self.fixture.project,
            state=CoreStates.OK,
            instance=self.instance,
        )

    def get_users_with_permission(self, url, method):
        """
        Return list of users which can access given url with given method
        """
        if method == "GET":
            return [self.fixture.staff, self.fixture.admin, self.fixture.manager]
        else:
            return [
                self.fixture.staff,
                self.fixture.admin,
                self.fixture.manager,
                self.fixture.owner,
            ]

    def get_users_without_permissions(self, url, method):
        """
        Return list of users which can not access given url with given method
        """
        return [self.fixture.user]

    def get_urls_configs(self):
        yield {"url": factories.BackupFactory.get_url(self.backup), "method": "GET"}
        yield {"url": factories.BackupFactory.get_url(self.backup), "method": "DELETE"}

    @patch("waldur_openstack.executors.BackupDeleteExecutor.execute")
    def test_permissions(self, mock_execute):
        """
        Go through all url configs ands checks that user with permissions
        can request them and users without - can't
        """
        for conf in self.get_urls_configs():
            url, method = conf["url"], conf["method"]
            data = conf["data"] if "data" in conf else {}

            for user in self.get_users_with_permission(url, method):
                self.client.force_authenticate(user=user)
                response = getattr(self.client, method.lower())(url, data=data)
                self.assertFalse(
                    response.status_code
                    in (status.HTTP_403_FORBIDDEN, status.HTTP_404_NOT_FOUND),
                    f"Error. User {user} can not reach url: {url} (method:{method}). (Response status code {response.status_code}, data {response.data})",
                )

            for user in self.get_users_without_permissions(url, method):
                self.client.force_authenticate(user=user)
                response = getattr(self.client, method.lower())(url, data=data)
                unreachable_statuses = (
                    status.HTTP_403_FORBIDDEN,
                    status.HTTP_404_NOT_FOUND,
                    status.HTTP_409_CONFLICT,
                )
                self.assertTrue(
                    response.status_code in unreachable_statuses,
                    f"Error. User {user} can reach url: {url} (method:{method}). (Response status code {response.status_code}, data {response.data})",
                )


class BackupSourceFilterTest(test.APITestCase):
    def test_filter_backup_by_scope(self):
        user = structure_factories.UserFactory.create(is_staff=True)

        instance1 = factories.InstanceFactory()
        factories.BackupFactory(instance=instance1)
        factories.BackupFactory(instance=instance1)

        instance2 = factories.InstanceFactory()
        factories.BackupFactory(instance=instance2)

        self.client.force_authenticate(user=user)
        response = self.client.get(factories.BackupFactory.get_list_url())
        self.assertEqual(3, len(response.data))

        response = self.client.get(
            factories.BackupFactory.get_list_url(),
            data={"instance_uuid": instance1.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(2, len(response.data))
        self.assertEqual(
            factories.InstanceFactory.get_url(instance1), response.data[0]["instance"]
        )


class BackupRestorationTest(test.APITestCase):
    def setUp(self):
        user = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(user=user)
        self.fixture = fixtures.OpenStackFixture()

        self.backup = self.fixture.backup
        self.backup.state = CoreStates.OK
        self.backup.save()
        self.url = factories.BackupFactory.get_url(self.backup, "restore")

        system_volume = self.backup.instance.volumes.get(bootable=True)
        self.disk_size = system_volume.size

        self.service_settings = self.fixture.settings
        self.service_settings.options = {"external_network_id": uuid.uuid4().hex}
        self.service_settings.save()
        self.tenant = self.fixture.tenant
        self.valid_flavor = self.fixture.flavor
        self.valid_flavor.disk = self.disk_size + 10
        self.valid_flavor.save
        self.subnet = self.fixture.subnet

    def test_instance_should_have_bootable_volume(self):
        self.backup.instance.volumes.filter(bootable=True).delete()
        response = self.client.post(self.url, self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(
            "OpenStack instance should have bootable volume", str(response.data)
        )

    def test_instance_should_have_exactly_one_bootable_volume(self):
        # Add an extra bootable volume to create multiple bootable volumes scenario
        factories.VolumeFactory(
            instance=self.backup.instance,
            bootable=True,
            tenant=self.backup.instance.tenant,
            service_settings=self.backup.instance.service_settings,
            project=self.backup.instance.project,
        )

        # Verify we now have 2 bootable volumes
        bootable_count = self.backup.instance.volumes.filter(bootable=True).count()
        self.assertEqual(bootable_count, 2)

        response = self.client.post(self.url, self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exactly one bootable volume", str(response.data))
        self.assertIn("found 2", str(response.data))

    def test_instance_with_three_bootable_volumes_shows_correct_count(self):
        # Add two extra bootable volumes to create multiple bootable volumes scenario
        for i in range(2):
            factories.VolumeFactory(
                instance=self.backup.instance,
                bootable=True,
                tenant=self.backup.instance.tenant,
                service_settings=self.backup.instance.service_settings,
                project=self.backup.instance.project,
            )

        # Verify we now have 3 bootable volumes (1 original + 2 added)
        bootable_count = self.backup.instance.volumes.filter(bootable=True).count()
        self.assertEqual(bootable_count, 3)

        response = self.client.post(self.url, self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("exactly one bootable volume", str(response.data))
        self.assertIn("found 3", str(response.data))

    def test_flavor_disk_size_should_match_system_volume_size(self):
        response = self.client.post(self.url, self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_restored_volume_inherits_bootable_flag_of_its_source_volume(self):
        # Without the inherited flag the restored instance has no system volume
        # and create_instance fails its `volumes.get(bootable=True)` guard.
        for volume in self.backup.instance.volumes.all():
            self.backup.snapshots.add(
                factories.SnapshotFactory(
                    project=self.fixture.project,
                    tenant=self.tenant,
                    state=CoreStates.OK,
                    source_volume=volume,
                    size=volume.size,
                )
            )

        response = self.client.post(self.url, self._get_valid_payload())

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        restored_instance = models.Instance.objects.get(uuid=response.data["uuid"])
        self.assertEqual(2, restored_instance.volumes.count())
        restored_system_volume = restored_instance.volumes.get(bootable=True)
        self.assertTrue(restored_system_volume.source_snapshot.source_volume.bootable)

    def test_security_groups_cannot_be_associated_if_they_belong_to_another_tenant(
        self,
    ):
        security_group = factories.SecurityGroupFactory()
        self.assertNotEqual(self.backup.tenant, security_group.tenant)
        payload = self._get_valid_payload(
            security_groups=[
                {"url": factories.SecurityGroupFactory.get_url(security_group)}
            ]
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("security_groups", response.data)

    def test_security_group_has_been_associated_with_an_instance(self):
        security_group1 = factories.SecurityGroupFactory(tenant=self.tenant)
        payload = self._get_valid_payload(
            security_groups=[
                {"url": factories.SecurityGroupFactory.get_url(security_group1)}
            ]
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data["security_groups"])
        self.assertEqual(
            response.data["security_groups"][0]["name"], security_group1.name
        )

    def test_floating_ip_is_not_associated_with_an_instance_if_it_is_booked_already(
        self,
    ):
        floating_ip = factories.FloatingIPFactory(tenant=self.tenant)
        subnet = factories.SubNetFactory(tenant=self.tenant)
        payload = self._get_valid_payload(
            floating_ips=[
                {
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                    "subnet": factories.SubNetFactory.get_url(subnet),
                }
            ],
            ports=[{"subnet": factories.SubNetFactory.get_url(subnet)}],
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("floating_ips", response.data)

    def test_floating_ip_is_not_associated_with_an_instance_if_it_belongs_to_different_tenant(
        self,
    ):
        floating_ip = factories.FloatingIPFactory()
        self.assertNotEqual(self.tenant, floating_ip.tenant)
        subnet = factories.SubNetFactory(tenant=self.tenant)
        payload = self._get_valid_payload(
            floating_ips=[
                {
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                    "subnet": factories.SubNetFactory.get_url(subnet),
                }
            ],
            ports=[{"subnet": factories.SubNetFactory.get_url(subnet)}],
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("floating_ips", response.data)

    def test_floating_ip_is_associated_with_an_instance_if_floating_ip_is_OK(
        self,
    ):
        floating_ip = self.fixture.floating_ip
        floating_ip.state = CoreStates.OK
        floating_ip.save()
        payload = self._get_valid_payload(
            floating_ips=[
                {
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                    "subnet": factories.SubNetFactory.get_url(self.subnet),
                }
            ],
            ports=[{"subnet": factories.SubNetFactory.get_url(self.subnet)}],
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertIn("floating_ips", response.data)

    def test_floating_ip_is_not_valid_if_it_is_already_assigned(self):
        subnet = factories.SubNetFactory(tenant=self.tenant)
        port = factories.PortFactory(subnet=subnet)
        floating_ip = factories.FloatingIPFactory(
            port=port,
            tenant=self.tenant,
        )

        payload = self._get_valid_payload(
            floating_ips=[
                {
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                    "subnet": factories.SubNetFactory.get_url(subnet),
                }
            ],
            ports=[{"subnet": factories.SubNetFactory.get_url(subnet)}],
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("floating_ips", response.data)

    def test_floating_ip_is_not_associated_with_an_instance_if_subnet_is_not_connected_to_the_instance(
        self,
    ):
        floating_ip = factories.FloatingIPFactory(tenant=self.tenant)
        subnet = factories.SubNetFactory(tenant=self.tenant)
        payload = self._get_valid_payload(
            floating_ips=[
                {
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                    "subnet": factories.SubNetFactory.get_url(subnet),
                }
            ]
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("floating_ips", response.data)

    def test_floating_ip_is_associated_with_an_instance(self):
        floating_ip = factories.FloatingIPFactory(
            tenant=self.tenant,
            state=CoreStates.OK,
        )
        payload = self._get_valid_payload(
            floating_ips=[
                {
                    "url": factories.FloatingIPFactory.get_url(floating_ip),
                    "subnet": factories.SubNetFactory.get_url(self.subnet),
                }
            ],
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIsNotNone(response.data["floating_ips"])
        self.assertEqual(response.data["floating_ips"][0]["uuid"], floating_ip.uuid.hex)
        instance = models.Instance.objects.get(name=payload["name"])
        self.assertEqual(instance.floating_ips.count(), 1)
        self.assertEqual(instance.floating_ips.first().uuid.hex, floating_ip.uuid.hex)

    def test_ports_are_not_associated_with_instance_if_subnet_belongs_to_another_settings(
        self,
    ):
        subnet = factories.SubNetFactory()
        payload = self._get_valid_payload(
            ports=[{"subnet": factories.SubNetFactory.get_url(subnet)}]
        )

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("ports", response.data)

    def test_ports_have_been_associated_with_instance(self):
        payload = self._get_valid_payload()

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        instance = models.Instance.objects.get(name=payload["name"])
        self.assertEqual(instance.ports.count(), 1)
        self.assertEqual(instance.subnets.count(), 1)
        self.assertEqual(instance.subnets.first().uuid.hex, self.subnet.uuid.hex)
        self.assertEqual(instance.flavor_name, self.valid_flavor.name)

    def test_backup_can_be_restored_for_instance_with_1_volume(self):
        self.backup.instance.volumes.get(bootable=False).delete()
        payload = self._get_valid_payload()

        response = self.client.post(self.url, payload)

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(
            models.BackupRestoration.objects.filter(
                instance__name=payload["name"]
            ).exists
        )

    def test_backup_restoration_serializer_includes_instance_floating_ips(self):
        """Test that BackupRestoration serializer can access floating_ips from the instance."""
        # Create a floating IP attached to the instance
        floating_ip = factories.FloatingIPFactory(
            tenant=self.backup.instance.tenant,
            state=CoreStates.OK,
        )
        port = factories.PortFactory(
            instance=self.backup.instance,
            tenant=self.backup.instance.tenant,
        )
        floating_ip.port = port
        floating_ip.save()

        # Create a backup restoration
        backup_restoration = models.BackupRestoration.objects.create(
            backup=self.backup,
            instance=self.backup.instance,
            flavor=self.valid_flavor,
        )

        # Test that the serializer can access floating_ips from the instance
        request_factory = APIRequestFactory()
        request = request_factory.get("/")
        serializer = OpenStackBackupRestorationSerializer(
            backup_restoration, context={"request": request}
        )
        serialized_data = serializer.data

        self.assertIn("floating_ips", serialized_data)
        self.assertEqual(len(serialized_data["floating_ips"]), 1)
        self.assertEqual(
            serialized_data["floating_ips"][0]["uuid"], floating_ip.uuid.hex
        )

    def test_backup_restoration_serializer_includes_instance_security_groups(self):
        """Test that BackupRestoration serializer can access security_groups from the instance."""
        # Create a security group attached to the instance
        security_group = factories.SecurityGroupFactory(
            tenant=self.backup.instance.tenant
        )
        self.backup.instance.security_groups.add(security_group)

        # Create a backup restoration
        backup_restoration = models.BackupRestoration.objects.create(
            backup=self.backup,
            instance=self.backup.instance,
            flavor=self.valid_flavor,
        )

        # Test that the serializer can access security_groups from the instance
        request_factory = APIRequestFactory()
        request = request_factory.get("/")
        serializer = OpenStackBackupRestorationSerializer(
            backup_restoration, context={"request": request}
        )
        serialized_data = serializer.data

        self.assertIn("security_groups", serialized_data)
        self.assertEqual(len(serialized_data["security_groups"]), 1)
        self.assertEqual(
            serialized_data["security_groups"][0]["name"], security_group.name
        )

    def test_backup_restoration_serializer_includes_instance_ports(self):
        """Test that BackupRestoration serializer can access ports from the instance."""
        # Create a port attached to the instance
        port = factories.PortFactory(
            instance=self.backup.instance,
            tenant=self.backup.instance.tenant,
        )

        # Create a backup restoration
        backup_restoration = models.BackupRestoration.objects.create(
            backup=self.backup,
            instance=self.backup.instance,
            flavor=self.valid_flavor,
        )

        # Test that the serializer can access ports from the instance
        request_factory = APIRequestFactory()
        request = request_factory.get("/")
        serializer = OpenStackBackupRestorationSerializer(
            backup_restoration, context={"request": request}
        )
        serialized_data = serializer.data

        self.assertIn("ports", serialized_data)
        # Instance should have at least one port (the one we created)
        self.assertGreaterEqual(len(serialized_data["ports"]), 1)
        # Check that the port URL contains the port UUID since the nested serializer uses URL field
        port_urls = [p["url"] for p in serialized_data["ports"]]
        self.assertTrue(any(port.uuid.hex in url for url in port_urls))

    def _get_valid_payload(self, **options):
        payload = {
            "name": "instance name",
            "flavor": factories.FlavorFactory.get_url(self.valid_flavor),
            "ports": [{"subnet": factories.SubNetFactory.get_url(self.subnet)}],
        }
        payload.update(options)
        return payload
