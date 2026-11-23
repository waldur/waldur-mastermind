from datetime import timedelta
from unittest import mock

from ddt import data, ddt
from django.utils import timezone
from rest_framework import status, test

from waldur_core.structure.tests import fixtures
from waldur_mastermind.marketplace import models, serializers

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


class AttributeStructureTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(self.fixture.staff)
        self.section = factories.SectionFactory(key="hpc_support")
        self.section_url = factories.SectionFactory.get_url(section=self.section)
        self.url = factories.AttributeFactory.get_list_url()

    def create(self, **payload):
        payload.setdefault("section", self.section_url)
        payload.setdefault("type", "string")
        return self.client.post(self.url, payload, format="json")

    def option_keys(self, attribute):
        return sorted(attribute.options.values_list("key", flat=True))

    def test_key_is_generated_from_section_and_title(self):
        response = self.create(title="Support e-mail")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "hpc_support_support_e_mail")

    def test_key_cannot_be_changed(self):
        attribute = factories.AttributeFactory(key="fixed", section=self.section)
        response = self.client.patch(
            factories.AttributeFactory.get_url(attribute=attribute),
            {"key": "moved"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(models.Attribute.objects.get().key, "fixed")

    def test_create_choice_attribute_with_options_and_default(self):
        response = self.create(
            title="Delivery",
            type="choice",
            options=[
                {"key": "online", "title": "Online"},
                {"key": "onsite", "title": "On site"},
            ],
            default="onsite",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        attribute = models.Attribute.objects.get()
        self.assertEqual(self.option_keys(attribute), ["online", "onsite"])
        self.assertEqual(
            {o["key"]: o["is_default"] for o in response.data["options"]},
            {"online": False, "onsite": True},
        )

    def test_option_key_is_generated_without_taking_a_requested_one(self):
        response = self.create(
            title="Delivery",
            type="choice",
            options=[{"title": "Online"}, {"key": "online", "title": "Remote"}],
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        attribute = models.Attribute.objects.get()
        self.assertEqual(self.option_keys(attribute), ["online", "online_2"])
        self.assertEqual(attribute.options.get(key="online").title, "Remote")

    def test_update_syncs_options(self):
        attribute = factories.AttributeFactory(section=self.section, type="list")
        factories.AttributeOptionFactory(attribute=attribute, key="a", title="A")
        factories.AttributeOptionFactory(attribute=attribute, key="b", title="B")
        response = self.client.patch(
            factories.AttributeFactory.get_url(attribute=attribute),
            {
                "options": [
                    {"key": "a", "title": "A renamed"},
                    {"key": "c", "title": "C"},
                ],
                "default": ["a", "c"],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(self.option_keys(attribute), ["a", "c"])
        self.assertEqual(attribute.options.get(key="a").title, "A renamed")
        attribute.refresh_from_db()
        self.assertEqual(attribute.default, ["a", "c"])

    def test_default_must_be_an_option(self):
        response = self.create(
            title="Delivery",
            type="choice",
            options=[{"key": "online", "title": "Online"}],
            default="offline",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("default", response.data)
        self.assertFalse(models.Attribute.objects.exists())

    def test_default_must_match_type(self):
        response = self.create(title="Nodes", type="integer", default="many")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("default", response.data)

    def test_options_rejected_for_type_without_options(self):
        response = self.create(title="Phone", options=[{"key": "x", "title": "X"}])
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("options", response.data)

    def test_duplicate_option_keys_rejected(self):
        response = self.create(
            title="Delivery",
            type="choice",
            options=[{"key": "x", "title": "X"}, {"key": "x", "title": "Y"}],
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_switching_to_type_without_options_deletes_them(self):
        attribute = factories.AttributeFactory(section=self.section, type="choice")
        factories.AttributeOptionFactory(attribute=attribute, key="a", title="A")
        response = self.client.patch(
            factories.AttributeFactory.get_url(attribute=attribute),
            {"type": "string"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertFalse(attribute.options.exists())

    def test_option_can_be_added_to_list_attribute(self):
        attribute = factories.AttributeFactory(section=self.section, type="list")
        response = self.client.post(
            factories.AttributeOptionFactory.get_list_url(),
            {
                "attribute": factories.AttributeFactory.get_url(attribute=attribute),
                "key": "a",
                "title": "A",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class CategorySectionsOrderTest(test.APITestCase):
    def test_sections_and_attributes_are_listed_in_creation_order(self):
        fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(fixture.staff)
        category = factories.CategoryFactory()
        now = timezone.now()
        # Created timestamps run against the insertion order, so the result
        # can only match when the query sorts by them.
        for index, key in enumerate(("alpha", "zeta")):
            section = factories.SectionFactory(key=key, category=category)
            models.Section.objects.filter(pk=key).update(
                created=now - timedelta(minutes=index)
            )
            for attr_index, attr_key in enumerate(("a", "z")):
                factories.AttributeFactory(key=f"{key}_{attr_key}", section=section)
                models.Attribute.objects.filter(pk=f"{key}_{attr_key}").update(
                    created=now - timedelta(minutes=attr_index)
                )
        response = self.client.get(factories.CategoryFactory.get_url(category))
        sections = response.data["sections"]
        self.assertEqual([s["key"] for s in sections], ["zeta", "alpha"])
        self.assertEqual(
            [a["key"] for a in sections[0]["attributes"]], ["zeta_z", "zeta_a"]
        )


class AttributeStaleDefaultTest(test.APITestCase):
    def setUp(self):
        fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(fixture.staff)
        self.attribute = factories.AttributeFactory(type="choice", default="gone")
        factories.AttributeOptionFactory(attribute=self.attribute, key="a")
        self.url = factories.AttributeFactory.get_url(attribute=self.attribute)

    def test_unrelated_edit_keeps_a_stale_default(self):
        response = self.client.patch(self.url, {"title": "New"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.attribute.refresh_from_db()
        self.assertEqual(self.attribute.default, "gone")

    def test_type_change_drops_a_default_that_no_longer_fits(self):
        self.attribute.default = "a"
        self.attribute.save()
        response = self.client.patch(self.url, {"type": "list"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.attribute.refresh_from_db()
        self.assertIsNone(self.attribute.default)

    def test_options_change_drops_a_default_that_was_removed(self):
        self.attribute.default = "a"
        self.attribute.save()
        response = self.client.patch(
            self.url, {"options": [{"key": "b", "title": "B"}]}, format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.attribute.refresh_from_db()
        self.assertIsNone(self.attribute.default)

    def test_sent_default_is_still_validated(self):
        response = self.client.patch(self.url, {"default": "gone"}, format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class GeneratedKeyCollisionTest(test.APITestCase):
    def test_section_key_is_regenerated_when_taken_at_insert_time(self):
        fixture = fixtures.ProjectFixture()
        self.client.force_authenticate(fixture.staff)
        category = factories.CategoryFactory(title="HPC")
        factories.SectionFactory(key="hpc_support")
        real = serializers.make_unique_key
        calls = []

        def racing_make_unique_key(model, base, fallback, taken=(), **scope):
            # The first lookup misses the row another request just inserted.
            calls.append(set(taken))
            if len(calls) == 1:
                return "hpc_support"
            return real(model, base, fallback, taken=taken, **scope)

        with mock.patch.object(
            serializers, "make_unique_key", side_effect=racing_make_unique_key
        ):
            response = self.client.post(
                factories.SectionFactory.get_list_url(),
                {
                    "title": "Support",
                    "category": factories.CategoryFactory.get_url(category),
                },
            )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        self.assertEqual(response.data["key"], "hpc_support_2")
