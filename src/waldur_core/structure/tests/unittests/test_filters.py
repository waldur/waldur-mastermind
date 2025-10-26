from datetime import datetime, timedelta

from rest_framework.test import APITransactionTestCase

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.structure.tests import factories
from waldur_core.structure.tests.factories import ProjectFactory, UserFactory


class CustomerAccountingStartDateFilterTest(APITransactionTestCase):
    def setUp(self):
        running_customer = factories.CustomerFactory(
            accounting_start_date=datetime.now() - timedelta(days=7)
        )
        not_running_customer = factories.CustomerFactory(
            accounting_start_date=datetime.now() + timedelta(days=7)
        )
        self.running_project = factories.ProjectFactory(customer=running_customer)
        self.not_running_project = factories.ProjectFactory(
            customer=not_running_customer
        )

    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_accounting_is_running_filter_behaves_properly(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        response = self.client.get(ProjectFactory.get_list_url())
        self.assertEqual(len(response.data), 2)

        response = self.client.get(
            ProjectFactory.get_list_url(),
            {
                "accounting_is_running": "true",
            },
        )
        self.assertEqual(len(response.data), 1)

        response = self.client.get(
            ProjectFactory.get_list_url(),
            {
                "accounting_is_running": "false",
            },
        )
        self.assertEqual(len(response.data), 1)


class ProjectIsRemovedFilterTest(APITransactionTestCase):
    def setUp(self):
        self.active_project = factories.ProjectFactory()
        self.removed_project = factories.ProjectFactory()
        self.removed_project.delete()  # Soft delete to set is_removed=True

    def test_is_removed_filter_behaves_properly(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        # Without filter - should only see active projects
        response = self.client.get(ProjectFactory.get_list_url())
        self.assertEqual(len(response.data), 1)
        project_uuids = [project["uuid"] for project in response.data]
        self.assertIn(str(self.active_project.uuid), project_uuids)
        self.assertNotIn(str(self.removed_project.uuid), project_uuids)

        # Filter by is_removed=false - should only see active projects
        response = self.client.get(
            ProjectFactory.get_list_url(),
            {
                "is_removed": "false",
                "include_terminated": "true",
            },
        )
        self.assertEqual(len(response.data), 1)
        project_uuids = [project["uuid"] for project in response.data]
        self.assertIn(str(self.active_project.uuid), project_uuids)
        self.assertNotIn(str(self.removed_project.uuid), project_uuids)

        # Filter by is_removed=true - should only see removed projects (need include_terminated)
        response = self.client.get(
            ProjectFactory.get_list_url(),
            {
                "is_removed": "true",
                "include_terminated": "true",
            },
        )
        self.assertEqual(len(response.data), 1)
        project_uuids = [project["uuid"] for project in response.data]
        self.assertNotIn(str(self.active_project.uuid), project_uuids)
        self.assertIn(str(self.removed_project.uuid), project_uuids)


class ProjectQueryFilterTest(APITransactionTestCase):
    def setUp(self):
        self.project1 = factories.ProjectFactory(name="Test Project Alpha")
        self.project2 = factories.ProjectFactory(name="Beta Project")
        self.project3 = factories.ProjectFactory(name="Gamma Research")
        # Manually set specific slugs for predictable testing
        self.project1.slug = "test-project-alpha"
        self.project1.save()
        self.project2.slug = "beta-project"
        self.project2.save()
        self.project3.slug = "gamma-research"
        self.project3.save()

    def test_query_filter_by_name(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        response = self.client.get(ProjectFactory.get_list_url(), {"query": "Alpha"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.project1.uuid))

    def test_query_filter_by_slug(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        # Test exact slug match
        response = self.client.get(
            ProjectFactory.get_list_url(), {"query": "beta-project"}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.project2.uuid))

        # Test partial slug match
        response = self.client.get(ProjectFactory.get_list_url(), {"query": "gamma"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.project3.uuid))

    def test_query_filter_by_uuid(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        response = self.client.get(
            ProjectFactory.get_list_url(), {"query": str(self.project1.uuid)}
        )
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], str(self.project1.uuid))

    def test_query_filter_no_matches(self):
        staff = UserFactory(is_staff=True)
        self.client.force_authenticate(user=staff)

        response = self.client.get(
            ProjectFactory.get_list_url(), {"query": "nonexistent"}
        )
        self.assertEqual(len(response.data), 0)
