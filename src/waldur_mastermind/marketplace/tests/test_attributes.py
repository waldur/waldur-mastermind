from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models

from . import factories


@ddt
class AttributeFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.section1 = factories.SectionFactory(key="filter-section-1")
        self.section2 = factories.SectionFactory(key="filter-section-2")
        self.attr1 = factories.AttributeFactory(
            section=self.section1, key="attr-in-section-1"
        )
        self.attr2 = factories.AttributeFactory(
            section=self.section2, key="attr-in-section-2"
        )

    @data("staff")
    def test_filter_attributes_by_section_url(self, user):
        """Filtering by section URL returns only attributes from that section."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        section_url = factories.SectionFactory.get_url(section=self.section1)
        response = self.client.get(
            factories.AttributeFactory.get_list_url(),
            {"section": section_url},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["key"], self.attr1.key)


@ddt
class AttributeOptionFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.attr1 = factories.AttributeFactory(type="choice", key="filter-attr-1")
        self.attr2 = factories.AttributeFactory(type="choice", key="filter-attr-2")
        self.opt1 = factories.AttributeOptionFactory(
            attribute=self.attr1, key="opt-1", title="Option 1"
        )
        self.opt2 = factories.AttributeOptionFactory(
            attribute=self.attr2, key="opt-2", title="Option 2"
        )

    @data("staff")
    def test_filter_options_by_attribute_url(self, user):
        """Filtering by attribute URL returns only options for that attribute."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        attribute_url = factories.AttributeFactory.get_url(attribute=self.attr1)
        response = self.client.get(
            factories.AttributeOptionFactory.get_list_url(),
            {"attribute": attribute_url},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        results = response.json()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["key"], self.opt1.key)


@ddt
class AttributeGetTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.attribute = factories.AttributeFactory()

    @data("staff")
    def test_attributes_should_be_visible_to_staff(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.AttributeFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.json()), 1)

    @data("owner", "user", "customer_support", "admin", "manager")
    def test_attributes_should_not_be_visible_to_other_users(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.AttributeFactory.get_list_url()
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class AttributeCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.AttributeFactory.get_list_url()
        self.section = factories.SectionFactory()

    @data("staff")
    def test_user_can_create_attribute(self, user):
        response = self.create_attribute(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.Attribute.objects.count(), 1)

    @data("user", "customer_support", "admin", "manager")
    def test_user_can_not_create_attribute(self, user):
        response = self.create_attribute(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_attribute(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.post(
            self.url,
            {
                "key": "test-attribute-key",
                "title": "Test Attribute",
                "section": factories.SectionFactory.get_url(section=self.section),
                "type": "string",
            },
        )


@ddt
class AttributeUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.attribute = factories.AttributeFactory(
            key="editable-attr", title="Original Title", type="string"
        )
        self.url = factories.AttributeFactory.get_url(attribute=self.attribute)

    @data("staff")
    def test_staff_can_update_attribute(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(
            self.url,
            {"title": "Updated Title", "required": True},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.attribute.refresh_from_db()
        self.assertEqual(self.attribute.title, "Updated Title")
        self.assertTrue(self.attribute.required)

    @data("user", "customer_support", "admin", "manager", "owner")
    def test_non_staff_cannot_update_attribute(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, {"title": "Hacked Title"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.attribute.refresh_from_db()
        self.assertEqual(self.attribute.title, "Original Title")


@ddt
class AttributeDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.attribute = factories.AttributeFactory(key="deletable-attr")
        self.url = factories.AttributeFactory.get_url(attribute=self.attribute)

    @data("staff")
    def test_staff_can_delete_attribute(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            models.Attribute.objects.filter(key=self.attribute.key).exists()
        )

    @data("user", "customer_support", "admin", "manager", "owner")
    def test_non_staff_cannot_delete_attribute(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.Attribute.objects.filter(key=self.attribute.key).exists()
        )


@ddt
class AttributeOptionCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.AttributeOptionFactory.get_list_url()
        self.choice_attribute = factories.AttributeFactory(type="choice")

    @data("staff")
    def test_user_can_create_option_for_choice_attribute(self, user):
        response = self.create_option(user)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(models.AttributeOption.objects.count(), 1)

    @data("staff")
    def test_user_can_not_create_option_for_string_attribute(self, user):
        string_attribute = factories.AttributeFactory(type="string")
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.post(
            self.url,
            {
                "key": "opt1",
                "title": "Option 1",
                "attribute": factories.AttributeFactory.get_url(
                    attribute=string_attribute
                ),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("attribute", response.json())

    @data("user", "customer_support", "admin", "manager")
    def test_user_can_not_create_option(self, user):
        response = self.create_option(user)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def create_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        return self.client.post(
            self.url,
            {
                "key": "opt1",
                "title": "Option 1",
                "attribute": factories.AttributeFactory.get_url(
                    attribute=self.choice_attribute
                ),
            },
        )


@ddt
class AttributeOptionUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.choice_attribute = factories.AttributeFactory(type="choice")
        self.option = factories.AttributeOptionFactory(
            attribute=self.choice_attribute,
            key="editable-opt",
            title="Original Option Title",
        )
        self.url = factories.AttributeOptionFactory.get_url(option=self.option)

    @data("staff")
    def test_staff_can_update_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(
            self.url,
            {"title": "Updated Option Title"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.option.refresh_from_db()
        self.assertEqual(self.option.title, "Updated Option Title")

    @data("user", "customer_support", "admin", "manager", "owner")
    def test_non_staff_cannot_update_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.patch(self.url, {"title": "Hacked Title"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.option.refresh_from_db()
        self.assertEqual(self.option.title, "Original Option Title")


@ddt
class AttributeOptionDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.choice_attribute = factories.AttributeFactory(type="choice")
        self.option = factories.AttributeOptionFactory(
            attribute=self.choice_attribute,
            key="deletable-opt",
            title="Option to Delete",
        )
        self.url = factories.AttributeOptionFactory.get_url(option=self.option)

    @data("staff")
    def test_staff_can_delete_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        option_pk = self.option.pk
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.AttributeOption.objects.filter(pk=option_pk).exists())

    @data("user", "customer_support", "admin", "manager", "owner")
    def test_non_staff_cannot_delete_option(self, user):
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(
            models.AttributeOption.objects.filter(pk=self.option.pk).exists()
        )


@ddt
class AttributeOptionIsDefaultTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.choice_attribute = factories.AttributeFactory(type="choice")
        self.option1 = factories.AttributeOptionFactory(
            attribute=self.choice_attribute, key="opt1", title="Option 1"
        )
        self.option2 = factories.AttributeOptionFactory(
            attribute=self.choice_attribute, key="opt2", title="Option 2"
        )

    @data("staff")
    def test_can_set_default_via_attribute(self, user):
        """User can set default option by PATCHing the attribute with default=option_key."""
        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.AttributeFactory.get_url(attribute=self.choice_attribute)
        response = self.client.patch(url, {"default": "opt1"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.choice_attribute.refresh_from_db()
        self.assertEqual(self.choice_attribute.default, "opt1")

        # Option1 should report is_default=True in API response
        option_url = factories.AttributeOptionFactory.get_url(option=self.option1)
        option_response = self.client.get(option_url)
        self.assertTrue(option_response.data["is_default"])

    @data("staff")
    def test_can_clear_default_via_attribute(self, user):
        """User can clear default by PATCHing the attribute with default=null."""
        self.choice_attribute.default = "opt1"
        self.choice_attribute.save()

        user = getattr(self.fixture, user)
        self.client.force_authenticate(user)
        url = factories.AttributeFactory.get_url(attribute=self.choice_attribute)
        response = self.client.patch(url, {"default": None})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        self.choice_attribute.refresh_from_db()
        self.assertIsNone(self.choice_attribute.default)

        option_response = self.client.get(
            factories.AttributeOptionFactory.get_url(option=self.option1)
        )
        self.assertFalse(option_response.data["is_default"])
