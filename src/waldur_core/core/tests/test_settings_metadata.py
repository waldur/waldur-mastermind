from contextlib import redirect_stdout
from io import StringIO

from django.conf import settings
from django.core.management import call_command
from rest_framework import status, test


class SettingsMetadataCountriesTest(test.APITestCase):
    """The country list setting must advertise every valid country, not just the default subset."""

    def setUp(self):
        self.url = "/api/metadata/settings/"

    def _get_countries_item(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        for section in response.data["settings"]:
            for item in section["items"]:
                if item["key"] == "COUNTRIES":
                    return item
        self.fail("COUNTRIES is missing from settings metadata")

    def test_country_list_field_exposes_all_countries_as_options(self):
        item = self._get_countries_item()
        self.assertEqual(item["type"], "country_list_field")
        options = {option["value"]: option["label"] for option in item["options"]}
        # Codes outside the shipped European default must be offered as well.
        self.assertIn("US", options)
        self.assertIn("JP", options)
        self.assertIn("EU", options)
        self.assertEqual(options["JP"], "Japan")
        self.assertGreater(len(options), len(item["default"]))

    def test_default_stays_the_shipped_subset(self):
        item = self._get_countries_item()
        self.assertIn("EE", item["default"])
        self.assertNotIn("US", item["default"])

    def _render_typescript(self):
        out = StringIO()
        # The command writes to stdout directly, as CI redirects it to a file.
        with redirect_stdout(out):
            call_command("print_settings_description")
        return out.getvalue()

    def test_typescript_description_includes_country_options(self):
        output = self._render_typescript()
        countries_block = output.split("key: 'COUNTRIES',")[1].split("\n      },")[0]
        self.assertIn("{ value: 'US', label: '", countries_block)
        self.assertIn("{ value: 'JP', label: 'Japan' }", countries_block)
        self.assertIn("{ value: 'EU', label: 'European Union' }", countries_block)

    def test_typescript_description_escapes_quotes_in_labels(self):
        output = self._render_typescript()
        # Country names such as "Lao People's Democratic Republic" would
        # otherwise terminate the generated single-quoted string early.
        self.assertNotIn("label: 'Lao People's", output)
        self.assertIn("People\\'s", output)


class SettingsMetadataTypeTest(test.APITestCase):
    """The API and the generated TypeScript must report the same setting types."""

    def _api_types(self):
        response = self.client.get("/api/metadata/settings/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {
            item["key"]: item["type"]
            for section in response.data["settings"]
            for item in section["items"]
        }

    def test_python_type_is_mapped_to_type_name(self):
        types = self._api_types()
        self.assertEqual(types["WALDUR_SUPPORT_SLA_RESPONSE_HOURS"], "integer")
        self.assertEqual(types["WALDUR_SUPPORT_SLA_RESOLUTION_HOURS"], "integer")

    def test_api_types_match_typescript_description(self):
        out = StringIO()
        with redirect_stdout(out):
            call_command("print_settings_description")
        rendered = out.getvalue()
        for key, value_type in self._api_types().items():
            self.assertIn(
                f"key: '{key}',", rendered, f"{key} is missing from TypeScript"
            )
            block = rendered.split(f"key: '{key}',")[1].split("\n      },")[0]
            self.assertIn(f"type: '{value_type}',", block, key)


class SettingsFieldsetsTest(test.APITestCase):
    def test_each_key_belongs_to_one_fieldset(self):
        seen = {}
        for title, keys in settings.CONSTANCE_CONFIG_FIELDSETS.items():
            for key in keys:
                self.assertNotIn(
                    key, seen, f"{key} is in both '{seen.get(key)}' and '{title}'"
                )
                seen[key] = title
