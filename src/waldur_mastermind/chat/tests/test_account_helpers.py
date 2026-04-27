from django.db.models import Q
from django.test import TestCase

from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.helpers import (
    name_search_filter,
    user_accessible_customers,
    user_accessible_projects,
    user_role_on_customer,
    user_role_on_project,
    validate_uuid,
)


class ValidateUuidTest(TestCase):
    def test_valid_uuid_returns_true(self):
        self.assertTrue(validate_uuid("a3000000000000000000000000000001"))

    def test_invalid_uuid_returns_false(self):
        self.assertFalse(validate_uuid("not-a-uuid"))
        self.assertFalse(validate_uuid(""))


class NameSearchFilterTest(TestCase):
    def test_name_search_matches_icontains(self):
        named = structure_factories.CustomerFactory(name="LUMI UT")
        structure_factories.CustomerFactory(name="other")
        q = name_search_filter("lumi")
        hits = Customer.objects.filter(q).distinct()
        self.assertEqual(list(hits), [named])

    def test_empty_search_returns_empty_q(self):
        q = name_search_filter("")
        self.assertEqual(q, Q())

    def test_extra_fields_add_icontains_branches(self):
        # Extra icontains field expands the OR set. Customer.abbreviation
        # stands in for backend_id (which isn't on Customer).
        abbr = structure_factories.CustomerFactory(abbreviation="LUMI")
        structure_factories.CustomerFactory(abbreviation="OTHER")
        q = name_search_filter("lumi", extra_fields=["abbreviation"])
        hits = Customer.objects.filter(q).distinct()
        self.assertIn(abbr, hits)

    def test_uuid_string_does_not_match_uuid_column(self):
        # Key behavioral change from the old helper: a UUID-shaped string in
        # `search` is NOT routed to the uuid column. It just does icontains
        # on name/extras and (almost always) matches nothing.
        customer = structure_factories.CustomerFactory(name="LUMI UT")
        q = name_search_filter(str(customer.uuid))
        self.assertNotIn(customer, Customer.objects.filter(q))


class UserScopingTest(TestCase):
    def setUp(self):
        self.fixture = ProjectFixture()

    def test_customer_owner_sees_customer(self):
        self.assertIn(
            self.fixture.customer, user_accessible_customers(self.fixture.owner)
        )

    def test_unrelated_user_does_not_see_customer(self):
        stranger = structure_factories.UserFactory()
        self.assertNotIn(self.fixture.customer, user_accessible_customers(stranger))

    def test_project_member_sees_project(self):
        self.assertIn(
            self.fixture.project, user_accessible_projects(self.fixture.member)
        )

    def test_unrelated_user_does_not_see_project(self):
        stranger = structure_factories.UserFactory()
        self.assertNotIn(self.fixture.project, user_accessible_projects(stranger))


class RoleLookupTest(TestCase):
    def setUp(self):
        self.fixture = ProjectFixture()

    def test_customer_role_for_owner(self):
        self.assertEqual(
            user_role_on_customer(self.fixture.owner, self.fixture.customer),
            RoleEnum.CUSTOMER_OWNER,
        )

    def test_customer_role_none_when_no_direct_role(self):
        # Member has PROJECT role only, no direct CUSTOMER role.
        self.assertIsNone(
            user_role_on_customer(self.fixture.member, self.fixture.customer)
        )

    def test_project_role_for_member(self):
        self.assertEqual(
            user_role_on_project(self.fixture.member, self.fixture.project),
            RoleEnum.PROJECT_MEMBER,
        )
