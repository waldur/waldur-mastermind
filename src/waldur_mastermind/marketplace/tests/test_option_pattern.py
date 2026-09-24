import copy

from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace import enums, models
from waldur_mastermind.marketplace.enums import OfferingStates, ResourceStates
from waldur_mastermind.marketplace.tests import factories, fixtures
from waldur_mastermind.marketplace.tests.test_order_crud import BaseOrderCreateTest

SLUG_PATTERN = "[a-z][a-z0-9-]{2,30}"
SLUG_ERROR = "Lowercase letters, digits and dashes; 3-31 characters."

PATTERN_OPTIONS = {
    "order": ["slug", "notes"],
    "options": {
        "slug": {
            "type": "string",
            "label": "Project slug",
            "required": True,
            "pattern": SLUG_PATTERN,
            "pattern_error": SLUG_ERROR,
        },
        "notes": {
            "type": "text",
            "label": "Notes",
            "pattern": "[^<>]*",
        },
    },
}


class OfferingPatternSaveTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        factories.ServiceProviderFactory(customer=self.customer)
        self.client.force_authenticate(self.fixture.staff)

    def create_offering(self, options, field="options"):
        payload = {
            "name": "offering",
            "category": factories.CategoryFactory.get_url(),
            "customer": structure_factories.CustomerFactory.get_url(self.customer),
            "type": enums.SUPPORT_OFFERING,
            field: options,
        }
        return self.client.post(
            factories.OfferingFactory.get_list_url(), payload, format="json"
        )

    def options_with(self, **slug_fields):
        options = copy.deepcopy(PATTERN_OPTIONS)
        options["options"]["slug"].update(slug_fields)
        return options

    def assert_rejected(self, options, field="options"):
        response = self.create_offering(options, field)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(field, response.data)

    def test_pattern_is_saved_and_returned(self):
        response = self.create_offering(PATTERN_OPTIONS)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        slug = offering.options["options"]["slug"]
        self.assertEqual(slug["pattern"], SLUG_PATTERN)
        self.assertEqual(slug["pattern_error"], SLUG_ERROR)
        self.assertEqual(
            response.data["options"]["options"]["slug"]["pattern"], SLUG_PATTERN
        )

    def test_pattern_is_saved_for_resource_options(self):
        response = self.create_offering(PATTERN_OPTIONS, field="resource_options")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(
            offering.resource_options["options"]["notes"]["pattern"], "[^<>]*"
        )

    def test_invalid_pattern_is_rejected(self):
        self.assert_rejected(self.options_with(pattern="[a-z"))

    def test_invalid_resource_options_pattern_is_rejected(self):
        self.assert_rejected(
            self.options_with(pattern="(unclosed"), field="resource_options"
        )

    def test_too_long_pattern_is_rejected(self):
        self.assert_rejected(self.options_with(pattern="a" * 501))

    def test_pattern_on_non_string_option_is_rejected(self):
        options = copy.deepcopy(PATTERN_OPTIONS)
        options["options"]["slug"]["type"] = "integer"
        self.assert_rejected(options)

    def test_pattern_error_without_pattern_is_rejected(self):
        options = copy.deepcopy(PATTERN_OPTIONS)
        del options["options"]["slug"]["pattern"]
        self.assert_rejected(options)

    def test_default_not_matching_pattern_is_rejected(self):
        self.assert_rejected(self.options_with(default="Not A Slug"))

    def test_default_matching_pattern_is_accepted(self):
        response = self.create_offering(self.options_with(default="my-project"))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_blank_pattern_and_message_mean_none(self):
        response = self.create_offering(self.options_with(pattern="", pattern_error=""))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        slug = offering.options["options"]["slug"]
        self.assertNotIn("pattern", slug)
        self.assertNotIn("pattern_error", slug)

    def test_message_with_blank_pattern_is_rejected(self):
        self.assert_rejected(self.options_with(pattern=""))

    def test_pattern_whitespace_is_kept(self):
        response = self.create_offering(self.options_with(pattern="[a-z]+ "))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        offering = models.Offering.objects.get(uuid=response.data["uuid"])
        self.assertEqual(offering.options["options"]["slug"]["pattern"], "[a-z]+ ")

    def test_backtracking_pattern_passes_save(self):
        # Saving only compiles the pattern; the order-time timeout is the guard.
        response = self.create_offering(self.options_with(pattern="(x+x+)+y"))
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)


class OrderPatternTest(BaseOrderCreateTest):
    def create_offering(self, options):
        return factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
            options=copy.deepcopy(options),
            resource_options={"options": {}, "order": []},
        )

    def order(self, attributes, options=PATTERN_OPTIONS):
        return self.create_order(
            self.fixture.staff,
            self.create_offering(options),
            add_payload={"attributes": attributes},
        )

    def test_matching_value_is_accepted_and_stored(self):
        attributes = {"slug": "my-project", "notes": "Nightly backups"}
        response = self.order(attributes)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        order = models.Order.objects.get(uuid=response.data["uuid"])
        self.assertEqual(order.attributes, attributes)

    def test_value_not_matching_pattern_is_rejected_with_provider_message(self):
        response = self.order({"slug": "My Project"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(SLUG_ERROR, str(response.data))

    def test_pattern_must_match_the_whole_value(self):
        response = self.order({"slug": "my-project!"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_surrounding_whitespace_is_not_trimmed_before_matching(self):
        # The value is stored as submitted, so it is checked as submitted.
        response = self.order({"slug": " my-project"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_default_message_names_the_pattern(self):
        options = copy.deepcopy(PATTERN_OPTIONS)
        del options["options"]["slug"]["pattern_error"]
        response = self.order({"slug": "My Project"}, options=options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn(SLUG_PATTERN, str(response.data))

    def test_optional_option_with_pattern_may_be_omitted(self):
        response = self.order({"slug": "my-project"})
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_text_option_is_checked(self):
        response = self.order({"slug": "my-project", "notes": "<script>"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("notes", str(response.data))

    def test_hidden_option_is_not_checked(self):
        options = copy.deepcopy(PATTERN_OPTIONS)
        options["order"] = ["custom_slug", "slug", "notes"]
        options["options"]["custom_slug"] = {"type": "boolean", "label": "Custom"}
        options["options"]["slug"]["visible_if"] = {
            "field": "custom_slug",
            "values": [True],
        }
        response = self.order(
            {"custom_slug": False, "slug": "Not A Slug"}, options=options
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_invalid_stored_pattern_rejects_the_value(self):
        # Offering import writes options without validating them.
        options = copy.deepcopy(PATTERN_OPTIONS)
        options["options"]["slug"]["pattern"] = "[a-z"
        response = self.order({"slug": "my-project"}, options=options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("service provider", str(response.data))

    def test_catastrophic_pattern_times_out_instead_of_hanging(self):
        options = copy.deepcopy(PATTERN_OPTIONS)
        # The regex package guards simple nested repeats such as (a+)+b,
        # but not this one.
        options["options"]["slug"]["pattern"] = "(x+x+)+y"
        response = self.order({"slug": "x" * 5000}, options=options)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("in time", str(response.data))


class ResourceOptionsPatternTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.fixture.offering.resource_options = copy.deepcopy(PATTERN_OPTIONS)
        self.fixture.offering.save()
        self.resource = self.fixture.resource
        self.resource.state = ResourceStates.OK
        self.resource.options = {"slug": "my-project"}
        self.resource.save()
        self.url = factories.ResourceFactory.get_url(self.resource, "update_options")
        CustomerRole.OWNER.add_permission(PermissionEnum.UPDATE_RESOURCE_OPTIONS)
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_ORDER)
        self.client.force_authenticate(self.fixture.owner)

    def update(self, options):
        return self.client.post(self.url, {"options": options}, format="json")

    def test_value_not_matching_pattern_is_rejected(self):
        response = self.update({"slug": "My Project"})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options, {"slug": "my-project"})

    def test_matching_value_is_stored(self):
        response = self.update({"slug": "other-project"})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.resource.refresh_from_db()
        self.assertEqual(self.resource.options["slug"], "other-project")
