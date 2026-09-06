from django.test import SimpleTestCase

from waldur_core.core import validators
from waldur_core.core.countries import ISO_3166_1
from waldur_core.core.fields import COUNTRIES, COUNTRIES_DICT


class CountriesTest(SimpleTestCase):
    def test_table_is_complete_and_unique(self):
        codes = [code for code, _name in ISO_3166_1]
        self.assertEqual(len(codes), 249)
        self.assertEqual(len(set(codes)), len(codes))
        self.assertTrue(all(len(code) == 2 and code.isupper() for code in codes))
        self.assertTrue(all(name for _code, name in ISO_3166_1))

    def test_known_entries(self):
        self.assertEqual(COUNTRIES_DICT["EE"], "Estonia")
        self.assertEqual(COUNTRIES_DICT["DE"], "Germany")
        self.assertEqual(COUNTRIES_DICT["EU"], "European Union")
        self.assertNotIn("EU", dict(ISO_3166_1))

    def test_validator_codes_match_choices(self):
        self.assertEqual(
            validators.ISO_3166_1_ALPHA_2_CODES, {code for code, _name in COUNTRIES}
        )
