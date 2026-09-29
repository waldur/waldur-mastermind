from rest_framework import status, test

from waldur_core.permissions.fixtures import ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models, serializers
from waldur_mastermind.proposal.tests import factories, fixtures

EMAIL_FIELDS = {"user_email"}
FULL_NAME_FIELDS = {"user_full_name", "created_by_full_name", "user_image"}
USERNAME_FIELDS = {"user_username", "user_uuid", "created_by_uuid"}
IDENTITY_FIELDS = EMAIL_FIELDS | FULL_NAME_FIELDS | USERNAME_FIELDS


class ProposalTeamApplicantVisibilityTest(test.APITestCase):
    """The proposal team list honours the call's applicant visibility config
    for reviewer-only viewers, exactly as the proposal payload does."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.call = self.fixture.call
        self.proposal = self.fixture.proposal_submitted
        self.reviewer = self.fixture.reviewer_1
        self.applicant = self.proposal.created_by
        self.url = factories.ProposalFactory.get_url(self.proposal, action="list_users")
        self.proposal.add_user(self.applicant, ProposalRole.MANAGER)
        self.co_applicant = structure_factories.UserFactory(
            email="co-applicant@example.com"
        )
        self.proposal.add_user(
            self.co_applicant, ProposalRole.MEMBER, created_by=self.applicant
        )

    def _configure(self, **flags):
        models.CallApplicantVisibilityConfig.objects.create(call=self.call, **flags)

    def _rows_as(self, user, params=None):
        self.client.force_authenticate(user)
        response = self.client.get(self.url, params or {})
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        data = response.data
        rows = data["results"] if isinstance(data, dict) else data
        self.assertEqual(len(rows), 2)
        return rows

    def _present(self, rows):
        present = set()
        for row in rows:
            present.update(field for field in IDENTITY_FIELDS if field in row)
        return present

    def test_reviewer_does_not_see_email_when_not_exposed(self):
        self._configure(expose_email=False)
        rows = self._rows_as(self.reviewer)
        for row in rows:
            self.assertNotIn("user_email", row)
        # Other exposed attributes are still there.
        self.assertTrue(FULL_NAME_FIELDS | USERNAME_FIELDS <= self._present(rows))

    def test_reviewer_sees_email_when_exposed(self):
        self._configure(expose_email=True)
        rows = self._rows_as(self.reviewer)
        emails = {row["user_email"] for row in rows}
        self.assertIn("co-applicant@example.com", emails)

    def test_reviewer_sees_default_attributes_when_call_has_no_config(self):
        rows = self._rows_as(self.reviewer)
        self.assertEqual(self._present(rows), IDENTITY_FIELDS)

    def test_reviewer_does_not_see_name_or_avatar_when_full_name_not_exposed(self):
        self._configure(expose_full_name=False)
        present = self._present(self._rows_as(self.reviewer))
        self.assertFalse(present & FULL_NAME_FIELDS)
        self.assertTrue(EMAIL_FIELDS | USERNAME_FIELDS <= present)

    def test_reviewer_does_not_see_identity_link_when_username_not_exposed(self):
        self._configure(expose_username=False)
        present = self._present(self._rows_as(self.reviewer))
        self.assertFalse(present & USERNAME_FIELDS)
        self.assertTrue(EMAIL_FIELDS | FULL_NAME_FIELDS <= present)

    def test_reviewer_sees_no_identity_when_nothing_exposed(self):
        self._configure(
            expose_email=False, expose_full_name=False, expose_username=False
        )
        rows = self._rows_as(self.reviewer)
        self.assertEqual(self._present(rows), set())
        # Rows still carry what the review page needs to render them.
        for row in rows:
            self.assertIn("uuid", row)
            self.assertIn("role_name", row)

    def _assert_full_rows(self, user):
        self._configure(
            expose_email=False, expose_full_name=False, expose_username=False
        )
        self.assertEqual(self._present(self._rows_as(user)), IDENTITY_FIELDS)

    def test_call_manager_sees_full_rows(self):
        self._assert_full_rows(self.fixture.call_manager)

    def test_staff_sees_full_rows(self):
        self._assert_full_rows(self.fixture.staff)

    def test_support_sees_full_rows(self):
        self._assert_full_rows(self.fixture.global_support)

    def test_applicant_sees_full_rows(self):
        self._assert_full_rows(self.applicant)

    def test_team_member_sees_full_rows(self):
        self._assert_full_rows(self.co_applicant)

    def test_proposal_payload_and_team_list_agree(self):
        attribute_cases = (
            {"expose_email": False},
            {"expose_full_name": False},
            {"expose_username": False},
            {"expose_email": False, "expose_full_name": False},
        )
        detail_url = factories.ProposalFactory.get_url(self.proposal)
        for flags in attribute_cases:
            with self.subTest(flags=flags):
                models.CallApplicantVisibilityConfig.objects.filter(
                    call=self.call
                ).delete()
                self._configure(**flags)
                self.client.force_authenticate(self.reviewer)
                payload = self.client.get(detail_url).data
                team_fields = self._present(self._rows_as(self.reviewer))
                for attribute in ("email", "full_name", "username"):
                    shown_on_proposal = any(
                        field in payload
                        for field in serializers.APPLICANT_FIELD_MAP[attribute]
                    )
                    shown_on_team = bool(
                        team_fields & set(serializers.TEAM_MEMBER_FIELD_MAP[attribute])
                    )
                    self.assertEqual(
                        shown_on_proposal, shown_on_team, (attribute, flags)
                    )


class ProposalTeamIdentityQueryTest(test.APITestCase):
    """A reviewer may not filter, search or order the team by an attribute the
    call conceals: the response would reveal it even with the column dropped."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal_submitted
        self.url = factories.ProposalFactory.get_url(self.proposal, action="list_users")
        self.member = structure_factories.UserFactory(
            email="member@example.com", username="member-login"
        )
        self.proposal.add_user(self.member, ProposalRole.MEMBER)

    def _get(self, user, params):
        self.client.force_authenticate(user)
        return self.client.get(self.url, params)

    def test_reviewer_cannot_probe_concealed_email(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.fixture.call, expose_email=False
        )
        for params in (
            {"search_string": "member@example.com"},
            {"o": "email"},
            {"o": "-email"},
        ):
            with self.subTest(params=params):
                response = self._get(self.fixture.reviewer_1, params)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )

    def test_reviewer_cannot_probe_concealed_username(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.fixture.call, expose_username=False
        )
        for params in (
            {"username": "member-login"},
            {"user_slug": "member"},
            {"user": self.member.uuid.hex},
            {"o": "username"},
        ):
            with self.subTest(params=params):
                response = self._get(self.fixture.reviewer_1, params)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )

    def test_reviewer_cannot_probe_concealed_name(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.fixture.call, expose_full_name=False
        )
        for params in (
            {"full_name": self.member.first_name},
            {"native_name": "x"},
            {"o": "full_name"},
        ):
            with self.subTest(params=params):
                response = self._get(self.fixture.reviewer_1, params)
                self.assertEqual(
                    response.status_code, status.HTTP_400_BAD_REQUEST, response.data
                )

    def test_reviewer_may_query_by_exposed_attributes(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.fixture.call, expose_email=False
        )
        for params in ({"username": "member-login"}, {"o": "-username"}, {}):
            with self.subTest(params=params):
                response = self._get(self.fixture.reviewer_1, params)
                self.assertEqual(
                    response.status_code, status.HTTP_200_OK, response.data
                )

    def test_call_manager_may_search_by_email(self):
        models.CallApplicantVisibilityConfig.objects.create(
            call=self.fixture.call, expose_email=False
        )
        response = self._get(
            self.fixture.call_manager, {"search_string": "member@example.com"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(len(response.data), 1)


class FieldMapCoverageTest(test.APISimpleTestCase):
    def test_maps_only_use_attributes_the_config_governs(self):
        governed = set(models.CallApplicantVisibilityConfig.get_attribute_names())
        self.assertLessEqual(set(serializers.APPLICANT_FIELD_MAP), governed)
        self.assertLessEqual(set(serializers.TEAM_MEMBER_FIELD_MAP), governed)
        self.assertLessEqual(set(serializers.TEAM_MEMBER_QUERY_MAP), governed)
