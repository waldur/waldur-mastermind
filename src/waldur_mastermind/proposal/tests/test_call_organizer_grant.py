from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.utils import get_create_permission
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models as proposal_models
from waldur_mastermind.proposal.tests import factories, fixtures


class CallOrganizerGrantPermissionTest(test.APITestCase):
    """An organization owner (and anyone holding CALL.CREATE_PERMISSION on the
    customer) can grant the Call organizer role on the managing organization.

    Regression for the dead ``CREATE_PERMISSIONS`` alias key: it was keyed by
    the scope-type alias ``call_organizer`` instead of the model name
    ``callmanagingorganisation``, so ``get_create_permission`` returned None and
    the grant 403'd for every non-staff user.
    """

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.cmo = self.fixture.manager
        self.customer = self.fixture.customer
        self.target = structure_factories.UserFactory()
        self.url = factories.CallManagingOrganisationFactory.get_url(
            self.cmo, action="add_user"
        )

    def _grant(self, actor):
        self.client.force_authenticate(actor)
        return self.client.post(
            self.url,
            {"user": self.target.uuid.hex, "role": "CUSTOMER.CALL_ORGANIZER"},
        )

    def test_create_permission_resolves_for_managing_organisation(self):
        self.assertEqual(
            get_create_permission(proposal_models.CallManagingOrganisation),
            PermissionEnum.CREATE_CALL_PERMISSION,
        )

    def test_owner_can_grant_call_organizer(self):
        # permissions.yaml grants CUSTOMER.OWNER the call role-grant permission;
        # replicate it here since the test DB does not import permissions.yaml.
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_CALL_PERMISSION)
        owner = structure_factories.UserFactory()
        self.customer.add_user(owner, CustomerRole.OWNER)
        response = self._grant(owner)
        self.assertIn(
            response.status_code,
            [status.HTTP_200_OK, status.HTTP_201_CREATED],
            getattr(response, "data", None),
        )

    def test_unrelated_user_cannot_grant(self):
        response = self._grant(structure_factories.UserFactory())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
