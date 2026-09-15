from types import SimpleNamespace
from unittest import mock

from django.test import SimpleTestCase
from django.urls import URLPattern, URLResolver, get_resolver
from rest_framework.exceptions import PermissionDenied

from waldur_core.core.permissions import ActionsPermission, requires_object
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory


def scoped_check():
    check = mock.Mock()
    check.sources = ["offering"]
    return check


def make_view(action, kwargs=None, lookup_url_kwarg=None, **attrs):
    return SimpleNamespace(
        action=action,
        lookup_field="uuid",
        lookup_url_kwarg=lookup_url_kwarg,
        kwargs=kwargs or {},
        get_object=mock.Mock(return_value=mock.sentinel.obj),
        **attrs,
    )


class ActionsPermissionTest(SimpleTestCase):
    def setUp(self):
        self.request = SimpleNamespace(method="GET")

    def test_detail_route_resolves_object_for_scoped_check(self):
        check = scoped_check()
        view = make_view(
            "retrieve",
            kwargs={"uuid": "abc"},
            detail=True,
            retrieve_permissions=[check],
        )

        self.assertTrue(ActionsPermission().has_permission(self.request, view))

        view.get_object.assert_called_once_with()
        check.assert_called_once_with(self.request, view, mock.sentinel.obj)

    def test_hand_wired_route_resolves_object_for_scoped_check(self):
        # as_view() outside a router leaves view.detail None; the lookup in
        # the URL is what tells a single-object route apart.
        check = scoped_check()
        view = make_view(
            "plan_detail",
            kwargs={"uuid": "abc"},
            detail=None,
            plan_detail_permissions=[check],
        )

        ActionsPermission().has_permission(self.request, view)

        view.get_object.assert_called_once_with()
        check.assert_called_once_with(self.request, view, mock.sentinel.obj)

    def test_custom_lookup_url_kwarg_resolves_object(self):
        check = scoped_check()
        view = make_view(
            "retrieve",
            kwargs={"offering_uuid": "abc"},
            lookup_url_kwarg="offering_uuid",
            retrieve_permissions=[check],
        )

        ActionsPermission().has_permission(self.request, view)

        view.get_object.assert_called_once_with()

    def test_collection_route_does_not_resolve_object(self):
        view = make_view(
            "list",
            detail=False,
            list_permissions=[
                permission_factory(
                    PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES, ["offering"]
                )
            ],
        )

        self.assertTrue(ActionsPermission().has_permission(self.request, view))

        view.get_object.assert_not_called()

    def test_collection_route_still_runs_unscoped_checks(self):
        def deny(request, view, obj=None):
            raise PermissionDenied()

        view = make_view("list", detail=False, list_permissions=[deny])

        with self.assertRaises(PermissionDenied):
            ActionsPermission().has_permission(self.request, view)

    def test_action_extra_permissions_run_after_method_permissions(self):
        calls = []

        def method_check(request, view, obj=None):
            calls.append("method")

        def extra_check(request, view, obj=None):
            calls.append("extra")

        view = make_view(
            "stats",
            safe_methods_permissions=[method_check],
            stats_extra_permissions=[extra_check],
        )

        ActionsPermission().has_permission(self.request, view)

        self.assertEqual(calls, ["method", "extra"])

    def test_action_extra_permissions_can_deny(self):
        def deny(request, view, obj=None):
            raise PermissionDenied()

        view = make_view(
            "stats",
            unsafe_methods_permissions=[],
            stats_extra_permissions=[deny],
        )

        with self.assertRaises(PermissionDenied):
            ActionsPermission().has_permission(SimpleNamespace(method="POST"), view)


def iter_routes(patterns, url_kwargs=frozenset()):
    for entry in patterns:
        names = url_kwargs | set(entry.pattern.regex.groupindex)
        if isinstance(entry, URLResolver):
            yield from iter_routes(entry.url_patterns, names)
        elif isinstance(entry, URLPattern):
            yield entry.callback, names


def uses_actions_permission(cls):
    return any(
        isinstance(permission_class, type)
        and issubclass(permission_class, ActionsPermission)
        for permission_class in getattr(cls, "permission_classes", ())
    )


class ActionsPermissionRoutesTest(SimpleTestCase):
    def test_collection_routes_declare_no_object_scoped_checks(self):
        # An object-scoped check on a route without a lookup never sees an
        # object, so it passes for everyone. Such a route has to scope its
        # queryset instead, e.g. through Model.Permissions and GenericRoleFilter.
        offenders = set()
        for callback, url_kwargs in iter_routes(get_resolver().url_patterns):
            cls = getattr(callback, "cls", None)
            actions = getattr(callback, "actions", None)
            if not actions or not uses_actions_permission(cls):
                continue
            if (cls.lookup_url_kwarg or cls.lookup_field) in url_kwargs:
                continue
            for method, action in actions.items():
                if action in getattr(cls, "disabled_actions", []):
                    continue
                view = cls.__new__(cls)
                view.action = action
                checks = ActionsPermission().get_permission_checks(
                    SimpleNamespace(method=method.upper()), view
                )
                if any(requires_object(check) for check in checks):
                    offenders.add(f"{cls.__module__}.{cls.__name__}.{action}")

        self.assertEqual(offenders, set())
