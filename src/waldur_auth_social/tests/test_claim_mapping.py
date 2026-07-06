from datetime import date

from django.test import SimpleTestCase

from waldur_auth_social.claim_mapping import (
    generate_default_mapping,
    get_available_claims_for_field,
    get_claim_suggestions_for_field,
    get_suggested_scopes,
    get_waldur_field_suggestions,
)
from waldur_auth_social.utils import normalize_mapped_claim_value


class NormalizeMappedClaimValueTest(SimpleTestCase):
    def test_birth_date_oidc_string_parsed_to_date(self):
        self.assertEqual(
            normalize_mapped_claim_value("birth_date", "1983-01-21"),
            date(1983, 1, 21),
        )

    def test_birth_date_invalid_string_returns_none(self):
        self.assertIsNone(
            normalize_mapped_claim_value("birth_date", "not-a-date"),
        )

    def test_uid_number_string_coerced_to_int(self):
        self.assertEqual(normalize_mapped_claim_value("uid_number", "50000"), 50000)
        self.assertEqual(normalize_mapped_claim_value("primary_gid", "60000"), 60000)

    def test_uid_number_non_numeric_returns_none(self):
        self.assertIsNone(normalize_mapped_claim_value("uid_number", "not-a-number"))


class ClaimMappingSuggestionsTest(SimpleTestCase):
    def test_get_claim_suggestions_for_first_name(self):
        suggestions = get_claim_suggestions_for_field("first_name")
        self.assertIn("given_name", suggestions)
        # given_name from PROVIDER_DEFAULTS should be first
        self.assertEqual(suggestions[0], "given_name")

    def test_get_claim_suggestions_for_email(self):
        suggestions = get_claim_suggestions_for_field("email")
        self.assertIn("email", suggestions)
        self.assertIn("mail", suggestions)

    def test_get_claim_suggestions_for_organization(self):
        suggestions = get_claim_suggestions_for_field("organization")
        # Should include claims from PROVIDER_DEFAULTS (keycloak uses multiple)
        self.assertIn("schac_home_organization", suggestions)
        self.assertIn("org", suggestions)

    def test_get_available_claims_for_field_exact_match(self):
        idp_claims = ["email", "given_name", "family_name", "sub"]
        available = get_available_claims_for_field("email", idp_claims)
        self.assertIn("email", available)

    def test_get_available_claims_for_field_case_insensitive(self):
        idp_claims = ["Email", "Given_Name", "Family_Name"]
        available = get_available_claims_for_field("email", idp_claims)
        self.assertIn("Email", available)

    def test_get_available_claims_for_field_no_match(self):
        idp_claims = ["sub", "name"]
        available = get_available_claims_for_field("phone_number", idp_claims)
        self.assertEqual(available, [])

    def test_get_available_claims_preserves_priority(self):
        # If multiple claims match, they should be in suggestion priority order
        # Priority is: 1) PROVIDER_DEFAULTS claims, 2) standard suggestions
        idp_claims = ["mail", "email", "emailAddress"]
        available = get_available_claims_for_field("email", idp_claims)
        # All matching claims should be present
        self.assertIn("email", available)
        self.assertIn("mail", available)
        # emailAddress is also in standard suggestions
        self.assertIn("emailAddress", available)


class WaldurFieldSuggestionsTest(SimpleTestCase):
    def test_get_waldur_field_suggestions_returns_all_fields(self):
        idp_claims = ["email", "given_name"]
        suggestions = get_waldur_field_suggestions(idp_claims)

        field_names = [s["field"] for s in suggestions]
        self.assertIn("first_name", field_names)
        self.assertIn("last_name", field_names)
        self.assertIn("email", field_names)
        self.assertIn("organization", field_names)

    def test_get_waldur_field_suggestions_includes_descriptions(self):
        idp_claims = ["email"]
        suggestions = get_waldur_field_suggestions(idp_claims)

        email_suggestion = next(s for s in suggestions if s["field"] == "email")
        self.assertIn("description", email_suggestion)
        self.assertTrue(len(email_suggestion["description"]) > 0)

    def test_get_waldur_field_suggestions_populates_available_claims(self):
        idp_claims = ["email", "given_name", "family_name"]
        suggestions = get_waldur_field_suggestions(idp_claims)

        email_suggestion = next(s for s in suggestions if s["field"] == "email")
        self.assertIn("email", email_suggestion["available_claims"])

        first_name_suggestion = next(
            s for s in suggestions if s["field"] == "first_name"
        )
        self.assertIn("given_name", first_name_suggestion["available_claims"])


class SuggestedScopesTest(SimpleTestCase):
    def test_get_suggested_scopes_always_includes_openid(self):
        scopes = get_suggested_scopes([], ["openid"])
        self.assertIn("openid", scopes)

    def test_get_suggested_scopes_includes_profile_and_email(self):
        scopes = get_suggested_scopes([], ["openid", "profile", "email", "phone"])
        self.assertIn("profile", scopes)
        self.assertIn("email", scopes)

    def test_get_suggested_scopes_based_on_claims(self):
        idp_claims = ["phone_number", "email"]
        idp_scopes = ["openid", "profile", "email", "phone"]
        scopes = get_suggested_scopes(idp_claims, idp_scopes)
        self.assertIn("phone", scopes)
        self.assertIn("email", scopes)

    def test_get_suggested_scopes_only_includes_available_scopes(self):
        idp_claims = ["phone_number"]
        idp_scopes = ["openid", "profile"]  # phone not available
        scopes = get_suggested_scopes(idp_claims, idp_scopes)
        self.assertNotIn("phone", scopes)


class GenerateDefaultMappingTest(SimpleTestCase):
    def test_generate_default_mapping_basic(self):
        idp_claims = ["email", "given_name", "family_name", "sub"]
        mapping = generate_default_mapping(idp_claims)

        self.assertIn("email", mapping)
        self.assertIn("first_name", mapping)
        self.assertIn("last_name", mapping)

    def test_generate_default_mapping_uses_available_claims(self):
        idp_claims = ["email", "given_name"]
        mapping = generate_default_mapping(idp_claims)

        self.assertEqual(mapping["email"], "email")
        self.assertEqual(mapping["first_name"], "given_name")

    def test_generate_default_mapping_multiple_claims_space_separated(self):
        idp_claims = ["email", "mail"]
        mapping = generate_default_mapping(idp_claims)

        # Both claims should be included, space-separated
        self.assertIn("email", mapping["email"])
        self.assertIn("mail", mapping["email"])

    def test_generate_default_mapping_empty_for_no_matching_claims(self):
        idp_claims = ["sub", "aud", "iss"]
        mapping = generate_default_mapping(idp_claims)

        # civil_number might match sub
        # but most fields should be empty
        self.assertNotIn("first_name", mapping)
        self.assertNotIn("last_name", mapping)
        self.assertNotIn("email", mapping)

    def test_generate_default_mapping_with_schac_claims(self):
        idp_claims = [
            "email",
            "given_name",
            "family_name",
            "schacHomeOrganization",
            "schacPersonalUniqueID",
        ]
        mapping = generate_default_mapping(idp_claims)

        self.assertIn("organization", mapping)
        self.assertIn("civil_number", mapping)
