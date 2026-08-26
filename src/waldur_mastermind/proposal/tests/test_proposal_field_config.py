"""Per-call configuration of the Project details fields (#291)."""

from constance.test.unittest import override_config
from ddt import data, ddt
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.enums import ProposalFieldStates
from waldur_mastermind.proposal.tests import factories, fixtures


class SeedingTest(test.APITestCase):
    """A call carries a concrete config from the moment it is created.

    Seeded rather than resolved on read, so that raising the installation
    default later cannot tighten a call that is already running.
    """

    def test_new_call_is_seeded_from_constance_defaults(self):
        call = factories.CallFactory()

        config = models.CallProposalFieldConfig.objects.get(call=call)
        self.assertEqual(
            config.get_states(),
            {
                "project_summary": ProposalFieldStates.REQUIRED,
                "description": ProposalFieldStates.OPTIONAL,
                "science_sub_domain": ProposalFieldStates.OPTIONAL,
                "supporting_documentation": ProposalFieldStates.OPTIONAL,
            },
        )

    @override_config(
        DEFAULT_PROPOSAL_REQUIRED_FIELDS=["project_summary", "description"],
        DEFAULT_PROPOSAL_HIDDEN_FIELDS=["supporting_documentation"],
    )
    def test_defaults_are_read_at_creation(self):
        call = factories.CallFactory()

        states = models.CallProposalFieldConfig.objects.get(call=call).get_states()
        self.assertEqual(states["description"], ProposalFieldStates.REQUIRED)
        self.assertEqual(states["supporting_documentation"], ProposalFieldStates.HIDDEN)

    def test_raising_the_installation_default_leaves_existing_calls_alone(self):
        call = factories.CallFactory()

        with override_config(
            DEFAULT_PROPOSAL_REQUIRED_FIELDS=[
                "project_summary",
                "description",
                "science_sub_domain",
            ]
        ):
            states = models.CallProposalFieldConfig.get_states_for_call(call)

        self.assertEqual(states["description"], ProposalFieldStates.OPTIONAL)
        self.assertEqual(states["science_sub_domain"], ProposalFieldStates.OPTIONAL)

    def test_a_call_without_a_config_row_falls_back_to_model_defaults(self):
        call = factories.CallFactory()
        models.CallProposalFieldConfig.objects.filter(call=call).delete()
        call.refresh_from_db()

        states = models.CallProposalFieldConfig.get_states_for_call(call)

        self.assertEqual(states["project_summary"], ProposalFieldStates.REQUIRED)
        self.assertEqual(states["description"], ProposalFieldStates.OPTIONAL)


@ddt
class ConfigurationApiTest(test.APITestCase):
    """Reading and writing the configuration through the protected call API.

    The fixture's call already carries a proposal, so every change made here is
    a loosening one; tightening is covered in LockingTest.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.url = factories.CallFactory.get_protected_url(self.call)

    def patch_config(self, user, payload):
        self.client.force_authenticate(getattr(self.fixture, user))
        return self.client.patch(self.url, {"proposal_field_config": payload})

    def current_states(self):
        """Read the row, not call.proposal_field_config.

        Creating the config in the seeding handler populates the reverse
        one-to-one cache on the Call instance the fixture holds, so reading
        through it would return the values from before the request.
        """
        return models.CallProposalFieldConfig.objects.get(call=self.call).get_states()

    def test_manager_can_relax_a_field(self):
        response = self.patch_config(
            "call_manager", {"field_project_summary": ProposalFieldStates.OPTIONAL}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.current_states()["project_summary"], ProposalFieldStates.OPTIONAL
        )

    def test_manager_can_hide_a_field(self):
        response = self.patch_config(
            "call_manager", {"field_description": ProposalFieldStates.HIDDEN}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(
            self.current_states()["description"], ProposalFieldStates.HIDDEN
        )

    def test_a_partial_update_leaves_the_other_fields_alone(self):
        # Loosening rather than tightening: the fixture's call already has a
        # proposal, so nothing here may become required (see LockingTest).
        self.patch_config(
            "call_manager", {"field_science_sub_domain": ProposalFieldStates.HIDDEN}
        )

        states = self.current_states()
        self.assertEqual(states["science_sub_domain"], ProposalFieldStates.HIDDEN)
        self.assertEqual(states["project_summary"], ProposalFieldStates.REQUIRED)
        self.assertEqual(states["description"], ProposalFieldStates.OPTIONAL)

    def test_metadata_reports_state_transitions_and_consumers(self):
        self.client.force_authenticate(self.fixture.call_manager)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        metadata = {
            row["field"]: row for row in response.data["proposal_field_metadata"]
        }
        self.assertEqual(
            set(metadata), set(models.CallProposalFieldConfig.field_names())
        )
        summary = metadata["project_summary"]
        self.assertEqual(summary["state"], ProposalFieldStates.REQUIRED)
        self.assertIsNone(summary["locked_reason"])
        self.assertIn(ProposalFieldStates.REQUIRED, summary["allowed_states"])
        # The summary feeds reviewer matching; a manager hiding it should be
        # told so before they do.
        self.assertIn("reviewer_matching", summary["usage"])


class LockingTest(test.APITestCase):
    """A field cannot become required once the call has proposals."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.proposal.round.call
        self.url = factories.CallFactory.get_protected_url(self.call)
        self.client.force_authenticate(self.fixture.staff)

    def patch_config(self, payload):
        return self.client.patch(self.url, {"proposal_field_config": payload})

    def test_optional_to_required_is_refused(self):
        response = self.patch_config(
            {"field_description": ProposalFieldStates.REQUIRED}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("description", str(response.data))
        self.assertEqual(
            models.CallProposalFieldConfig.objects.get(call=self.call).get_states()[
                "description"
            ],
            ProposalFieldStates.OPTIONAL,
        )

    def test_hidden_to_required_is_refused(self):
        config = models.CallProposalFieldConfig.objects.get(call=self.call)
        config.field_science_sub_domain = ProposalFieldStates.HIDDEN
        config.save()

        response = self.patch_config(
            {"field_science_sub_domain": ProposalFieldStates.REQUIRED}
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_restating_an_already_required_field_is_allowed(self):
        response = self.patch_config(
            {"field_project_summary": ProposalFieldStates.REQUIRED}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_loosening_stays_open(self):
        response = self.patch_config(
            {
                "field_project_summary": ProposalFieldStates.OPTIONAL,
                "field_description": ProposalFieldStates.HIDDEN,
            }
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_tightening_is_allowed_while_the_call_has_no_proposals(self):
        empty_call = self.fixture.new_call
        response = self.client.patch(
            factories.CallFactory.get_protected_url(empty_call),
            {
                "proposal_field_config": {
                    "field_description": ProposalFieldStates.REQUIRED
                }
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)

    def test_metadata_marks_the_locked_fields(self):
        response = self.client.get(self.url)

        metadata = {
            row["field"]: row for row in response.data["proposal_field_metadata"]
        }
        self.assertNotIn(
            ProposalFieldStates.REQUIRED, metadata["description"]["allowed_states"]
        )
        self.assertIsNotNone(metadata["description"]["locked_reason"])
        # Already required, so nothing to tighten and nothing to explain.
        self.assertIn(
            ProposalFieldStates.REQUIRED,
            metadata["project_summary"]["allowed_states"],
        )


@ddt
class SubmitValidationTest(test.APITestCase):
    """Requiredness is enforced by the API, not only by a disabled button."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.call = self.proposal.round.call
        self.url = factories.ProposalFactory.get_url(self.proposal, "submit")
        self.client.force_authenticate(self.proposal.created_by)

    def set_state(self, field_name, state):
        config = models.CallProposalFieldConfig.objects.get(call=self.call)
        setattr(config, models.CallProposalFieldConfig.column_for(field_name), state)
        config.save()

    def test_submitting_without_a_required_summary_is_refused(self):
        self.proposal.project_summary = ""
        self.proposal.save()

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("project_summary", str(response.data))

    @data("optional", "hidden")
    def test_an_unrequired_summary_does_not_block_submission(self, state):
        self.proposal.project_summary = ""
        self.proposal.save()
        self.set_state("project_summary", state)

        response = self.client.post(self.url)

        self.assertNotIn("project_summary", str(response.data))

    def test_a_required_science_domain_must_be_set(self):
        self.set_state("science_sub_domain", ProposalFieldStates.REQUIRED)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("science_sub_domain", str(response.data))

        self.proposal.science_sub_domain = structure_factories.ScienceSubDomainFactory()
        self.proposal.save()

        response = self.client.post(self.url)

        self.assertNotIn("science_sub_domain", str(response.data))

    def test_a_required_documentation_field_counts_attachments(self):
        self.set_state("supporting_documentation", ProposalFieldStates.REQUIRED)

        response = self.client.post(self.url)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("supporting_documentation", str(response.data))


class DuplicateCallTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        config = models.CallProposalFieldConfig.objects.get(call=self.call)
        config.field_description = ProposalFieldStates.REQUIRED
        config.field_science_sub_domain = ProposalFieldStates.HIDDEN
        config.save()
        self.client.force_authenticate(self.fixture.staff)

    def test_duplicate_carries_the_field_config_over(self):
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, "duplicate"),
            {"name": "Copy of the call"},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_call = models.Call.objects.get(uuid=response.data["uuid"])
        states = models.CallProposalFieldConfig.get_states_for_call(new_call)
        self.assertEqual(states["description"], ProposalFieldStates.REQUIRED)
        self.assertEqual(states["science_sub_domain"], ProposalFieldStates.HIDDEN)

    def test_duplicate_can_skip_the_field_config(self):
        response = self.client.post(
            factories.CallFactory.get_protected_url(self.call, "duplicate"),
            {"name": "Copy without config", "copy_proposal_field_config": False},
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        new_call = models.Call.objects.get(uuid=response.data["uuid"])
        states = models.CallProposalFieldConfig.get_states_for_call(new_call)
        self.assertEqual(states["description"], ProposalFieldStates.OPTIONAL)
