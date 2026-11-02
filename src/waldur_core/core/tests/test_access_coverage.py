"""Every API route says who may call it.

The test resolves each DRF route and method with ``access_for``, the function
the OpenAPI schema uses, and fails listing, grouped by app, the routes that
declare nothing and those whose checks do not describe themselves. It runs in its own CI job, allowed to fail until every app has
declared its routes; the sharded unit suite skips it (opt in with
``-m access_coverage``).
"""

import collections

import pytest
from django.test import SimpleTestCase
from django.urls import get_resolver
from drf_spectacular.settings import spectacular_settings
from rest_framework.views import APIView

from waldur_core.core import access
from waldur_core.core.tests.test_actions_permission import iter_routes

pytestmark = pytest.mark.access_coverage

SKIPPED_METHODS = {"HEAD", "OPTIONS"}


def _view(cls, initkwargs, url_kwargs, method, action=None, actions=None):
    view = cls(**initkwargs)
    view.action_map = actions or {}
    view.args = ()
    # The URL's own arguments, so that a route wired by hand with a lookup
    # counts as having an object, as it does for ActionsPermission.
    view.kwargs = {name: "x" for name in url_kwargs}
    view.format_kwarg = None
    # As schema generation does, so that views choosing permissions per
    # request resolve alike for this test and for the schema.
    view.swagger_fake_view = True
    view.request = spectacular_settings.GET_MOCK_REQUEST(method, "/", view, None)
    view.action = action
    return view


def unexplained_routes():
    """`module.View.action METHOD` for the routes that do not say who may call them.

    Returns those that declare nothing, and those whose checks have neither a
    description nor a docstring. A view mounted at several URLs is resolved at
    each of them, and counts if any mount does.
    """
    undeclared, undescribed = set(), set()
    for callback, url_kwargs in iter_routes(get_resolver().url_patterns):
        cls = getattr(callback, "cls", None)
        if not (isinstance(cls, type) and issubclass(cls, APIView)):
            continue
        initkwargs = getattr(callback, "initkwargs", None) or {}
        name = f"{cls.__module__}.{cls.__name__}"
        actions = getattr(callback, "actions", None)
        if actions:
            routes = [
                (method.upper(), action)
                for method, action in actions.items()
                if action not in getattr(cls, "disabled_actions", [])
            ]
        else:
            methods = cls(**initkwargs).allowed_methods
            routes = [(method, None) for method in methods]
        for method, action in routes:
            if method in SKIPPED_METHODS:
                continue
            view = _view(cls, initkwargs, url_kwargs, method, action, actions)
            route = access.access_for(view, method)
            key = f"{name}.{action or method.lower()} {method}"
            if not route.declared:
                undeclared.add(key)
            elif not route.described:
                undescribed.add(key)
    return sorted(undeclared), sorted(undescribed - undeclared)


def _app(route):
    parts = route.split(".")
    if parts[0] == "waldur_mastermind":
        return parts[1]
    if parts[0] == "waldur_core":
        return f"core/{parts[1]}"
    return parts[0].removeprefix("waldur_")


def _by_app(title, routes):
    by_app = collections.defaultdict(list)
    for route in routes:
        by_app[_app(route)].append(route)
    lines = [title, ""]
    for app, app_routes in sorted(
        by_app.items(), key=lambda item: (-len(item[1]), item[0])
    ):
        lines.append(f"{app} ({len(app_routes)})")
        lines.extend(f"  {route}" for route in app_routes)
    return lines + [""]


def report(undeclared, undescribed):
    lines = [
        f"{len(undeclared) + len(undescribed)} API routes do not say who may call "
        "them. Declare or describe each as docs/guides/waldur-permissions.md "
        "describes.",
        "",
    ]
    if undeclared:
        lines += _by_app(f"{len(undeclared)} declare nothing:", undeclared)
    if undescribed:
        lines += _by_app(
            f"{len(undescribed)} rely on checks with no description or docstring:",
            undescribed,
        )
    return "\n".join(lines)


class AccessCoverageTest(SimpleTestCase):
    def test_every_route_declares_who_may_call_it(self):
        undeclared, undescribed = unexplained_routes()
        if undeclared or undescribed:
            self.fail(report(undeclared, undescribed))
