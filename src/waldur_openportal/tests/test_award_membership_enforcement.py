"""Local team changes on a project holding an award follow the award's rules.

The award sync applies membership_control and allowed_domains itself; these
tests cover the local paths (add_user, update_user, delete_user, invitations),
enforced while the project.enforce_allowed_domains feature is on.
"""

from rest_framework import status, test

from waldur_core.core.models import Feature
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.utils import has_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.users import models as user_models
from waldur_core.users.enums import InvitationState
from waldur_core.users.tests import factories as user_factories
from waldur_openportal import models, utils

FEATURE = "project.enforce_allowed_domains"
ALLOWED_EMAIL = "member@physics.example.ac.uk"
OUTSIDE_EMAIL = "outsider@gmail.com"


class AwardMembershipEnforcementTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff = self.fixture.staff
        self.member = structure_factories.UserFactory(email=ALLOWED_EMAIL)
        self.outsider = structure_factories.UserFactory(email=OUTSIDE_EMAIL)
        self.feature = Feature.objects.create(key=FEATURE, value=True)
        self.client.force_authenticate(self.staff)

    def award(self, membership_control="locked", allowed_domains=None):
        details = {"membership_control": membership_control}
        if allowed_domains is not None:
            details["allowed_domains"] = allowed_domains
        return models.ManagedProject.objects.create(
            destination="awards.example.org",
            identifier="award-1.awards",
            project=self.project,
            details=details,
        )

    def project_action(self, action, user, **extra):
        url = structure_factories.ProjectFactory.get_url(self.project, action=action)
        return self.client.post(
            url,
            {"user": user.uuid.hex, "role": ProjectRole.MEMBER.name, **extra},
            format="json",
        )

    def invite(self, email):
        return self.client.post(
            user_factories.InvitationBaseFactory.get_list_url(),
            {
                "email": email,
                "scope": structure_factories.ProjectFactory.get_url(self.project),
                "role": ProjectRole.MEMBER.uuid.hex,
            },
            format="json",
        )

    def accept(self, invitation, user):
        self.client.force_authenticate(user)
        return self.client.post(
            user_factories.ProjectInvitationFactory.get_url(invitation, action="accept")
        )

    def pending_invitation(self, email, created_by=None):
        return user_factories.ProjectInvitationFactory(
            scope=self.project,
            email=email,
            role=ProjectRole.MEMBER,
            created_by=created_by or self.fixture.owner,
            state=InvitationState.PENDING,
        )

    # membership_control: locked

    def test_adding_a_member_to_a_locked_award_is_refused(self):
        self.award()
        response = self.project_action("add_user", self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(has_user(self.project, self.member))

    def test_changing_a_role_on_a_locked_award_is_refused(self):
        self.project.add_user(self.member, ProjectRole.MEMBER)
        self.award()
        response = self.project_action(
            "update_user", self.member, expiration_time="2099-01-01T00:00:00Z"
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_removing_a_member_from_a_locked_award_is_refused(self):
        self.project.add_user(self.member, ProjectRole.MEMBER)
        self.award()
        response = self.project_action("delete_user", self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(has_user(self.project, self.member, ProjectRole.MEMBER))

    def test_inviting_to_a_locked_award_is_refused(self):
        self.award()
        response = self.invite(ALLOWED_EMAIL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(user_models.Invitation.objects.exists())

    def test_accepting_an_invitation_to_a_locked_award_is_refused(self):
        invitation = self.pending_invitation(ALLOWED_EMAIL)
        self.award()
        response = self.accept(invitation, self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(has_user(self.project, self.member))

    def test_roles_only_award_allows_role_changes_but_not_membership(self):
        self.project.add_user(self.member, ProjectRole.MEMBER)
        self.award(membership_control="roles_only")
        response = self.project_action(
            "update_user", self.member, expiration_time="2099-01-01T00:00:00Z"
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.project_action("add_user", self.outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_openportal_invitations_are_accepted_on_a_locked_award(self):
        """The award sync invites members as the OpenPortal robot."""
        invitation = self.pending_invitation(
            ALLOWED_EMAIL, created_by=utils.get_openportal_robot()
        )
        self.award(allowed_domains=["*.ac.uk"])
        response = self.accept(invitation, self.member)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(has_user(self.project, self.member, ProjectRole.MEMBER))

    def test_an_outsider_cannot_accept_an_openportal_invitation(self):
        """The robot's invitation skips the membership lock, not the domains:
        whoever holds the link still has to be allowed to join."""
        invitation = self.pending_invitation(
            ALLOWED_EMAIL, created_by=utils.get_openportal_robot()
        )
        self.award(allowed_domains=["*.ac.uk"])
        response = self.accept(invitation, self.outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(has_user(self.project, self.outsider))

    def test_a_user_named_like_the_robot_is_not_the_robot(self):
        impostor = structure_factories.UserFactory(
            username=utils.OPENPORTAL_ROBOT_USERNAME, is_staff=False
        )
        invitation = self.pending_invitation(ALLOWED_EMAIL, created_by=impostor)
        self.award()
        response = self.accept(invitation, self.member)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(has_user(self.project, self.member))

    # allowed_domains

    def test_a_user_without_an_email_cannot_join_a_domain_limited_award(self):
        no_email = structure_factories.UserFactory(email="")
        self.award(membership_control="open", allowed_domains=["*.ac.uk"])
        response = self.project_action("add_user", no_email)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(has_user(self.project, no_email))

    def test_adding_a_user_outside_the_allowed_domains_is_refused(self):
        self.award(membership_control="open", allowed_domains=["*.ac.uk"])
        response = self.project_action("add_user", self.outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(has_user(self.project, self.outsider))

        response = self.project_action("add_user", self.member)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_inviting_an_email_outside_the_allowed_domains_is_refused(self):
        self.award(membership_control="open", allowed_domains=["*.ac.uk"])
        response = self.invite(OUTSIDE_EMAIL)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        response = self.invite(ALLOWED_EMAIL)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_accepting_with_an_account_outside_the_allowed_domains_is_refused(self):
        invitation = self.pending_invitation(OUTSIDE_EMAIL)
        self.award(membership_control="open", allowed_domains=["*.ac.uk"])
        response = self.accept(invitation, self.outsider)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertFalse(has_user(self.project, self.outsider))

    # where it does not apply

    def test_nothing_is_enforced_while_the_feature_is_off(self):
        self.feature.value = False
        self.feature.save()
        self.award(allowed_domains=["*.ac.uk"])
        response = self.project_action("add_user", self.outsider)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_a_project_without_an_award_is_not_affected(self):
        response = self.project_action("add_user", self.outsider)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        response = self.project_action("delete_user", self.outsider)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
