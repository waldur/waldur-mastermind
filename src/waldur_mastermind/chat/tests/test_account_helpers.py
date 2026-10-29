from django.contrib.auth.models import AnonymousUser
from django.db.models import Q
from django.test import TestCase

from waldur_core.permissions.enums import RoleEnum
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests.fixtures import ProjectFixture
from waldur_mastermind.chat.tools.account.get_order import GetOrderTool
from waldur_mastermind.chat.tools.account.get_project import GetProjectTool
from waldur_mastermind.chat.tools.account.get_resource import GetResourceTool
from waldur_mastermind.chat.tools.account.helpers import (
    invalid_uuid_message,
    name_search_filter,
    string_list,
    text_arguments,
    user_accessible_customers,
    user_accessible_projects,
    user_consumer_resources,
    user_role_on_customer,
    user_role_on_project,
    validate_uuid,
)
from waldur_mastermind.chat.tools.account.list_invoices import ListInvoicesTool
from waldur_mastermind.chat.tools.account.list_orders import ListOrdersTool
from waldur_mastermind.marketplace.tests import factories as marketplace_factories


class ValidateUuidTest(TestCase):
    def test_valid_uuid_returns_true(self):
        self.assertTrue(validate_uuid("a3000000000000000000000000000001"))

    def test_invalid_uuid_returns_false(self):
        self.assertFalse(validate_uuid("not-a-uuid"))
        self.assertFalse(validate_uuid(""))


class StringListTest(TestCase):
    def test_accepts_list_of_strings_and_bare_string(self):
        self.assertEqual(string_list(["a", "b"]), ["a", "b"])
        self.assertEqual(string_list("a"), ["a"])

    def test_missing_value_is_empty(self):
        self.assertEqual(string_list(None), [])
        self.assertEqual(string_list(""), [])

    def test_rejects_other_shapes(self):
        for value in (4, {"a": 1}, ["a", 1], [{"a": 1}]):
            with self.subTest(value=value):
                self.assertIsNone(string_list(value))


class TextArgumentsTest(TestCase):
    def test_strips_strings_and_reads_missing_as_empty(self):
        values, error = text_arguments({"a": "  x ", "b": None}, "a", "b", "c")
        self.assertEqual(values, {"a": "x", "b": "", "c": ""})
        self.assertEqual(error, "")

    def test_rejects_non_strings(self):
        for value in (12345, 0, True, False, 1.5, [], ["x"], {"a": 1}):
            with self.subTest(value=value):
                _, error = text_arguments({"a": value}, "a")
                self.assertEqual(error, "a must be a string.")


class NulArgumentTest(TestCase):
    def test_nul_byte_is_rejected(self):
        _, error = text_arguments({"a": "lab\x00"}, "a")
        self.assertEqual(error, "a must not contain a NUL character.")

    def test_nul_byte_in_name_search_is_validation_error(self):
        staff = structure_factories.UserFactory(is_staff=True)
        cases = (
            (GetResourceTool, "resource_name"),
            (GetProjectTool, "project_name"),
            (ListOrdersTool, "project_name"),
            (ListInvoicesTool, "customer_name"),
        )
        for tool_class, name in cases:
            with self.subTest(tool=tool_class.__name__):
                result = tool_class().execute(staff, {name: "agt\x00"})
                self.assertEqual(result["type"], "validation_error")


class InvalidUuidMessageTest(TestCase):
    def test_hint_points_to_the_name_argument(self):
        self.assertEqual(
            invalid_uuid_message("project_uuid", "Agents Lab"),
            "Invalid UUID for project_uuid: Agents Lab. "
            "Pass the name as project_name instead.",
        )

    def test_echo_is_cut(self):
        message = invalid_uuid_message("order_uuid", "x" * 1000)
        self.assertIn("x" * 64 + ".", message)
        self.assertNotIn("x" * 65, message)

    def test_get_resource_suggests_name_lookup(self):
        staff = structure_factories.UserFactory(is_staff=True)
        result = GetResourceTool().execute(staff, {"resource_uuid": "agt-web-1"})
        self.assertEqual(result["type"], "validation_error")
        self.assertIn("get_resource(resource_name=...)", result["summary"])


class UnknownArgumentsTest(TestCase):
    def test_unknown_argument_is_validation_error(self):
        staff = structure_factories.UserFactory(is_staff=True)
        for tool_class in (
            GetResourceTool,
            GetProjectTool,
            ListOrdersTool,
            GetOrderTool,
            ListInvoicesTool,
        ):
            with self.subTest(tool=tool_class.__name__):
                result = tool_class().execute(staff, {"resource_nam": "agt-db-1"})
                self.assertEqual(result["type"], "validation_error")
                self.assertTrue(
                    result["summary"].startswith("Unknown argument: resource_nam.")
                )


class NonStringArgumentsTest(TestCase):
    # Every text argument of the #801 tools, for a validation_error instead
    # of a crash on .strip().
    TEXT_ARGUMENTS = {
        GetResourceTool: ("resource_uuid", "resource_name", "project_name"),
        GetProjectTool: ("project_uuid", "project_name"),
        ListOrdersTool: (
            "project_uuid",
            "project_name",
            "resource_uuid",
            "offering_uuid",
            "offering_name",
            "customer_uuid",
            "customer_name",
        ),
        GetOrderTool: ("order_uuid",),
        ListInvoicesTool: ("customer_uuid", "customer_name"),
    }

    def test_non_string_values_are_validation_errors(self):
        staff = structure_factories.UserFactory(is_staff=True)
        for tool_class, names in self.TEXT_ARGUMENTS.items():
            for name in names:
                for value in (12345, True, ["x"], {"a": 1}):
                    with self.subTest(tool=tool_class.__name__, name=name, value=value):
                        result = tool_class().execute(staff, {name: value})
                        self.assertEqual(result["type"], "validation_error")
                        self.assertEqual(result["summary"], f"{name} must be a string.")


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

    def test_project_member_sees_consumer_resource(self):
        resource = marketplace_factories.ResourceFactory(project=self.fixture.project)
        self.assertIn(resource, user_consumer_resources(self.fixture.member))

    def test_anonymous_sees_no_consumer_resources(self):
        marketplace_factories.ResourceFactory(project=self.fixture.project)
        self.assertFalse(user_consumer_resources(AnonymousUser()).exists())


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
