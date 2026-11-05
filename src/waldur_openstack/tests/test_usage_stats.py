from rest_framework import status, test

from waldur_openstack import models

from . import factories, fixtures


def pluck(fields, row):
    return {field: row[field] for field in fields}


def clean_row(row):
    return pluck(("name", "running_instances_count", "created_instances_count"), row)


def clean_rows(rows):
    return sorted(map(clean_row, rows), key=lambda k: k["name"])


class TestImageUsageStats(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.admin = self.fixture.staff
        factories.InstanceFactory(
            volumes__image_name="Ubuntu 16.04",
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        factories.InstanceFactory(
            volumes__image_name="Ubuntu 16.04",
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        factories.InstanceFactory(
            volumes__image_name="Windows 10",
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        factories.ImageFactory(
            name="Ubuntu 16.04", settings=self.fixture.tenant.service_settings
        )
        factories.ImageFactory(
            name="CentOS 10.04", settings=self.fixture.tenant.service_settings
        )
        factories.ImageFactory(
            name="Windows 10", settings=self.fixture.tenant.service_settings
        )
        factories.ImageFactory(
            name="Windows 10", settings=self.fixture.tenant.service_settings
        )

    def test_usage_stats(self):
        expected = [
            {
                "name": "CentOS 10.04",
                "running_instances_count": 0,
                "created_instances_count": 0,
            },
            {
                "name": "Windows 10",
                "running_instances_count": 1,
                "created_instances_count": 1,
            },
            {
                "name": "Ubuntu 16.04",
                "running_instances_count": 1,
                "created_instances_count": 2,
            },
        ]
        self.client.force_authenticate(user=self.admin)

        url = factories.ImageFactory.get_list_url(action="usage_stats")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertListEqual(clean_rows(expected), clean_rows(response.data))


class TestFlavorUsageStats(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        self.admin = self.fixture.staff
        factories.InstanceFactory(
            flavor_name="Small",
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        factories.InstanceFactory(
            flavor_name="Small",
            runtime_state=models.Instance.RuntimeStates.SHUTOFF,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        factories.InstanceFactory(
            flavor_name="Large",
            runtime_state=models.Instance.RuntimeStates.ACTIVE,
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )

        factories.FlavorFactory(
            name="Small", settings=self.fixture.tenant.service_settings
        )
        factories.FlavorFactory(
            name="Medium", settings=self.fixture.tenant.service_settings
        )
        factories.FlavorFactory(
            name="Large", settings=self.fixture.tenant.service_settings
        )
        factories.FlavorFactory(
            name="Large", settings=self.fixture.tenant.service_settings
        )

    def test_usage_stats(self):
        expected = [
            {
                "running_instances_count": 1,
                "created_instances_count": 1,
                "name": "Large",
            },
            {
                "running_instances_count": 0,
                "created_instances_count": 0,
                "name": "Medium",
            },
            {
                "running_instances_count": 1,
                "created_instances_count": 2,
                "name": "Small",
            },
        ]
        self.client.force_authenticate(user=self.admin)

        url = factories.FlavorFactory.get_list_url(action="usage_stats")
        response = self.client.get(url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertListEqual(clean_rows(expected), clean_rows(response.data))


class UsageStatsAccessTest(test.APITestCase):
    """The reports count every cloud's instances: staff and support only (#1064)."""

    def setUp(self):
        self.fixture = fixtures.OpenStackFixture()
        factories.InstanceFactory(
            flavor_name="Small",
            volumes__image_name="Ubuntu 16.04",
            tenant=self.fixture.tenant,
            project=self.fixture.project,
        )
        factories.FlavorFactory(
            name="Small", settings=self.fixture.tenant.service_settings
        )
        factories.ImageFactory(
            name="Ubuntu 16.04", settings=self.fixture.tenant.service_settings
        )
        self.urls = [
            factories.FlavorFactory.get_list_url(action="usage_stats"),
            factories.ImageFactory.get_list_url(action="usage_stats"),
        ]

    def assert_status(self, user, expected):
        self.client.force_authenticate(user=user)
        for url in self.urls:
            with self.subTest(url=url):
                response = self.client.get(url)
                self.assertEqual(response.status_code, expected, response.data)

    def test_user_without_role_cannot_read_usage_stats(self):
        self.assert_status(self.fixture.user, status.HTTP_403_FORBIDDEN)

    def test_owner_of_a_project_using_the_cloud_cannot_read_usage_stats(self):
        self.assert_status(self.fixture.owner, status.HTTP_403_FORBIDDEN)

    def test_support_can_read_usage_stats(self):
        self.assert_status(self.fixture.global_support, status.HTTP_200_OK)

    def test_staff_can_read_usage_stats(self):
        self.assert_status(self.fixture.staff, status.HTTP_200_OK)
