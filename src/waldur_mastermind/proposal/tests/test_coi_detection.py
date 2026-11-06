"""Tests for COI (Conflict of Interest) detection functionality."""

from datetime import date, timedelta
from unittest.mock import patch

from django.test import TestCase
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CallRole, ProposalRole
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.coi_detection import (
    detect_coauthorship_conflicts,
    detect_institutional_conflicts,
    detect_named_personnel_conflicts,
    fuzzy_name_match,
    normalize_name,
    run_coi_detection_for_call,
    run_coi_detection_for_pair,
)
from waldur_mastermind.proposal.enums import (
    COIDetectionJobStates,
    COISeverityLevels,
    COIStatuses,
    COITypes,
    ReviewerPoolInvitationStatuses,
)
from waldur_mastermind.proposal.tests import factories


class NameMatchingTest(TestCase):
    """Test fuzzy name matching utilities."""

    def test_normalize_name(self):
        self.assertEqual(normalize_name("John  Smith"), "john smith")
        self.assertEqual(normalize_name("  Jane   Doe  "), "jane doe")
        self.assertEqual(normalize_name(""), "")
        self.assertEqual(normalize_name(None), "")

    def test_exact_match(self):
        self.assertTrue(fuzzy_name_match("John Smith", "john smith"))
        self.assertTrue(fuzzy_name_match("Jane Doe", "JANE DOE"))

    def test_fuzzy_match(self):
        self.assertTrue(fuzzy_name_match("John Smith", "Jon Smith", threshold=0.8))
        self.assertTrue(
            fuzzy_name_match("Michael Johnson", "Michel Johnson", threshold=0.85)
        )

    def test_no_match(self):
        self.assertFalse(fuzzy_name_match("John Smith", "Jane Doe"))
        self.assertFalse(fuzzy_name_match("Alice Brown", "Bob Wilson"))

    def test_empty_names(self):
        self.assertFalse(fuzzy_name_match("", "John Smith"))
        self.assertFalse(fuzzy_name_match("John Smith", ""))
        self.assertFalse(fuzzy_name_match(None, "John Smith"))


class CoauthorshipConflictDetectionTest(TestCase):
    """Co-authorship between the reviewer and the proposal's applicant team."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.reviewer = factories.ReviewerProfileFactory()
        self.config = factories.CallCOIConfigurationFactory(
            call=self.call,
            coauthorship_lookback_years=3,
            coauthorship_threshold_papers=1,
        )
        self.member = structure_factories.UserFactory(full_name="Alice Johnson")
        self.proposal.add_user(self.member, ProposalRole.MEMBER)

    def publish(self, year=None, **kwargs):
        return factories.ReviewerPublicationFactory(
            reviewer_profile=self.reviewer,
            publication_year=year or date.today().year,
            **kwargs,
        )

    def detect(self):
        return detect_coauthorship_conflicts(self.reviewer, self.proposal, self.config)

    def test_no_conflict_without_shared_publications(self):
        self.publish(coauthors=[{"name": "Random Person", "orcid": None}])
        self.assertEqual(self.detect(), [])

    def test_conflict_detected_by_name_match(self):
        self.publish(coauthors=[{"name": "Alice Johnson", "orcid": None}])

        (conflict,) = self.detect()

        self.assertEqual(conflict.coi_type, COITypes.COAUTH_RECENT)
        self.assertEqual(conflict.severity, COISeverityLevels.APPARENT)
        self.assertEqual(conflict.conflicting_user, self.member)
        self.assertEqual(conflict.status, COIStatuses.PENDING)
        (shared,) = conflict.evidence_data["shared_publications"]
        self.assertEqual(shared["match_type"], "name")

    def test_conflict_detected_by_coauthor_listed_as_a_plain_name(self):
        self.publish(coauthors=["Alice Johnson"])
        (conflict,) = self.detect()
        self.assertEqual(conflict.conflicting_user, self.member)

    def test_conflict_detected_by_orcid_match(self):
        factories.ReviewerProfileFactory(
            user=self.member, orcid_id="0000-0001-2345-6789"
        )
        self.publish(coauthors=[{"name": "A. J.", "orcid": "0000-0001-2345-6789"}])

        (conflict,) = self.detect()

        self.assertEqual(conflict.conflicting_user, self.member)
        (shared,) = conflict.evidence_data["shared_publications"]
        self.assertEqual(shared["match_type"], "orcid")

    def test_conflict_detected_by_team_members_alternative_name(self):
        factories.ReviewerProfileFactory(
            user=self.member, alternative_names=["A. M. Johnson"]
        )
        self.publish(coauthors=[{"name": "A. M. Johnson", "orcid": None}])
        (conflict,) = self.detect()
        self.assertEqual(conflict.conflicting_user, self.member)

    def test_conflict_detected_by_a_publication_both_list(self):
        """The same DOI on the reviewer's and a team member's publication
        list is a shared paper, whoever the coauthor list names."""
        member_profile = factories.ReviewerProfileFactory(user=self.member)
        self.publish(doi="10.5555/shared.1")
        factories.ReviewerPublicationFactory(
            reviewer_profile=member_profile,
            doi="10.5555/SHARED.1",
            publication_year=date.today().year,
        )

        (conflict,) = self.detect()

        self.assertEqual(conflict.conflicting_user, self.member)
        (shared,) = conflict.evidence_data["shared_publications"]
        self.assertEqual(shared["match_type"], "doi")

    def test_distant_coauthorship_is_a_potential_conflict(self):
        self.publish(
            year=date.today().year - 2,
            coauthors=[{"name": "Alice Johnson", "orcid": None}],
        )
        (conflict,) = self.detect()
        self.assertEqual(conflict.coi_type, COITypes.COAUTH_OLD)
        self.assertEqual(conflict.severity, COISeverityLevels.POTENTIAL)

    def test_the_author_removed_from_the_team_is_not_matched(self):
        author = self.proposal.created_by
        author.full_name = "Bob Author"
        author.save()
        self.publish(coauthors=[{"name": "Bob Author", "orcid": None}])
        self.assertEqual(self.detect(), [])

        self.proposal.add_user(author, ProposalRole.MANAGER)
        (conflict,) = self.detect()
        self.assertEqual(conflict.conflicting_user, author)

    def test_the_reviewer_on_the_team_is_not_their_own_coauthor(self):
        self.proposal.add_user(self.reviewer.user, ProposalRole.MEMBER)
        self.publish(coauthors=[{"name": "Random Person", "orcid": None}])
        self.assertEqual(self.detect(), [])

    def test_old_publications_outside_lookback_ignored(self):
        self.publish(
            year=date.today().year - 5,
            coauthors=[{"name": "Alice Johnson", "orcid": None}],
        )
        self.assertEqual(self.detect(), [])


class InstitutionalConflictDetectionTest(TestCase):
    """Test institutional affiliation based COI detection."""

    def setUp(self):
        # Create organization (customer)
        self.organization = structure_factories.CustomerFactory()

        # Create call with round and proposal linked to organization
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.project = structure_factories.ProjectFactory(customer=self.organization)
        self.proposal = factories.ProposalFactory(
            round=self.round, project=self.project
        )

        # Create reviewer
        self.reviewer = factories.ReviewerProfileFactory()

        # Create COI configuration
        self.config = factories.CallCOIConfigurationFactory(
            call=self.call,
            institutional_lookback_years=2,
            include_same_institution=True,
        )

    def test_no_conflict_without_shared_institution(self):
        """No conflict when reviewer has different institution."""
        other_org = structure_factories.CustomerFactory()
        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer,
            organization=other_org,
            end_date=None,  # Current affiliation
        )

        conflicts = detect_institutional_conflicts(
            self.reviewer, self.proposal, self.config
        )
        self.assertEqual(len(conflicts), 0)

    def test_current_same_institution_conflict(self):
        """Conflict detected when reviewer currently at same institution."""
        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer,
            organization=self.organization,
            end_date=None,  # Current affiliation
        )

        conflicts = detect_institutional_conflicts(
            self.reviewer, self.proposal, self.config
        )

        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict.coi_type, COITypes.INST_SAME)
        self.assertEqual(conflict.severity, COISeverityLevels.REAL)
        self.assertEqual(conflict.status, COIStatuses.PENDING)

    def test_former_institution_conflict(self):
        """Conflict detected when reviewer was recently at same institution."""
        end_date = date.today() - timedelta(days=180)  # 6 months ago
        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer,
            organization=self.organization,
            end_date=end_date,  # Former affiliation
        )

        conflicts = detect_institutional_conflicts(
            self.reviewer, self.proposal, self.config
        )

        self.assertEqual(len(conflicts), 1)
        conflict = conflicts[0]
        self.assertEqual(conflict.coi_type, COITypes.INST_FORMER)
        self.assertEqual(conflict.severity, COISeverityLevels.APPARENT)

    def test_old_affiliation_outside_lookback_ignored(self):
        """Former affiliation outside lookback window is ignored."""
        end_date = date.today() - timedelta(days=1000)  # ~3 years ago
        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer,
            organization=self.organization,
            end_date=end_date,
        )

        conflicts = detect_institutional_conflicts(
            self.reviewer, self.proposal, self.config
        )
        self.assertEqual(len(conflicts), 0)

    def test_detection_disabled_by_config(self):
        """No detection when include_same_institution is False."""
        self.config.include_same_institution = False
        self.config.save()

        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer,
            organization=self.organization,
            end_date=None,
        )

        conflicts = detect_institutional_conflicts(
            self.reviewer, self.proposal, self.config
        )
        self.assertEqual(len(conflicts), 0)


class NamedPersonnelConflictDetectionTest(TestCase):
    """The reviewer is on the proposal's applicant team."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.reviewer = factories.ReviewerProfileFactory(
            user=structure_factories.UserFactory(
                full_name="Maria Garcia", email="maria@example.com"
            )
        )

    def detect(self):
        return detect_named_personnel_conflicts(self.reviewer, self.proposal)

    def test_conflict_detected_when_reviewer_is_on_the_team(self):
        self.proposal.add_user(self.reviewer.user, ProposalRole.MANAGER)

        (conflict,) = self.detect()

        self.assertEqual(conflict.coi_type, COITypes.ROLE_NAMED)
        self.assertEqual(conflict.severity, COISeverityLevels.REAL)
        self.assertEqual(conflict.conflicting_user, self.reviewer.user)
        self.assertEqual(conflict.evidence_data["match_reason"], "User account match")

    def test_the_author_counts_only_while_on_the_team(self):
        self.proposal.created_by = self.reviewer.user
        self.proposal.save()
        self.assertEqual(self.detect(), [])

    def test_conflict_detected_by_email_of_another_account(self):
        other = structure_factories.UserFactory(
            full_name="Someone Else", email="MARIA@example.com"
        )
        self.proposal.add_user(other, ProposalRole.MEMBER)

        (conflict,) = self.detect()

        self.assertEqual(conflict.conflicting_user, other)
        self.assertTrue(conflict.evidence_data["match_reason"].startswith("Email"))

    def test_conflict_detected_by_name(self):
        other = structure_factories.UserFactory(full_name="maria  garcia")
        self.proposal.add_user(other, ProposalRole.MEMBER)
        (conflict,) = self.detect()
        self.assertTrue(conflict.evidence_data["match_reason"].startswith("Name"))

    def test_conflict_detected_by_reviewers_alternative_name(self):
        self.reviewer.alternative_names = ["M. Garcia-Lopez"]
        self.reviewer.save()
        other = structure_factories.UserFactory(full_name="M. Garcia-Lopez")
        self.proposal.add_user(other, ProposalRole.MEMBER)
        (conflict,) = self.detect()
        self.assertTrue(
            conflict.evidence_data["match_reason"].startswith("Alternative name")
        )

    def test_no_conflict_when_different_person(self):
        self.proposal.add_user(
            structure_factories.UserFactory(full_name="John Smith"),
            ProposalRole.MEMBER,
        )
        self.assertEqual(self.detect(), [])


class SameDepartmentConflictDetectionTest(TestCase):
    """Same department: the reviewer's and a team member's current
    affiliations name the same organisation and department."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.reviewer = factories.ReviewerProfileFactory()
        self.config = factories.CallCOIConfigurationFactory(
            call=self.call, include_same_institution=False
        )
        self.university = structure_factories.CustomerFactory(name="Uni")
        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer,
            organization=self.university,
            department="Physics",
        )
        self.member = structure_factories.UserFactory()
        self.proposal.add_user(self.member, ProposalRole.MEMBER)
        self.member_profile = factories.ReviewerProfileFactory(user=self.member)

    def affiliate_member(self, **kwargs):
        kwargs.setdefault("organization", self.university)
        kwargs.setdefault("department", "physics ")
        return factories.ReviewerAffiliationFactory(
            reviewer_profile=self.member_profile, **kwargs
        )

    def detect(self):
        return detect_institutional_conflicts(self.reviewer, self.proposal, self.config)

    def test_same_department_detected(self):
        self.affiliate_member()

        (conflict,) = self.detect()

        self.assertEqual(conflict.coi_type, COITypes.INST_DEPT)
        self.assertEqual(conflict.severity, COISeverityLevels.APPARENT)
        self.assertEqual(conflict.conflicting_user, self.member)
        self.assertEqual(conflict.conflicting_organization, self.university)

    def test_not_detected_when_the_call_turns_it_off(self):
        self.affiliate_member()
        self.config.include_same_department = False
        self.config.save()
        self.assertEqual(self.detect(), [])

    def test_other_department_is_not_a_match(self):
        self.affiliate_member(department="Chemistry")
        self.assertEqual(self.detect(), [])

    def test_same_department_name_elsewhere_is_not_a_match(self):
        self.affiliate_member(organization=structure_factories.CustomerFactory())
        self.assertEqual(self.detect(), [])

    def test_a_past_affiliation_is_not_a_match(self):
        self.affiliate_member(end_date=date.today() - timedelta(days=30))
        self.assertEqual(self.detect(), [])

    def test_no_department_recorded_is_not_a_match(self):
        self.affiliate_member(department="")
        self.assertEqual(self.detect(), [])


class COITypeHandlingTest(TestCase):
    """The call's type handling sets the severity a detected conflict is
    recorded with, and the conflict still waits for a manager."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.reviewer = factories.ReviewerProfileFactory()
        self.config = factories.CallCOIConfigurationFactory(call=self.call)
        self.proposal.add_user(self.reviewer.user, ProposalRole.MEMBER)

    def test_without_handling_the_type_default_applies(self):
        (conflict,) = run_coi_detection_for_pair(
            self.reviewer, self.proposal, self.config
        )
        self.assertEqual(conflict.severity, COISeverityLevels.REAL)

    def test_disclosure_only_type_is_a_potential_conflict_still_pending(self):
        self.config.disclosure_only_types = [COITypes.ROLE_NAMED]
        self.config.save()
        (conflict,) = run_coi_detection_for_pair(
            self.reviewer, self.proposal, self.config
        )
        self.assertEqual(conflict.severity, COISeverityLevels.POTENTIAL)
        self.assertEqual(conflict.status, COIStatuses.PENDING)

    def test_management_type_is_an_apparent_conflict(self):
        self.config.management_allowed_types = [COITypes.ROLE_NAMED]
        self.config.save()
        (conflict,) = run_coi_detection_for_pair(
            self.reviewer, self.proposal, self.config
        )
        self.assertEqual(conflict.severity, COISeverityLevels.APPARENT)

    def test_recusal_type_is_a_real_conflict(self):
        self.proposal.remove_user(self.reviewer.user)
        member = structure_factories.UserFactory(full_name="Alice Johnson")
        self.proposal.add_user(member, ProposalRole.MEMBER)
        factories.ReviewerPublicationFactory(
            reviewer_profile=self.reviewer,
            publication_year=date.today().year - 2,
            coauthors=["Alice Johnson"],
        )
        self.config.recusal_required_types = [COITypes.COAUTH_OLD]
        self.config.save()

        (conflict,) = run_coi_detection_for_pair(
            self.reviewer, self.proposal, self.config
        )

        self.assertEqual(conflict.coi_type, COITypes.COAUTH_OLD)
        self.assertEqual(conflict.severity, COISeverityLevels.REAL)
        self.assertEqual(conflict.status, COIStatuses.PENDING)


class COIDetectionForPairTest(TestCase):
    """Test combined COI detection for a reviewer-proposal pair."""

    def setUp(self):
        self.organization = structure_factories.CustomerFactory()
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.project = structure_factories.ProjectFactory(customer=self.organization)
        self.proposal = factories.ProposalFactory(
            round=self.round, project=self.project
        )
        self.reviewer = factories.ReviewerProfileFactory()
        self.config = factories.CallCOIConfigurationFactory(call=self.call)

    def test_runs_all_detection_algorithms(self):
        """All enabled detection algorithms are run."""
        conflicts = run_coi_detection_for_pair(
            self.reviewer, self.proposal, self.config
        )
        # Should complete without error even if no conflicts found
        self.assertIsInstance(conflicts, list)

    def test_respects_config_flags(self):
        """Detection algorithms respect configuration flags."""
        self.config.auto_detect_coauthorship = False
        self.config.auto_detect_institutional = False
        self.config.auto_detect_named_personnel = False
        self.config.save()

        # Create data that would normally trigger conflicts
        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer,
            organization=self.organization,
            end_date=None,
        )

        conflicts = run_coi_detection_for_pair(
            self.reviewer, self.proposal, self.config
        )
        self.assertEqual(len(conflicts), 0)


class COIDetectionForCallTest(TestCase):
    """Test batch COI detection for an entire call."""

    def setUp(self):
        self.organization = structure_factories.CustomerFactory()
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.project = structure_factories.ProjectFactory(customer=self.organization)
        self.config = factories.CallCOIConfigurationFactory(call=self.call)

        # Create proposals
        self.proposal1 = factories.ProposalFactory(
            round=self.round, project=self.project
        )
        self.proposal2 = factories.ProposalFactory(round=self.round)

        # Create reviewers in pool
        self.reviewer1 = factories.ReviewerProfileFactory()
        self.reviewer2 = factories.ReviewerProfileFactory()

        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer1,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )
        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=self.reviewer2,
            invitation_status=ReviewerPoolInvitationStatuses.ACCEPTED,
        )

    def test_processes_all_reviewer_proposal_pairs(self):
        """Detection runs for all reviewer-proposal pairs."""
        job = factories.COIDetectionJobFactory(call=self.call)

        result = run_coi_detection_for_call(self.call, job)

        # 2 reviewers x 2 proposals = 4 pairs
        self.assertEqual(result["total_pairs"], 4)
        self.assertEqual(result["processed"], 4)

    def test_job_state_updated_on_completion(self):
        """Job state is updated to COMPLETED when finished."""
        job = factories.COIDetectionJobFactory(call=self.call)

        run_coi_detection_for_call(self.call, job)

        job.refresh_from_db()
        self.assertEqual(job.state, COIDetectionJobStates.COMPLETED)
        self.assertIsNotNone(job.completed_at)

    def test_job_records_conflicts_found(self):
        """Job records the number of conflicts found."""
        # Create an institutional conflict
        factories.ReviewerAffiliationFactory(
            reviewer_profile=self.reviewer1,
            organization=self.organization,
            end_date=None,
        )

        job = factories.COIDetectionJobFactory(call=self.call)
        result = run_coi_detection_for_call(self.call, job)

        job.refresh_from_db()
        # Should find at least 1 conflict (reviewer1 at same org as proposal1)
        self.assertGreaterEqual(job.conflicts_found, 1)
        self.assertEqual(len(result["created_conflicts"]), job.conflicts_found)

    def test_excludes_pending_reviewers(self):
        """Only accepted reviewers in pool are included."""
        pending_reviewer = factories.ReviewerProfileFactory()
        factories.CallReviewerPoolFactory(
            call=self.call,
            reviewer=pending_reviewer,
            invitation_status=ReviewerPoolInvitationStatuses.PENDING,
        )

        job = factories.COIDetectionJobFactory(call=self.call)
        result = run_coi_detection_for_call(self.call, job)

        # Still only 2 accepted reviewers x 2 proposals = 4 pairs
        self.assertEqual(result["total_pairs"], 4)


class COIDetectionAPITest(test.APITestCase):
    """Test COI detection API endpoints."""

    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)

        # Add user as call manager
        self.call.add_user(self.staff, CallRole.MANAGER)

    @patch("waldur_mastermind.proposal.tasks.run_coi_detection.delay")
    def test_trigger_detection_creates_job(self, mock_task):
        """POST to detect-conflicts creates a job and triggers background task."""
        self.client.force_authenticate(self.staff)

        # Count jobs before
        job_count_before = models.COIDetectionJob.objects.filter(call=self.call).count()

        url = factories.CallFactory.get_protected_url(
            self.call, action="detect-conflicts"
        )
        response = self.client.post(url, {})

        # Check response status (may be 201 or 202)
        self.assertIn(
            response.status_code, [status.HTTP_201_CREATED, status.HTTP_202_ACCEPTED]
        )

        # Check job was created
        job_count_after = models.COIDetectionJob.objects.filter(call=self.call).count()
        self.assertEqual(job_count_after, job_count_before + 1)

        # Verify Celery task was called
        mock_task.assert_called_once()

    def test_anonymous_cannot_trigger_detection(self):
        """Anonymous users cannot trigger COI detection."""
        url = factories.CallFactory.get_protected_url(
            self.call, action="detect-conflicts"
        )
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_non_manager_cannot_trigger_detection(self):
        """Non-managers cannot trigger COI detection."""
        regular_user = structure_factories.UserFactory()
        self.client.force_authenticate(regular_user)

        url = factories.CallFactory.get_protected_url(
            self.call, action="detect-conflicts"
        )
        response = self.client.post(url, {})

        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)


class COIDetectionTaskTest(TestCase):
    """Test the Celery task for COI detection."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        factories.CallCOIConfigurationFactory(call=self.call)

    @patch("waldur_mastermind.proposal.coi_detection.run_coi_detection_for_call")
    def test_task_runs_detection(self, mock_detection):
        """Task calls the detection function with correct parameters."""
        from waldur_mastermind.proposal.tasks import run_coi_detection

        mock_detection.return_value = {
            "total_pairs": 0,
            "processed": 0,
            "conflicts_found": 0,
            "created_conflicts": [],
        }

        job = factories.COIDetectionJobFactory(call=self.call)

        # Call the task function directly (without Celery)
        run_coi_detection(str(job.uuid))

        mock_detection.assert_called_once()
        call_args = mock_detection.call_args
        self.assertEqual(call_args[0][0], self.call)
        self.assertEqual(call_args[0][1].uuid, job.uuid)

    def test_task_handles_missing_job(self):
        """Task handles case where job doesn't exist."""
        from waldur_mastermind.proposal.tasks import run_coi_detection

        # Should not raise exception
        result = run_coi_detection("00000000-0000-0000-0000-000000000000")
        self.assertIsNone(result)

    def test_task_skips_non_pending_jobs(self):
        """Task skips jobs that are not in PENDING state."""
        from waldur_mastermind.proposal.tasks import run_coi_detection

        job = factories.COIDetectionJobFactory(
            call=self.call,
            state=COIDetectionJobStates.COMPLETED,
        )

        with patch(
            "waldur_mastermind.proposal.coi_detection.run_coi_detection_for_call"
        ) as mock:
            run_coi_detection(str(job.uuid))
            mock.assert_not_called()


class COIResolutionPermissionTest(test.APITestCase):
    """Resolving a conflict is call-management work, on every route.

    The ungated update route let the reviewer a conflict was about dismiss it
    themselves, which the dismiss action refuses them.
    """

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)

        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)

        self.reviewer_user = structure_factories.UserFactory()
        self.profile = factories.ReviewerProfileFactory(user=self.reviewer_user)
        self.coi = factories.ConflictOfInterestFactory(
            reviewer=self.profile, proposal=self.proposal, call=self.call
        )
        self.url = factories.ConflictOfInterestFactory.get_url(self.coi)

    def test_conflict_names_its_proposal_by_slug(self):
        self.client.force_authenticate(self.manager)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["proposal_slug"], self.proposal.slug)

    def test_conflicted_reviewer_cannot_patch_own_conflict(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.patch(self.url, {"review_notes": "nothing to see"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_conflicted_reviewer_cannot_dismiss_own_conflict(self):
        self.client.force_authenticate(self.reviewer_user)
        response = self.client.post(self.url + "dismiss/", {"status": "dismissed"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.coi.refresh_from_db()
        self.assertEqual(self.coi.status, COIStatuses.PENDING)

    def test_status_cannot_be_set_through_the_update_route(self):
        self.client.force_authenticate(self.manager)
        response = self.client.patch(self.url, {"status": COIStatuses.DISMISSED})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.coi.refresh_from_db()
        self.assertEqual(self.coi.status, COIStatuses.PENDING)

    def test_a_manager_on_the_applicant_team_neither_lists_nor_resolves_it(self):
        self.proposal.add_user(self.manager, ProposalRole.MEMBER)
        other = factories.ConflictOfInterestFactory(
            reviewer=self.profile,
            proposal=factories.ProposalFactory(round=self.round),
            call=self.call,
        )
        self.client.force_authenticate(self.manager)

        response = self.client.get(factories.ConflictOfInterestFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual([row["uuid"] for row in response.data], [other.uuid.hex])

        response = self.client.post(
            self.url + "dismiss/", {"status": COIStatuses.DISMISSED}
        )
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)
        self.coi.refresh_from_db()
        self.assertEqual(self.coi.status, COIStatuses.PENDING)

    def test_manager_resolves_through_the_dismiss_action(self):
        self.client.force_authenticate(self.manager)
        response = self.client.post(
            self.url + "dismiss/", {"status": COIStatuses.DISMISSED}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.coi.refresh_from_db()
        self.assertEqual(self.coi.status, COIStatuses.DISMISSED)
        self.assertEqual(self.coi.reviewed_by, self.manager)
        self.assertIsNotNone(self.coi.reviewed_at)


class COIStatusTransitionTest(test.APITestCase):
    """A conflict is resolved once: dismissed, waived or recused from
    pending, and never moved between those outcomes."""

    def setUp(self):
        self.call = factories.CallFactory()
        self.round = factories.RoundFactory(call=self.call)
        self.proposal = factories.ProposalFactory(round=self.round)
        self.manager = structure_factories.UserFactory()
        self.call.add_user(self.manager, CallRole.MANAGER)
        CallRole.MANAGER.add_permission(PermissionEnum.MANAGE_PROPOSAL_REVIEW)
        self.coi = factories.ConflictOfInterestFactory(
            proposal=self.proposal,
            call=self.call,
            coi_type=COITypes.COAUTH_RECENT,
        )
        self.url = factories.ConflictOfInterestFactory.get_url(self.coi)
        self.client.force_authenticate(self.manager)

    OUTCOME = {
        "dismiss": COIStatuses.DISMISSED,
        "waive": COIStatuses.WAIVED,
        "recuse": COIStatuses.RECUSED,
    }

    def post(self, action, **payload):
        payload.setdefault("status", self.OUTCOME[action])
        return self.client.post(self.url + action + "/", payload, format="json")

    def test_each_resolution_is_allowed_from_pending(self):
        for action, outcome, payload in (
            ("dismiss", COIStatuses.DISMISSED, {}),
            ("waive", COIStatuses.WAIVED, {"management_plan": "Second reader"}),
            ("recuse", COIStatuses.RECUSED, {}),
        ):
            with self.subTest(action=action):
                self.coi.status = COIStatuses.PENDING
                self.coi.save()
                response = self.post(action, **payload)
                self.assertEqual(response.status_code, status.HTTP_200_OK)
                self.coi.refresh_from_db()
                self.assertEqual(self.coi.status, outcome)

    def test_a_resolved_conflict_cannot_be_resolved_again(self):
        for current in (
            COIStatuses.DISMISSED,
            COIStatuses.WAIVED,
            COIStatuses.RECUSED,
        ):
            for action, payload in (
                ("dismiss", {}),
                ("waive", {"management_plan": "Second reader"}),
                ("recuse", {}),
            ):
                with self.subTest(current=current, action=action):
                    self.coi.status = current
                    self.coi.save()
                    response = self.post(action, **payload)
                    self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
                    self.assertIn("pending", str(response.data).lower())
                    self.coi.refresh_from_db()
                    self.assertEqual(self.coi.status, current)

    def test_a_type_requiring_recusal_cannot_be_waived(self):
        factories.CallCOIConfigurationFactory(
            call=self.call, recusal_required_types=[COITypes.COAUTH_RECENT]
        )

        response = self.post("waive", management_plan="Second reader")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.coi.refresh_from_db()
        self.assertEqual(self.coi.status, COIStatuses.PENDING)
        # A false positive is still dismissed, and the reviewer still recused.
        self.assertEqual(self.post("recuse").status_code, status.HTTP_200_OK)

    def test_a_type_requiring_recusal_can_be_dismissed_as_no_conflict(self):
        factories.CallCOIConfigurationFactory(
            call=self.call, recusal_required_types=[COITypes.COAUTH_RECENT]
        )
        self.assertEqual(self.post("dismiss").status_code, status.HTTP_200_OK)
