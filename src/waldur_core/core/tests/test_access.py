from types import SimpleNamespace

from ddt import data, ddt
from django.test import SimpleTestCase, TestCase
from rest_framework import exceptions
from rest_framework import filters as rf_filters
from rest_framework import permissions as rf_permissions

from waldur_core.core import access
from waldur_core.core import permissions as core_permissions
from waldur_core.core.views import ActionsViewSet
from waldur_core.permissions.enums import PermissionEnum, RoleEnum
from waldur_core.permissions.utils import permission_factory
from waldur_core.structure import filters as structure_filters
from waldur_core.structure import permissions as structure_permissions
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.marketplace.views import any_permission
from waldur_mastermind.support import filters as support_filters

PROJECT_ADMIN = access.role(
    [
        (RoleEnum.CUSTOMER_OWNER, "customer"),
        (RoleEnum.PROJECT_MANAGER, "project"),
        (RoleEnum.PROJECT_ADMIN, "project"),
    ]
)


def checked_by_hand(request, view, obj=None):
    """Only the project's managers may do this,
    and only while the project is active.

    Further detail that is not part of the summary.
    """


def unexplained_rule(request, view, obj=None):
    pass


@access.describes(access.owned("The caller's own key."))
def own_key(request, view, obj=None):
    pass


class ExampleViewSet(ActionsViewSet):
    __test__ = False
    lookup_field = "uuid"
    unsafe_methods_permissions = [structure_permissions.is_administrator]
    destroy_permissions = [
        permission_factory(PermissionEnum.DELETE_PROJECT, ["customer"])
    ]
    approve_permissions = [structure_permissions.is_staff, checked_by_hand]
    rotate_permissions = [own_key]
    audit_permissions = [unexplained_rule]
    either_permissions = [
        any_permission(
            permission_factory(PermissionEnum.UPDATE_PROJECT, ["customer"]),
            permission_factory(PermissionEnum.DELETE_PROJECT, ["customer"]),
        )
    ]
    countries_permissions = []
    access_declarations = {
        "import_resource": [access.custom("The serializer checks the project.")],
        "mixed": [access.any_of(access.staff(), access.undescribed("some.check"))],
    }
    filter_backends = (structure_filters.GenericRoleFilter,)


class OwnObjectsFilter(rf_filters.BaseFilterBackend):
    """Objects the caller created,
    and those shared with them.

    Further detail that is not part of the summary.
    """

    access_visibility = access.CUSTOM_VISIBILITY


class UnexplainedFilter(rf_filters.BaseFilterBackend):
    access_visibility = access.CUSTOM_VISIBILITY


class UndescribedPermission(rf_permissions.BasePermission):
    """Only callers from the allowed networks."""


class BarePermission(rf_permissions.BasePermission):
    pass


def resolve(view_class, action, method, detail=True, **attributes):
    view = view_class()
    view.action = action
    view.detail = detail
    for name, value in attributes.items():
        setattr(view, name, value)
    return access.access_for(view, method)


class AccessForTest(SimpleTestCase):
    def test_permission_factory_check_is_described_by_its_permission(self):
        route = resolve(ExampleViewSet, "destroy", "DELETE")
        self.assertEqual(
            route.entries,
            [access.permission(PermissionEnum.DELETE_PROJECT, ["customer"])],
        )

    def test_shared_role_check_lists_each_role_where_it_is_held(self):
        route = resolve(ExampleViewSet, "update", "PUT")
        self.assertEqual(route.entries, [PROJECT_ADMIN])

    def test_route_wired_by_hand_with_a_lookup_has_an_object(self):
        view = ExampleViewSet()
        view.action = "update"
        view.detail = None
        route = access.access_for(view, "PUT", url_kwargs=["uuid"])
        self.assertEqual(route.entries, [PROJECT_ADMIN])

    def test_object_check_on_route_without_object_declares_nothing(self):
        # ActionsPermission calls it without an object, and it lets
        # everyone through.
        route = resolve(ExampleViewSet, "create", "POST", detail=False)
        self.assertEqual(route.entries, [])
        self.assertFalse(route.declared)

    def test_undescribed_check_is_described_by_its_first_paragraph(self):
        route = resolve(ExampleViewSet, "approve", "POST")
        self.assertEqual(
            route.entries,
            [
                access.staff(),
                access.custom(
                    "Only the project's managers may do this, "
                    "and only while the project is active."
                ),
            ],
        )

    def test_check_with_no_docstring_does_not_describe_the_route(self):
        route = resolve(ExampleViewSet, "audit", "POST")
        self.assertEqual(
            route.entries,
            [access.undescribed(f"{__name__}.unexplained_rule")],
        )
        self.assertTrue(route.declared)
        self.assertFalse(route.described)

    def test_alternative_with_no_description_leaves_the_route_undescribed(self):
        route = resolve(ExampleViewSet, "mixed", "POST")
        self.assertFalse(route.described)

    def test_check_function_can_describe_itself(self):
        route = resolve(ExampleViewSet, "rotate", "POST")
        self.assertEqual(route.entries, [access.owned("The caller's own key.")])

    def test_any_permission_describes_its_alternatives(self):
        route = resolve(ExampleViewSet, "either", "POST")
        self.assertEqual(
            route.entries,
            [
                access.any_of(
                    access.permission(PermissionEnum.UPDATE_PROJECT, ["customer"]),
                    access.permission(PermissionEnum.DELETE_PROJECT, ["customer"]),
                )
            ],
        )

    def test_view_declares_what_is_checked_elsewhere(self):
        route = resolve(ExampleViewSet, "import_resource", "POST")
        self.assertIn(
            access.custom("The serializer checks the project."), route.entries
        )
        self.assertTrue(route.declared)

    def test_role_filter_narrows_list_and_retrieve(self):
        for action in ("list", "retrieve"):
            with self.subTest(action=action):
                route = resolve(ExampleViewSet, action, "GET")
                self.assertEqual(route.entries, [])
                self.assertEqual(route.visibility, "role")
                self.assertEqual(route.schema_entries(), [access.authenticated()])

    def test_role_filter_does_not_declare_other_reads(self):
        route = resolve(ExampleViewSet, "countries", "GET", detail=False)
        self.assertIsNone(route.visibility)
        self.assertFalse(route.declared)

    def test_role_filtered_list_open_to_anonymous_callers_says_so(self):
        route = resolve(ExampleViewSet, "list", "GET", permission_classes=[])
        self.assertEqual(route.schema_entries(), [access.public()])

    def test_route_that_declares_nothing_is_undeclared(self):
        route = resolve(ExampleViewSet, "create", "POST", unsafe_methods_permissions=[])
        self.assertFalse(route.declared)
        self.assertEqual(route.schema_entries(), [access.undeclared()])

    def test_is_authenticated_alone_declares_nothing(self):
        route = resolve(
            ExampleViewSet,
            "list",
            "GET",
            permission_classes=[rf_permissions.IsAuthenticated],
            filter_backends=(),
        )
        self.assertFalse(route.declared)

    def test_permission_classes_describe_themselves(self):
        cases = [
            ([core_permissions.IsStaff], "GET", [access.staff()]),
            ([core_permissions.IsSupport], "POST", [access.staff_or_support()]),
            (
                [rf_permissions.IsAuthenticated, core_permissions.IsAdminOrReadOnly],
                "GET",
                [access.authenticated()],
            ),
            ([core_permissions.IsAdminOrReadOnly], "GET", [access.public()]),
            ([core_permissions.IsAdminOrReadOnly], "POST", [access.staff()]),
            ([core_permissions.TokenCarriesUserRole], "POST", []),
            ([rf_permissions.AllowAny], "POST", [access.public()]),
            (
                [UndescribedPermission],
                "GET",
                [access.custom("Only callers from the allowed networks.")],
            ),
            (
                [BarePermission],
                "GET",
                [access.undescribed(f"{__name__}.BarePermission")],
            ),
        ]
        for classes, method, expected in cases:
            with self.subTest(classes=classes, method=method):
                route = resolve(
                    ExampleViewSet,
                    "list",
                    method,
                    permission_classes=classes,
                    filter_backends=(),
                )
                self.assertEqual(route.entries, expected)

    def test_permissions_chosen_per_request_fall_back_to_the_declared_classes(self):
        class PerRequestViewSet(ExampleViewSet):
            permission_classes = [core_permissions.IsStaff]

            def get_permissions(self):
                return [] if self.request.user.is_staff else []

        route = resolve(PerRequestViewSet, "list", "GET", request=None)
        self.assertEqual(route.entries, [access.staff()])

    def test_caller_and_helpdesk_filters_state_their_wider_visibility(self):
        route = resolve(
            ExampleViewSet,
            "list",
            "GET",
            filter_backends=(support_filters.IssueCallerOrRoleFilterBackend,),
        )
        self.assertEqual(route.visibility, "role_caller_or_helpdesk")

    def test_filter_with_a_rule_of_its_own_says_what_it_shows(self):
        route = resolve(
            ExampleViewSet,
            "list",
            "GET",
            filter_backends=(
                rf_filters.OrderingFilter,
                OwnObjectsFilter,
                structure_filters.GenericRoleFilter,
            ),
        )
        self.assertEqual(route.visibility, access.CUSTOM_VISIBILITY)
        self.assertEqual(
            route.visibility_description,
            "Objects the caller created, and those shared with them.",
        )
        self.assertTrue(route.described)

    def test_filter_with_a_rule_of_its_own_and_no_docstring_is_undescribed(self):
        route = resolve(
            ExampleViewSet, "list", "GET", filter_backends=(UnexplainedFilter,)
        )
        self.assertTrue(route.declared)
        self.assertFalse(route.described)

    def test_descriptions_are_copies(self):
        route = resolve(ExampleViewSet, "approve", "POST")
        route.entries[0]["via"] = "changed"
        self.assertEqual(structure_permissions.is_staff.describe(), access.staff())


class ObjectRulesTest(SimpleTestCase):
    """`requires_object` and `needs_object` agree with what the checks do."""

    def test_permission_factory_checks(self):
        with_sources = permission_factory(PermissionEnum.DELETE_PROJECT, ["customer"])
        without_sources = permission_factory(PermissionEnum.DELETE_PROJECT)
        self.assertTrue(access.requires_object(with_sources))
        self.assertTrue(access.needs_object(with_sources))
        self.assertFalse(access.requires_object(without_sources))
        self.assertTrue(access.needs_object(without_sources))

    def test_shared_checks(self):
        self.assertFalse(access.requires_object(structure_permissions.is_administrator))
        self.assertTrue(access.needs_object(structure_permissions.is_administrator))
        self.assertFalse(access.needs_object(structure_permissions.is_staff))

    def test_wrappers_keep_the_rule_of_what_they_wrap(self):
        check = any_permission(
            permission_factory(PermissionEnum.UPDATE_PROJECT, ["customer"])
        )
        self.assertTrue(access.requires_object(check))
        self.assertTrue(access.needs_object(check))


def passes(check, user, obj):
    try:
        check(SimpleNamespace(user=user, auth=None), None, obj)
    except exceptions.PermissionDenied:
        return False
    return True


USERS_AND_ROLES = {
    "owner": RoleEnum.CUSTOMER_OWNER,
    "service_manager": RoleEnum.CUSTOMER_MANAGER,
    "customer_support": RoleEnum.CUSTOMER_SUPPORT,
    "manager": RoleEnum.PROJECT_MANAGER,
    "admin": RoleEnum.PROJECT_ADMIN,
    "member": RoleEnum.PROJECT_MEMBER,
}


def described_roles(check):
    return {holder["role"] for holder in check.describe()["roles"]}


@ddt
class SharedRoleChecksEnforceWhatTheyDescribeTest(TestCase):
    """A shared role check passes exactly the roles its description lists."""

    @data(
        ("is_owner", "customer"),
        ("is_service_manager", "customer"),
        ("is_administrator", "project"),
    )
    def test_check_passes_the_roles_it_describes(self, case):
        name, scope = case
        check = getattr(structure_permissions, name)
        fixture = structure_fixtures.ProjectFixture()
        obj = getattr(fixture, scope)
        described = described_roles(check)

        for user, role in USERS_AND_ROLES.items():
            with self.subTest(user=user):
                self.assertEqual(
                    passes(check, getattr(fixture, user), obj), role in described
                )
        self.assertTrue(passes(check, fixture.staff, obj))
        self.assertFalse(passes(check, fixture.user, obj))

    def test_offering_manager_check_passes_the_roles_it_describes(self):
        check = structure_permissions.is_offering_manager
        fixture = marketplace_fixtures.MarketplaceFixture()
        described = described_roles(check)
        users_and_roles = {
            "offering_owner": RoleEnum.CUSTOMER_OWNER,
            "service_manager": RoleEnum.CUSTOMER_MANAGER,
            "offering_manager": RoleEnum.OFFERING_MANAGER,
            "member": RoleEnum.PROJECT_MEMBER,
        }
        for user, role in users_and_roles.items():
            with self.subTest(user=user):
                self.assertEqual(
                    passes(check, getattr(fixture, user), fixture.offering),
                    role in described,
                )
        self.assertTrue(passes(check, fixture.staff, fixture.offering))
        self.assertFalse(passes(check, fixture.user, fixture.offering))
