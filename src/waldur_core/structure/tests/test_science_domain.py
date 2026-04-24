from ddt import data, ddt
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures

PRESETS_URL = factories.ScienceDomainFactory.get_list_url() + "presets/"
LOAD_PRESET_URL = factories.ScienceDomainFactory.get_list_url() + "load_preset/"


@ddt
class ScienceDomainListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.url = factories.ScienceDomainFactory.get_list_url()
        self.initial_count = models.ScienceDomain.objects.count()
        self.domain1 = factories.ScienceDomainFactory(name="Test Domain A")
        self.domain2 = factories.ScienceDomainFactory(name="Test Domain B")

    @data("staff", "user", None)
    def test_user_can_list_science_domains(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), self.initial_count + 2)

    def test_filter_by_name(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"name": "Test Domain A"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.domain1.uuid.hex)

    def test_subdomains_count_is_returned(self):
        self.client.force_authenticate(user=self.fixture.staff)
        factories.ScienceSubDomainFactory(domain=self.domain1)
        factories.ScienceSubDomainFactory(domain=self.domain1)
        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        domain_data = next(
            item for item in response.data if item["uuid"] == self.domain1.uuid.hex
        )
        self.assertEqual(domain_data["subdomains_count"], 2)


@ddt
class ScienceDomainCRUDTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.ScienceDomainFactory.get_list_url()

    def test_staff_can_create(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(self.url, {"name": "New Domain"})
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        self.assertTrue(models.ScienceDomain.objects.filter(name="New Domain").exists())

    def test_non_staff_cannot_create(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(self.url, {"name": "New Domain"})
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_staff_can_update(self):
        self.client.force_authenticate(user=self.fixture.staff)
        domain = factories.ScienceDomainFactory()
        url = factories.ScienceDomainFactory.get_url(domain)
        response = self.client.patch(url, {"name": "Updated Name"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        domain.refresh_from_db()
        self.assertEqual(domain.name, "Updated Name")

    def test_staff_can_delete(self):
        self.client.force_authenticate(user=self.fixture.staff)
        domain = factories.ScienceDomainFactory()
        url = factories.ScienceDomainFactory.get_url(domain)
        response = self.client.delete(url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)
        self.assertFalse(models.ScienceDomain.objects.filter(pk=domain.pk).exists())

    def test_non_staff_cannot_delete(self):
        self.client.force_authenticate(user=self.fixture.owner)
        domain = factories.ScienceDomainFactory()
        url = factories.ScienceDomainFactory.get_url(domain)
        response = self.client.delete(url)
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)


@ddt
class ScienceSubDomainListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.UserFixture()
        self.domain = factories.ScienceDomainFactory(name="Test Physics")
        self.sub1 = factories.ScienceSubDomainFactory(
            name="Astrophysics", domain=self.domain
        )
        self.sub2 = factories.ScienceSubDomainFactory(
            name="Plasma Physics", domain=self.domain
        )
        self.url = factories.ScienceSubDomainFactory.get_list_url()

    @data("staff", "user", None)
    def test_user_can_list_science_sub_domains(self, user):
        if user:
            self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self.url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_filter_by_domain_uuid(self):
        other_domain = factories.ScienceDomainFactory(name="Other Test")
        factories.ScienceSubDomainFactory(name="Others", domain=other_domain)
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"domain_uuid": self.domain.uuid.hex})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_filter_by_domain_name(self):
        other_domain = factories.ScienceDomainFactory(name="Life Science Test")
        factories.ScienceSubDomainFactory(name="Biology", domain=other_domain)
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(self.url, data={"domain_name": "Test Physics"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 2)

    def test_domain_info_in_response(self):
        self.client.force_authenticate(user=self.fixture.staff)
        url = factories.ScienceSubDomainFactory.get_url(self.sub1)
        response = self.client.get(url)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(str(response.data["domain_uuid"]), self.domain.uuid.hex)
        self.assertEqual(response.data["domain_name"], "Test Physics")


@ddt
class ScienceSubDomainCRUDTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.domain = factories.ScienceDomainFactory()
        self.url = factories.ScienceSubDomainFactory.get_list_url()

    def test_staff_can_create(self):
        self.client.force_authenticate(user=self.fixture.staff)
        domain_url = factories.ScienceDomainFactory.get_url(self.domain)
        response = self.client.post(
            self.url, {"name": "New SubDomain", "domain": domain_url}
        )
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        self.assertTrue(
            models.ScienceSubDomain.objects.filter(name="New SubDomain").exists()
        )

    def test_non_staff_cannot_create(self):
        self.client.force_authenticate(user=self.fixture.owner)
        domain_url = factories.ScienceDomainFactory.get_url(self.domain)
        response = self.client.post(
            self.url, {"name": "New SubDomain", "domain": domain_url}
        )
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_staff_can_delete(self):
        self.client.force_authenticate(user=self.fixture.staff)
        sub = factories.ScienceSubDomainFactory(domain=self.domain)
        url = factories.ScienceSubDomainFactory.get_url(sub)
        response = self.client.delete(url)
        self.assertEqual(status.HTTP_204_NO_CONTENT, response.status_code)
        self.assertFalse(models.ScienceSubDomain.objects.filter(pk=sub.pk).exists())

    def test_cascade_delete_removes_subdomains(self):
        sub = factories.ScienceSubDomainFactory(domain=self.domain)
        self.domain.delete()
        self.assertFalse(models.ScienceSubDomain.objects.filter(pk=sub.pk).exists())


class ProjectScienceSubDomainTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_PROJECT)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)
        self.domain = factories.ScienceDomainFactory(name="Physics")
        self.sub_domain = factories.ScienceSubDomainFactory(
            name="Astrophysics", domain=self.domain
        )

    def test_assign_science_sub_domain_on_project_create(self):
        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ProjectFactory.get_list_url()
        response = self.client.post(
            url,
            {
                "name": "Test Project",
                "customer": factories.CustomerFactory.get_url(self.fixture.customer),
                "science_sub_domain": self.sub_domain.uuid.hex,
            },
        )
        self.assertEqual(status.HTTP_201_CREATED, response.status_code)
        self.assertEqual(
            str(response.data["science_sub_domain"]), self.sub_domain.uuid.hex
        )
        self.assertEqual(response.data["science_sub_domain_name"], "Astrophysics")
        self.assertEqual(
            str(response.data["science_domain_uuid"]), self.domain.uuid.hex
        )
        self.assertEqual(response.data["science_domain_name"], "Physics")

    def test_update_science_sub_domain_on_project(self):
        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ProjectFactory.get_url(self.fixture.project)
        response = self.client.patch(
            url, {"science_sub_domain": self.sub_domain.uuid.hex}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.project.refresh_from_db()
        self.assertEqual(self.fixture.project.science_sub_domain, self.sub_domain)

    def test_clear_science_sub_domain(self):
        self.fixture.project.science_sub_domain = self.sub_domain
        self.fixture.project.save()
        self.client.force_authenticate(user=self.fixture.owner)
        url = factories.ProjectFactory.get_url(self.fixture.project)
        response = self.client.patch(url, {"science_sub_domain": None})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.fixture.project.refresh_from_db()
        self.assertIsNone(self.fixture.project.science_sub_domain)

    def test_filter_projects_by_science_domain(self):
        self.fixture.project.science_sub_domain = self.sub_domain
        self.fixture.project.save()
        self.client.force_authenticate(user=self.fixture.staff)
        url = factories.ProjectFactory.get_list_url()
        response = self.client.get(
            url, data={"science_domain_uuid": self.domain.uuid.hex}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.project.uuid.hex)

    def test_filter_projects_by_science_sub_domain(self):
        self.fixture.project.science_sub_domain = self.sub_domain
        self.fixture.project.save()
        self.client.force_authenticate(user=self.fixture.staff)
        url = factories.ProjectFactory.get_list_url()
        response = self.client.get(
            url, data={"science_sub_domain_uuid": self.sub_domain.uuid.hex}
        )
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(len(response.data), 1)

    def test_set_null_on_subdomain_delete(self):
        self.fixture.project.science_sub_domain = self.sub_domain
        self.fixture.project.save()
        self.sub_domain.delete()
        self.fixture.project.refresh_from_db()
        self.assertIsNone(self.fixture.project.science_sub_domain)


class ScienceDomainPresetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_staff_can_list_presets(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(PRESETS_URL)
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        names = [p["name"] for p in response.data]
        self.assertIn("cscs", names)
        self.assertIn("oecd_fos_2007", names)

    def test_staff_can_load_cscs_preset(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(LOAD_PRESET_URL, {"preset": "cscs"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(response.data["created_domains"], 7)
        self.assertEqual(response.data["created_subdomains"], 24)
        self.assertEqual(models.ScienceDomain.objects.count(), 7)
        self.assertEqual(models.ScienceSubDomain.objects.count(), 24)

    def test_staff_can_load_oecd_preset(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(LOAD_PRESET_URL, {"preset": "oecd_fos_2007"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(response.data["created_domains"], 6)
        self.assertEqual(response.data["created_subdomains"], 43)

    def test_load_preset_is_idempotent(self):
        self.client.force_authenticate(user=self.fixture.staff)
        self.client.post(LOAD_PRESET_URL, {"preset": "cscs"})
        response = self.client.post(LOAD_PRESET_URL, {"preset": "cscs"})
        self.assertEqual(status.HTTP_200_OK, response.status_code)
        self.assertEqual(response.data["created_domains"], 0)
        self.assertEqual(response.data["created_subdomains"], 0)
        self.assertEqual(response.data["skipped_domains"], 7)
        self.assertEqual(response.data["skipped_subdomains"], 24)

    def test_non_staff_cannot_load_preset(self):
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.post(LOAD_PRESET_URL, {"preset": "cscs"})
        self.assertEqual(status.HTTP_403_FORBIDDEN, response.status_code)

    def test_invalid_preset_name(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(LOAD_PRESET_URL, {"preset": "nonexistent"})
        self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
