from rest_framework import status, test
from rest_framework.exceptions import ValidationError

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, OfferingRole
from waldur_core.permissions.utils import has_user
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_core.users import models
from waldur_core.users.tests import factories
from waldur_core.users.utils import get_scope_link
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class OfferingScopeLinkTest(test.APITestCase):
    """The invitation email links back to the scope; offerings were missing."""

    def test_offering_scope_type_resolves_to_the_public_offering_route(self):
        link = get_scope_link("offering", "abc123")
        self.assertIn("marketplace-public-offering/abc123/", link)

    def test_lookup_is_case_insensitive(self):
        # Offering declares verbose_name = _("Offering"), and it is the
        # verbose_name that reaches this function.
        self.assertEqual(
            get_scope_link("Offering", "abc123"), get_scope_link("offering", "abc123")
        )

    def test_unmapped_scope_type_still_falls_back(self):
        self.assertIn("unknown/abc123/", get_scope_link("widget", "abc123"))


class OfferingInvitationTest(test.APITestCase):
    # Deliberately a TestCase, not a TransactionTestCase: the latter truncates
    # every table on teardown, taking the system roles created by the data
    # migrations with it, which then breaks order-sensitive assertions in
    # permissions/tests/test_roles.py later in the same session. Nothing here
    # needs real transaction semantics.
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.customer = self.fixture.customer
        self.offering = marketplace_factories.OfferingFactory(
            shared=True, customer=self.customer
        )
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_OFFERING_PERMISSION)
        self.owner = self.fixture.owner

    def create_invitation(self, offering=None):
        self.client.force_authenticate(user=self.owner)
        return self.client.post(
            factories.InvitationBaseFactory.get_list_url(),
            {
                "email": "invitee@example.com",
                "scope": marketplace_factories.OfferingFactory.get_url(
                    offering or self.offering
                ),
                "role": OfferingRole.MANAGER.uuid.hex,
            },
        )

    def test_owner_can_invite_a_user_into_a_shared_offering(self):
        response = self.create_invitation()
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])
        self.assertEqual(invitation.scope, self.offering)
        self.assertEqual(invitation.customer, self.customer)

    def test_accepting_the_invitation_grants_the_offering_role(self):
        response = self.create_invitation()
        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])

        invitee = structure_factories.UserFactory()
        invitation.accept(invitee)

        self.assertTrue(has_user(self.offering, invitee, OfferingRole.MANAGER))

    def test_a_private_offering_can_not_be_invited_into(self):
        private = marketplace_factories.OfferingFactory(
            shared=False, customer=self.customer
        )
        response = self.create_invitation(private)
        self.assertEqual(
            response.status_code, status.HTTP_400_BAD_REQUEST, response.data
        )

    def test_accepting_is_refused_once_the_offering_stops_being_shared(self):
        """The guard also has to hold at accept time.

        Creation validated a shared offering; unsharing it afterwards must not
        leave a live invitation that grants a role the add_user endpoint would
        now refuse.
        """
        response = self.create_invitation()
        invitation = models.Invitation.objects.get(uuid=response.data["uuid"])

        self.offering.shared = False
        self.offering.save(update_fields=["shared"])

        invitee = structure_factories.UserFactory()
        with self.assertRaises(ValidationError):
            invitation.accept(invitee)
        self.assertFalse(has_user(self.offering, invitee, OfferingRole.MANAGER))
