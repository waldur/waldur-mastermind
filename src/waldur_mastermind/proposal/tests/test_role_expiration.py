import datetime

from django.test import TestCase
from django.utils import timezone

from waldur_core.permissions.fixtures import ProjectRole, ProposalRole
from waldur_core.permissions.tasks import check_expired_permissions
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.proposal.tests import fixtures


class ProposalRoleExpirationTest(TestCase):
    """A submitted proposal's team must survive the automatic expiration sweep;
    draft proposals and other scopes keep normal expiry."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.expired = timezone.now() - datetime.timedelta(days=1)

    def _grant_expired(self, scope, role):
        user = structure_factories.UserFactory()
        return scope.add_user(user, role, expiration_time=self.expired)

    def test_submitted_proposal_role_is_not_auto_revoked(self):
        permission = self._grant_expired(
            self.fixture.proposal_submitted, ProposalRole.MANAGER
        )

        check_expired_permissions()

        permission.refresh_from_db()
        self.assertTrue(permission.is_active)

    def test_draft_proposal_role_is_auto_revoked(self):
        permission = self._grant_expired(self.fixture.proposal, ProposalRole.MANAGER)

        check_expired_permissions()

        permission.refresh_from_db()
        self.assertFalse(permission.is_active)

    def test_project_role_is_auto_revoked(self):
        # Control: the generic sweep still expires non-proposal scopes.
        project = structure_fixtures.ProjectFixture().project
        permission = self._grant_expired(project, ProjectRole.MEMBER)

        check_expired_permissions()

        permission.refresh_from_db()
        self.assertFalse(permission.is_active)
