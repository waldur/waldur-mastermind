from contextlib import redirect_stdout
from io import StringIO

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
