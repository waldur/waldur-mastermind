from django.test import SimpleTestCase

from waldur_core.checklist import enums
from waldur_core.checklist.utils import SELECT_QUESTION_TYPES, map_option_references


class MapOptionReferencesTest(SimpleTestCase):
    mapping = {"a": "x", "b": "y"}

    def test_maps_one_reference(self):
        self.assertEqual(map_option_references("a", self.mapping), "x")

    def test_maps_a_list_of_references(self):
        self.assertEqual(map_option_references(["a", "b"], self.mapping), ["x", "y"])

    def test_keeps_none(self):
        self.assertIsNone(map_option_references(None, self.mapping))

    def test_refuses_an_unknown_reference(self):
        with self.assertRaises(KeyError):
            map_option_references(["a", "c"], self.mapping)

    def test_select_types(self):
        self.assertEqual(
            SELECT_QUESTION_TYPES,
            {enums.QuestionTypes.SINGLE_SELECT, enums.QuestionTypes.MULTI_SELECT},
        )
