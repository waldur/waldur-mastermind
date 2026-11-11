"""An invitation does not create access for an account by an email its user set.

With INVITATION_CREATE_MISSING_USER on, the invited address gets the account
that has it, and a FreeIPA profile's password is mailed there. Holding a user's
API token is enough to set their email, so such an address is not theirs to
be invited by.
"""

from rest_framework import test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.users import utils
from waldur_core.users.tests import factories

ADDRESS = "invited@example.com"


class GetOrCreateUserTest(test.APITestCase):
    def setUp(self):
        self.holder = structure_factories.UserFactory(email=ADDRESS)
        self.invitation = factories.ProjectInvitationFactory(email=ADDRESS)

    def test_the_account_with_the_address_is_used(self):
        user, created = utils.get_or_create_user(self.invitation)

        self.assertFalse(created)
        self.assertEqual(user, self.holder)

    def test_not_an_account_whose_user_set_the_address(self):
        self.holder.mark_email_set_by_user()
        self.holder.save()

        user, created = utils.get_or_create_user(self.invitation)

        self.assertTrue(created)
        self.assertNotEqual(user, self.holder)
