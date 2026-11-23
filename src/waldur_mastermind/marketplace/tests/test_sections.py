from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models

from . import factories


@ddt
class SectionGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.section = factories.SectionFactory()

    @data(
        "staff",
    )
    def test_sections_should_be_visible_to_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.SectionFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_sections_should_not_be_visible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.SectionFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class SectionCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.SectionFactory.get_list_url()

    @data(
        "staff",
    )
    def test_user_can_create_section(self, user):
        response = self.create_section(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Section.objects.count(), 1)

    @data("user", "customer_support", "admin", "manager")
    def test_user_can_not_create_section(self, user):
        response = self.create_section(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_section(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.post(
            self.url,
            {
                "key": "key-section",
                "title": "title-section",
                "category": factories.CategoryFactory.get_url(),
            },
        )


@ddt
class SectionUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.section = factories.SectionFactory()

    @data(
        "staff",
    )
    def test_user_can_update_section(self, user):
        response, section = self.update_section(user)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(section.title, "new_title")

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_can_not_update_section(self, user):
        response, section = self.update_section(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def update_section(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.SectionFactory.get_url(section=self.section)

        response = self.client.patch(url, {"title": "new_title"})
        self.section.refresh_from_db()

        return response, self.section


@ddt
class SectionDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.section = factories.SectionFactory()

    @data(
        "staff",
    )
    def test_user_can_delete_section(self, user):
        response = self.delete_section(user)
        self.assertEqual(
            response.status_code, status.HTTP_204_NO_CONTENT, response.data
        )

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_user_can_not_delete_section(self, user):
        response = self.delete_section(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def delete_section(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.SectionFactory.get_url(self.section)
        response = self.client.delete(url)
        return response


class SectionKeyTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.category = factories.CategoryFactory(title="HPC Services")

    def create(self, **payload):
        payload.setdefault("category", factories.CategoryFactory.get_url(self.category))
        return self.client.post(factories.SectionFactory.get_list_url(), payload)

    def test_key_is_generated_from_category_and_title(self):
        response = self.create(title="Support Team")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "hpc_services_support_team")

    def test_generated_key_is_numbered_when_taken(self):
        self.create(title="Support")
        response = self.create(title="Support")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "hpc_services_support_2")

    def test_title_without_slug_characters_falls_back(self):
        category = factories.CategoryFactory(title="Ж")
        response = self.create(
            title="Поддержка", category=factories.CategoryFactory.get_url(category)
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertTrue(response.data["key"].startswith("section_"))

    def test_key_cannot_be_changed(self):
        section = factories.SectionFactory(key="original", category=self.category)
        response = self.client.patch(
            factories.SectionFactory.get_url(section=section), {"key": "renamed"}
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            list(models.Section.objects.values_list("key", flat=True)), ["original"]
        )

    def test_filter_by_category(self):
        factories.SectionFactory(key="mine", category=self.category)
        factories.SectionFactory(key="other")
        response = self.client.get(
            factories.SectionFactory.get_list_url(),
            {"category_uuid": self.category.uuid.hex},
        )
        self.assertEqual([s["key"] for s in response.data], ["mine"])
