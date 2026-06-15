from constance.test import override_config
from ddt import ddt
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

    def test_staff_sees_full_registry(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_non_staff_sees_only_affiliations_approved_for_their_customers(self):
        # User with a role in a customer that has org1 (but not org2) in its
        # default_affiliations should see only org1.
        project_fixture = fixtures.ProjectFixture()
        project_fixture.customer.default_affiliations.add(self.org1)
        self.client.force_authenticate(user=project_fixture.owner)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        uuids = {item["uuid"] for item in response.data}
        self.assertEqual(uuids, {self.org1.uuid.hex})

    def test_non_staff_without_customer_role_sees_nothing(self):
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 0)

    def test_anonymous_sees_nothing(self):
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 0)

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

    def test_filter_default_for_customer(self):
        customer_fixture = fixtures.CustomerFixture()
        customer_fixture.customer.default_affiliations.add(self.org1)
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url,
            data={"default_for_customer": customer_fixture.customer.uuid.hex},
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.org1.uuid.hex)

    def test_projects_count_is_returned(self):
        self.client.force_authenticate(user=self.fixture.staff)
        project_fixture = fixtures.ProjectFixture()
        project_fixture.project.affiliation = self.org1
        project_fixture.project.save()
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


class ProjectAffiliationAssignmentTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        self.org1 = factories.AffiliatedOrganizationFactory()
        self.org2 = factories.AffiliatedOrganizationFactory()
        self.fixture.customer.default_affiliations.add(self.org1, self.org2)
        self.url = factories.ProjectFactory.get_url(
            self.fixture.project, action="update_affiliation"
        )

    def test_owner_can_assign_affiliation_from_default_list(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, {"affiliation": self.org1.uuid.hex})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.project.refresh_from_db()
        self.assertEqual(self.fixture.project.affiliation, self.org1)

    def test_owner_cannot_assign_affiliation_outside_default_list(self):
        outside_org = factories.AffiliatedOrganizationFactory()
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, {"affiliation": outside_org.uuid.hex})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    def test_staff_can_assign_any_affiliation(self):
        outside_org = factories.AffiliatedOrganizationFactory()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.url, {"affiliation": outside_org.uuid.hex})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.project.refresh_from_db()
        self.assertEqual(self.fixture.project.affiliation, outside_org)

    def test_assignment_replaces_previous(self):
        self.client.force_authenticate(user=self.fixture.owner)
        self.fixture.project.affiliation = self.org1
        self.fixture.project.save()
        response = self.client.post(self.url, {"affiliation": self.org2.uuid.hex})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.project.refresh_from_db()
        self.assertEqual(self.fixture.project.affiliation, self.org2)

    def test_owner_can_clear_affiliation(self):
        self.client.force_authenticate(user=self.fixture.owner)
        self.fixture.project.affiliation = self.org1
        self.fixture.project.save()
        response = self.client.post(self.url, {"affiliation": None})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.project.refresh_from_db()
        self.assertIsNone(self.fixture.project.affiliation)

    def test_member_cannot_assign(self):
        self.client.force_authenticate(user=self.fixture.member)
        response = self.client.post(self.url, {"affiliation": self.org1.uuid.hex})
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


class CustomerDefaultAffiliationsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.org1 = factories.AffiliatedOrganizationFactory()
        self.org2 = factories.AffiliatedOrganizationFactory()
        self.url = factories.CustomerFactory.get_url(
            self.fixture.customer, action="update_default_affiliations"
        )

    def test_staff_can_set_default_affiliations(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            self.url,
            {"default_affiliations": [self.org1.uuid.hex, self.org2.uuid.hex]},
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(
            set(self.fixture.customer.default_affiliations.all()),
            {self.org1, self.org2},
        )

    def test_set_replaces_existing(self):
        self.fixture.customer.default_affiliations.add(self.org1)
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            self.url, {"default_affiliations": [self.org2.uuid.hex]}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(
            list(self.fixture.customer.default_affiliations.all()), [self.org2]
        )

    def test_owner_cannot_set_default_affiliations(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(
            self.url, {"default_affiliations": [self.org1.uuid.hex]}
        )
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


class ProjectAffiliationOnCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        self.org_in = factories.AffiliatedOrganizationFactory()
        self.org_out = factories.AffiliatedOrganizationFactory()
        self.fixture.customer.default_affiliations.add(self.org_in)
        self.url = factories.ProjectFactory.get_list_url()

    def _payload(self, affiliation_uuid=None):
        payload = {
            "name": "Project Foo",
            "customer": factories.CustomerFactory.get_url(self.fixture.customer),
        }
        if affiliation_uuid is not None:
            payload["affiliation_uuid"] = affiliation_uuid
        return payload

    def test_owner_can_create_with_affiliation_in_default_list(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, self._payload(self.org_in.uuid.hex))
        self.assertEqual(status.HTTP_201_CREATED, response.status_code, response.data)
        project = models.Project.objects.get(uuid=response.data["uuid"])
        self.assertEqual(project.affiliation, self.org_in)

    def test_owner_cannot_create_with_affiliation_outside_default_list(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, self._payload(self.org_out.uuid.hex))
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)

    def test_staff_can_create_with_any_affiliation(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.url, self._payload(self.org_out.uuid.hex))
        self.assertEqual(status.HTTP_201_CREATED, response.status_code, response.data)

    @override_config(AFFILIATION_REQUIRED_AT_PROJECT_CREATION=True)
    def test_create_fails_when_required_and_missing(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, self._payload(None))
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
        self.assertIn("affiliation_uuid", response.data)

    @override_config(AFFILIATION_REQUIRED_AT_PROJECT_CREATION=True)
    def test_create_succeeds_when_required_and_provided(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, self._payload(self.org_in.uuid.hex))
        self.assertEqual(status.HTTP_201_CREATED, response.status_code, response.data)

    @override_config(AFFILIATION_REQUIRED_AT_PROJECT_CREATION=False)
    def test_create_succeeds_without_affiliation_when_not_required(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, self._payload(None))
        self.assertEqual(status.HTTP_201_CREATED, response.status_code, response.data)


class ProjectAffiliationFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.org = factories.AffiliatedOrganizationFactory(name="Test Org")
        self.fixture.project.affiliation = self.org
        self.fixture.project.save()
        self.project2 = factories.ProjectFactory(customer=self.fixture.customer)
        self.url = factories.ProjectFactory.get_list_url()

    def test_filter_by_affiliation_uuid(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(
            self.url, data={"affiliation_uuid": self.org.uuid.hex}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.project.uuid.hex)

    def test_filter_by_affiliation_name(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"affiliation_name": "Test"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.project.uuid.hex)

    def test_filter_has_affiliation_true(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"has_affiliation": True})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.project.uuid.hex)

    def test_filter_has_affiliation_false(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"has_affiliation": False})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        uuids = [item["uuid"] for item in response.data]
        self.assertIn(self.project2.uuid.hex, uuids)
        self.assertNotIn(self.fixture.project.uuid.hex, uuids)


class ProjectAffiliationFlatFieldsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.org = factories.AffiliatedOrganizationFactory(
            name="ETH Zurich", code="ETHZ"
        )

    def test_flat_fields_present_when_affiliation_set(self):
        self.fixture.project.affiliation = self.org
        self.fixture.project.save()
        self.client.force_authenticate(user=self.fixture.staff)
        url = factories.ProjectFactory.get_url(self.fixture.project)
        response = self.client.get(url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(str(response.data["affiliation_uuid"]), self.org.uuid.hex)
        self.assertEqual(response.data["affiliation_name"], "ETH Zurich")
        self.assertEqual(response.data["affiliation_code"], "ETHZ")

    def test_flat_fields_absent_when_affiliation_unset(self):
        # Mirrors the science_sub_domain pattern: when source-traversal hits None,
        # the flat read-only fields are omitted from the response. The nested
        # `affiliation` field stays present (as None) so consumers can detect.
        self.client.force_authenticate(user=self.fixture.staff)
        url = factories.ProjectFactory.get_url(self.fixture.project)
        response = self.client.get(url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertIsNone(response.data["affiliation"])
        self.assertIsNone(response.data["affiliation_uuid"])
        self.assertNotIn("affiliation_name", response.data)
        self.assertNotIn("affiliation_code", response.data)


class AffiliatedOrganizationStatsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.org = factories.AffiliatedOrganizationFactory()
        self.fixture.project.affiliation = self.org
        self.fixture.project.save()
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
        self.fixture.project.affiliation = self.org
        self.fixture.project.save()
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
