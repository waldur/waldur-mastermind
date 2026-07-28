"""
Tests for per-call COI configuration validation (WAL-9601).

The three type-handling rules (recusal / management / disclosure) must stay
mutually exclusive, and each entry must be a valid COI type code.
"""

from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import CallStates, COITypes
from waldur_mastermind.proposal.tests import factories


class COIConfigurationValidationTestCase(APITestCase):
    """The type-handling rule lists must be disjoint and use valid type codes."""

    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        manager_org = factories.CallManagingOrganisationFactory(customer=self.customer)
        self.call = factories.CallFactory(manager=manager_org, state=CallStates.ACTIVE)
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    @property
    def url(self):
        return f"/api/proposal-protected-calls/{self.call.uuid.hex}/coi-configuration/"

    def _patch(self, payload):
        return self.client.patch(self.url, payload, format="json")

    def test_disjoint_configuration_is_accepted(self):
        response = self._patch(
            {
                "recusal_required_types": [COITypes.INST_SAME],
                "management_allowed_types": [COITypes.COMPET],
                "disclosure_only_types": [COITypes.SOC_MEMBER],
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.recusal_required_types, [COITypes.INST_SAME])
        self.assertEqual(config.management_allowed_types, [COITypes.COMPET])
        self.assertEqual(config.disclosure_only_types, [COITypes.SOC_MEMBER])

    def test_same_type_in_two_rules_in_one_request_is_rejected(self):
        response = self._patch(
            {
                "recusal_required_types": [COITypes.INST_SAME],
                "management_allowed_types": [COITypes.INST_SAME],
            }
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        # Nothing should have been persisted.
        self.assertFalse(
            models.CallCOIConfiguration.objects.filter(
                call=self.call, recusal_required_types__contains=[COITypes.INST_SAME]
            ).exists()
        )

    def test_partial_update_overlap_with_existing_value_is_rejected(self):
        # First assign the type to the recusal rule.
        first = self._patch({"recusal_required_types": [COITypes.INST_SAME]})
        self.assertEqual(first.status_code, status.HTTP_200_OK, first.data)

        # Now try to also add it to the management rule via a partial update.
        second = self._patch({"management_allowed_types": [COITypes.INST_SAME]})
        self.assertEqual(second.status_code, status.HTTP_400_BAD_REQUEST)

        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.management_allowed_types, [])

    def test_reassigning_a_type_between_rules_in_one_request_is_accepted(self):
        # Assign to recusal first.
        self._patch({"recusal_required_types": [COITypes.INST_SAME]})

        # Move it to management by clearing recusal and setting management at once.
        response = self._patch(
            {
                "recusal_required_types": [],
                "management_allowed_types": [COITypes.INST_SAME],
            }
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.recusal_required_types, [])
        self.assertEqual(config.management_allowed_types, [COITypes.INST_SAME])

    def test_invalid_coi_type_is_rejected(self):
        response = self._patch({"recusal_required_types": ["NOT_A_REAL_COI_TYPE"]})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("recusal_required_types", response.data)

    def _config_with_preexisting_overlap(self):
        """A configuration that already overlapped before the rule existed."""
        return models.CallCOIConfiguration.objects.create(
            call=self.call,
            recusal_required_types=[COITypes.INST_SAME],
            management_allowed_types=[COITypes.INST_SAME],
        )

    def test_preexisting_overlap_is_left_as_is(self):
        """Existing assignments are not rewritten or frozen — only new overlaps
        are blocked, so an untouched legacy overlap must not fail unrelated edits.
        """
        self._config_with_preexisting_overlap()

        response = self._patch({"coauthorship_lookback_years": 5})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.coauthorship_lookback_years, 5)
        self.assertEqual(config.recusal_required_types, [COITypes.INST_SAME])
        self.assertEqual(config.management_allowed_types, [COITypes.INST_SAME])

    def test_preexisting_overlap_does_not_block_an_untouched_rule(self):
        self._config_with_preexisting_overlap()

        # The stale INST_SAME overlap sits in the two rules left untouched.
        response = self._patch({"disclosure_only_types": [COITypes.SOC_MEMBER]})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.disclosure_only_types, [COITypes.SOC_MEMBER])

    def test_preexisting_overlap_survives_resubmitting_the_same_value(self):
        """Saving a rule back unchanged is not a rewrite.

        The edit form keeps an already-assigned type visible in the rule that
        holds it, so a no-op save resubmits the colliding field verbatim. That
        must not be turned into a demand to clean up a legacy overlap first.
        """
        self._config_with_preexisting_overlap()

        response = self._patch({"management_allowed_types": [COITypes.INST_SAME]})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.recusal_required_types, [COITypes.INST_SAME])
        self.assertEqual(config.management_allowed_types, [COITypes.INST_SAME])

    def test_preexisting_overlap_may_not_be_extended(self):
        self._config_with_preexisting_overlap()

        # Writing to a rule that holds the stale overlap must not sneak it past.
        response = self._patch(
            {"management_allowed_types": [COITypes.INST_SAME, COITypes.COMPET]}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.management_allowed_types, [COITypes.INST_SAME])

    def test_preexisting_overlap_can_be_resolved(self):
        self._config_with_preexisting_overlap()

        response = self._patch({"management_allowed_types": []})

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        config = models.CallCOIConfiguration.objects.get(call=self.call)
        self.assertEqual(config.recusal_required_types, [COITypes.INST_SAME])
        self.assertEqual(config.management_allowed_types, [])

    def test_error_names_the_type_and_the_colliding_rules(self):
        response = self._patch(
            {
                "recusal_required_types": [COITypes.INST_SAME],
                "management_allowed_types": [COITypes.INST_SAME],
            }
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        message = str(response.data["non_field_errors"][0])
        self.assertIn(COITypes.INST_SAME, message)
        self.assertIn("recusal_required_types", message)
        self.assertIn("management_allowed_types", message)
