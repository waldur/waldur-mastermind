from django.test import TestCase

from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.utils import add_user, get_permissions
from waldur_core.structure.tests import factories as structure_factories
from waldur_openportal import utils

EMAIL = "someone@example.com"


class GetOrCreateUserByEmailTest(TestCase):
    """
    Membership sync resolves every incoming address through this helper, so it
    has to cope with an address that belongs to a deactivated account.
    """

    def test_existing_active_user_is_returned(self):
        user = structure_factories.UserFactory(email=EMAIL, username=EMAIL)

        self.assertEqual(utils.get_or_create_user_by_email(EMAIL).pk, user.pk)

    def test_existing_inactive_user_is_returned(self):
        """
        The default User manager hides inactive accounts.  Looking a user up
        through it misses the account and then cannot create a replacement,
        because username is unique and that account already holds it.
        """
        user = structure_factories.UserFactory(email=EMAIL, username=EMAIL)
        user.is_active = False
        user.save(update_fields=["is_active"])

        self.assertEqual(utils.get_or_create_user_by_email(EMAIL).pk, user.pk)

    def test_unknown_address_creates_a_user(self):
        user = utils.get_or_create_user_by_email("newcomer@example.com")

        self.assertEqual(user.email, "newcomer@example.com")
        self.assertFalse(user.has_usable_password())

    def test_address_is_normalised(self):
        user = structure_factories.UserFactory(email=EMAIL, username=EMAIL)

        self.assertEqual(
            utils.get_or_create_user_by_email("SomeOne@Example.COM").pk, user.pk
        )


class RemoveProjectMemberTest(TestCase):
    def setUp(self):
        self.project = structure_factories.ProjectFactory()
        self.user = structure_factories.UserFactory(email=EMAIL, username=EMAIL)
        add_user(self.project, self.user, ProjectRole.ADMIN)

    def active_roles(self):
        return [
            permission
            for permission in get_permissions(self.project, self.user)
            if permission.is_active
        ]

    def test_member_is_removed(self):
        self.assertEqual(len(self.active_roles()), 1)

        utils.remove_project_member(self.project, EMAIL)

        self.assertEqual(self.active_roles(), [])

    def test_address_case_does_not_matter(self):
        utils.remove_project_member(self.project, "SomeOne@Example.COM")

        self.assertEqual(self.active_roles(), [])

    def test_unknown_address_is_a_no_op(self):
        utils.remove_project_member(self.project, "nobody@example.com")

        self.assertEqual(len(self.active_roles()), 1)
