from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures


@ddt
class AffiliatedOrganizationListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.org1 = factories.AffiliatedOrganizationFactory(
            name="Alpha University", abbreviation="AU", country="EE"
        )
        self.org2 = factories.AffiliatedOrganizationFactory(
            name="Beta Institute", abbreviation="BI", country="FI"
        )
        self.url = factories.AffiliatedOrganizationFactory.get_list_url()

    @data("staff", "user", None)
    def test_user_can_list_affiliated_organizations(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_filter_by_name(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"name": "Alpha"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.org1.uuid.hex)

    def test_filter_by_abbreviation(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"abbreviation": "BI"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.org2.uuid.hex)

    def test_filter_by_country(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"country": "EE"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.org1.uuid.hex)

    def test_projects_count_is_returned(self):
        self.client.force_authenticate(user=self.fixture.staff)
        project_fixture = fixtures.ProjectFixture()
        project_fixture.project.affiliated_organizations.add(self.org1)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        org1_data = next(
            item for item in response.data if item["uuid"] == self.org1.uuid.hex
        )
        self.assertEqual(org1_data["projects_count"], 1)


@ddt
class AffiliatedOrganizationCRUDTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.AffiliatedOrganizationFactory.get_list_url()

    def test_staff_can_create(self):
        self.client.force_authenticate(user=self.fixture.staff)
        payload = {
            "name": "New Org",
            "code": "NEWORG",
            "abbreviation": "NO",
            "country": "DE",
            "email": "org@example.com",
        }
        response = self.client.post(self.url, payload)
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        self.assertTrue(
            models.AffiliatedOrganization.objects.filter(name="New Org").exists()
        )

    def test_non_staff_cannot_create(self):
        self.client.force_authenticate(user=self.fixture.owner)
        payload = {"name": "New Org"}
        response = self.client.post(self.url, payload)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_staff_can_update(self):
        self.client.force_authenticate(user=self.fixture.staff)
        org = factories.AffiliatedOrganizationFactory()
        url = factories.AffiliatedOrganizationFactory.get_url(org)
        response = self.client.patch(url, {"name": "Updated Name"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        org.refresh_from_db()
        self.assertEqual(org.name, "Updated Name")

    def test_staff_can_delete(self):
        self.client.force_authenticate(user=self.fixture.staff)
        org = factories.AffiliatedOrganizationFactory()
        url = factories.AffiliatedOrganizationFactory.get_url(org)
        response = self.client.delete(url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)
        self.assertFalse(
            models.AffiliatedOrganization.objects.filter(pk=org.pk).exists()
        )

    def test_non_staff_cannot_delete(self):
        self.client.force_authenticate(user=self.fixture.owner)
        org = factories.AffiliatedOrganizationFactory()
        url = factories.AffiliatedOrganizationFactory.get_url(org)
        response = self.client.delete(url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


class AffiliatedOrganizationAssignmentTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.org1 = factories.AffiliatedOrganizationFactory()
        self.org2 = factories.AffiliatedOrganizationFactory()
        self.url = factories.ProjectFactory.get_url(
            self.fixture.project, action="update_affiliated_organizations"
        )

    def test_owner_can_assign_affiliated_organizations(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.url,
            {"affiliated_organizations": [self.org1.uuid.hex, self.org2.uuid.hex]},
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.project.refresh_from_db()
        self.assertIn(self.org1, self.fixture.project.affiliated_organizations.all())
        self.assertIn(self.org2, self.fixture.project.affiliated_organizations.all())

    def test_assignment_clears_previous(self):
        self.client.force_authenticate(user=self.fixture.owner)
        self.fixture.project.affiliated_organizations.add(self.org1)
        response = self.client.post(
            self.url,
            {"affiliated_organizations": [self.org2.uuid.hex]},
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        orgs = list(self.fixture.project.affiliated_organizations.all())
        self.assertEqual(orgs, [self.org2])

    def test_member_cannot_assign(self):
        self.client.force_authenticate(user=self.fixture.member)
        response = self.client.post(
            self.url,
            {"affiliated_organizations": [self.org1.uuid.hex]},
        )
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


class ProjectAffiliatedOrganizationFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.org = factories.AffiliatedOrganizationFactory(name="Test Org")
        self.fixture.project.affiliated_organizations.add(self.org)
        self.project2 = factories.ProjectFactory(customer=self.fixture.customer)
        self.url = factories.ProjectFactory.get_list_url()

    def test_filter_by_affiliated_organization_uuid(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, data={"affiliated_organization_uuid": self.org.uuid.hex}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.project.uuid.hex)

    def test_filter_by_affiliated_organization_name(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, data={"affiliated_organization_name": "Test"}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.project.uuid.hex)

    def test_filter_has_affiliated_organization_true(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"has_affiliated_organization": True})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.project.uuid.hex)

    def test_filter_has_affiliated_organization_false(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, data={"has_affiliated_organization": False}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.project2.uuid.hex, uuids)
        self.assertNotIn(self.fixture.project.uuid.hex, uuids)


class AffiliatedOrganizationStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.org = factories.AffiliatedOrganizationFactory()
        self.fixture.project.affiliated_organizations.add(self.org)
        self.url = factories.AffiliatedOrganizationFactory.get_url(
            self.org, action="stats"
        )

    def test_stats_returns_counts(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIn("active_projects_count", response.data)
        self.assertIn("resources_count", response.data)
        self.assertIn("estimated_monthly_cost", response.data)
        self.assertEqual(response.data["active_projects_count"], 1)


class AffiliatedOrganizationReportTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.org = factories.AffiliatedOrganizationFactory()
        self.fixture.project.affiliated_organizations.add(self.org)
        self.url = factories.AffiliatedOrganizationFactory.get_list_url() + "report/"

    def test_staff_can_access_report(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        # Should have at least the org row + unaffiliated row
        self.assertGreaterEqual(len(response.data), 2)
        org_row = next(r for r in response.data if r["org_uuid"] == self.org.uuid.hex)
        self.assertEqual(org_row["projects_count"], 1)
        unaffiliated_row = next(r for r in response.data if r["org_uuid"] is None)
        self.assertIsNotNone(unaffiliated_row)

    def test_non_staff_cannot_access_report(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)
