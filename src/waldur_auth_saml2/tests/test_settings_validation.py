from django.test import SimpleTestCase
from pydantic.v1 import ValidationError

from waldur_core.core.metadata import WaldurAuthSAML2


class SamlAttributeMappingValidationTest(SimpleTestCase):
    """SAML_ATTRIBUTE_MAPPING is handed to djangosaml2, which iterates each value.

    A mapping value must therefore be a sequence of destination fields, not a
    bare string -- a string would be iterated character by character.
    """

    def test_list_of_destination_fields_is_accepted(self):
        config = WaldurAuthSAML2(
            SAML_ATTRIBUTE_MAPPING={
                "eduPersonPrincipalName": ["username"],
                "mail": ["email"],
            }
        )
        self.assertEqual(
            config.SAML_ATTRIBUTE_MAPPING,
            {"eduPersonPrincipalName": ["username"], "mail": ["email"]},
        )

    def test_tuple_of_destination_fields_is_accepted(self):
        # djangosaml2 documents the mapping with tuples.
        config = WaldurAuthSAML2(SAML_ATTRIBUTE_MAPPING={"uid": ("username",)})
        self.assertEqual(config.SAML_ATTRIBUTE_MAPPING, {"uid": ["username"]})

    def test_multiple_destination_fields_are_preserved(self):
        config = WaldurAuthSAML2(
            SAML_ATTRIBUTE_MAPPING={"cn": ["full_name", "username"]}
        )
        self.assertEqual(
            config.SAML_ATTRIBUTE_MAPPING, {"cn": ["full_name", "username"]}
        )

    def test_bare_string_is_rejected(self):
        with self.assertRaises(ValidationError) as context:
            WaldurAuthSAML2(SAML_ATTRIBUTE_MAPPING={"mail": "email"})
        errors = context.exception.errors()
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["loc"], ("SAML_ATTRIBUTE_MAPPING", "mail"))
        self.assertEqual(errors[0]["type"], "type_error.list")
