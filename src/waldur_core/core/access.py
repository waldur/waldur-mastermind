"""Who may call an endpoint, as its permission checks describe themselves.

``access_for(view, method)`` resolves a route the way a request to it is
authorized -- the view's permission classes, the checks ``ActionsPermission``
picks for the action, and what the view declares about checks made
elsewhere -- and returns what each of them says. The OpenAPI schema publishes
the result as ``x-permissions``, and a coverage test lists the routes that
declare nothing at all.

Each entry is a dict with a ``kind``:

- ``permission``: ``permission`` held on the object or on one of ``scopes``,
  as ``permission_factory`` checks it;
- ``role``: one of ``roles``, each held on the object's customer, project or
  offering (its ``on``); ``via`` names the request field the object comes from
  when it is not the object acted on;
- ``staff``, ``staff_or_support``: a request acting as staff, or as staff or
  global support;
- ``public``, ``authenticated``, ``owned``: declared on purpose;
- ``any_of``: at least one entry of ``of`` passes;
- ``custom``: a check that does not describe itself, by its docstring;
- ``undescribed``: a check with neither a description nor a docstring, by
  name; it does not count as saying who may call the route;
- ``undeclared``: nothing describes the route.

Every entry of a route must pass. A request acting as staff passes
``permission`` and ``role`` entries.

A list or retrieve narrowed by a filter backend also has a visibility, the
backend's ``access_visibility``: ``role`` (``GenericRoleFilter``),
``role_caller_or_helpdesk``, or ``custom``, which the backend's docstring
describes. A view that narrows its queryset in ``get_queryset`` says what it
shows with ``shows``, and a backend that narrows a custom visibility further
adds its ``access_visibility_note`` where it runs. Other read actions that
look their object up as
retrieve does are named in the view's ``access_filtered_actions`` and share
its visibility.
"""

import json
from dataclasses import dataclass, field
from types import SimpleNamespace

from rest_framework.permissions import SAFE_METHODS, AllowAny, IsAuthenticated

# Actions whose response the view's filter backends narrow to what the caller
# may see: list filters the queryset, retrieve looks the object up in it.
FILTERED_ACTIONS = ("list", "retrieve")

# The ``access_visibility`` of a filter backend that narrows by a rule of its
# own; the first paragraph of its docstring says what it shows.
CUSTOM_VISIBILITY = "custom"


def permission(permission, scopes=None) -> dict:
    return {
        "kind": "permission",
        "permission": getattr(permission, "value", permission),
        "scopes": scopes,
    }


def role(holders, via=None) -> dict:
    """``holders`` are (role, scope) pairs: the role and where it is held."""
    entry = {
        "kind": "role",
        "roles": [
            {"role": getattr(name, "value", name), "on": scope}
            for name, scope in holders
        ],
    }
    if via:
        entry["via"] = via
    return entry


def staff() -> dict:
    return {"kind": "staff"}


def staff_or_support() -> dict:
    return {"kind": "staff_or_support"}


def public() -> dict:
    return {"kind": "public"}


def authenticated() -> dict:
    return {"kind": "authenticated"}


def owned(description) -> dict:
    return {"kind": "owned", "description": description}


def custom(description) -> dict:
    return {"kind": "custom", "description": description}


def any_of(*entries) -> dict:
    return {"kind": "any_of", "of": list(entries)}


def undescribed(name) -> dict:
    return {"kind": "undescribed", "check": name}


def undeclared() -> dict:
    return {"kind": "undeclared"}


class Check:
    """A permission check that enforces one rule and describes it.

    It is called like the functions in ``<action>_permissions`` lists,
    ``check(request, view, obj=None)``, and raises ``PermissionDenied``.
    ``needs_object`` says that without an object it lets everyone through, as
    ``ActionsPermission`` calls it on routes that have none.
    """

    name = "check"
    needs_object = False

    def __call__(self, request, view, obj=None, **kwargs):
        raise NotImplementedError

    def describe(self) -> dict:
        raise NotImplementedError

    @property
    def __name__(self):
        return self.name

    def __repr__(self):
        return f"<{type(self).__name__}: {self.name}>"


def describes(*entries, needs_object=False):
    """Describe a permission check function that is not a ``Check``."""

    def decorate(check):
        check.access_entries = list(entries)
        if needs_object:
            check.needs_object = True
        return check

    return decorate


def shows(description):
    """Say what a view's ``get_queryset`` narrows its queryset to.

    List and retrieve then show the caller ``description`` (a ``custom``
    visibility) in place of what the view's filter backends say. It describes
    ``get_queryset`` alone: a backend that narrows further says how in its
    ``access_visibility_note``, which is added wherever the backend runs. A
    subclass that overrides ``get_queryset`` describes its own.
    """

    def decorate(get_queryset):
        get_queryset.access_shows = description
        return get_queryset

    return decorate


def _summary(described) -> str | None:
    """The first paragraph of a docstring, if there is one."""
    doc = (getattr(described, "__doc__", None) or "").strip()
    if not doc:
        return None
    paragraph = doc.split("\n\n")[0]
    return " ".join(line.strip() for line in paragraph.splitlines())


def _name(described) -> str:
    name = getattr(described, "__qualname__", None) or type(described).__qualname__
    return f"{getattr(described, '__module__', '')}.{name}"


def _described_by_docstring(described) -> dict:
    summary = _summary(described)
    if summary is None:
        return undescribed(_name(described))
    return custom(summary)


def describe_check(check) -> list[dict]:
    if isinstance(check, Check):
        return [check.describe()]
    entries = getattr(check, "access_entries", None)
    if entries:
        return list(entries)
    required = getattr(check, "permission", None)
    if required is not None:
        return [permission(required, getattr(check, "sources", None))]
    return [_described_by_docstring(check)]


def requires_object(check) -> bool:
    """Whether ``ActionsPermission`` looks the object up before calling ``check``.

    It does for ``permission_factory`` checks with ``sources``. Other checks
    that test an object get it only from ``has_object_permission``, which DRF
    calls when the view runs ``get_object()``.
    """
    return bool(getattr(check, "sources", None))


def needs_object(check) -> bool:
    """Whether ``check`` passes everyone when it is not given an object.

    Every check that ``requires_object`` does, and every check that says so
    with ``needs_object``: ``permission_factory`` checks and the shared role
    checks among them.
    """
    return requires_object(check) or bool(getattr(check, "needs_object", False))


def has_lookup(view, url_kwargs=None) -> bool:
    """Whether the request URL names the single object view.get_object() looks up.

    ``url_kwargs`` are the URL's argument names, for when the view has not
    been given a request (as in schema generation); otherwise the view's own
    ``kwargs`` are read.
    """
    if not hasattr(view, "get_object"):
        return False
    lookup = getattr(view, "lookup_url_kwarg", None) or getattr(
        view, "lookup_field", None
    )
    if url_kwargs is None:
        url_kwargs = getattr(view, "kwargs", None) or {}
    return bool(lookup) and lookup in url_kwargs


def describe_permission_class(
    permission_class, view, method, requires_authentication
) -> list[dict]:
    describe_access = getattr(permission_class, "describe_access", None)
    if describe_access is not None:
        return describe_access(
            view, method, requires_authentication=requires_authentication
        )
    if issubclass(permission_class, AllowAny):
        return [public()]
    if issubclass(permission_class, IsAuthenticated):
        # Required of every request by default; it says nothing about who.
        return []
    return [_described_by_docstring(permission_class)]


def read_access(requires_authentication) -> list[dict]:
    """What a permission class that lets every read through says about one."""
    if requires_authentication:
        return [authenticated()]
    return [public()]


@dataclass
class RouteAccess:
    entries: list[dict] = field(default_factory=list)
    # How a list or retrieve is narrowed to what the caller may see.
    visibility: str | None = None
    requires_authentication: bool = True
    # What a custom visibility shows, in a sentence.
    visibility_description: str | None = None

    @property
    def declared(self) -> bool:
        """Something applies to the route."""
        return bool(self.entries) or self.visibility is not None

    @property
    def described(self) -> bool:
        """Something applies, and all of it says who may call the route."""
        if self.visibility == CUSTOM_VISIBILITY and not self.visibility_description:
            return False
        return self.declared and all(_is_described(entry) for entry in self.entries)

    def schema_entries(self) -> list[dict]:
        if self.entries:
            return self.entries
        if self.visibility:
            return read_access(self.requires_authentication)
        return [undeclared()]


def _is_described(entry) -> bool:
    if entry["kind"] == "undescribed":
        return False
    if entry["kind"] == "any_of":
        return all(_is_described(alternative) for alternative in entry["of"])
    return True


def _permission_instances(view):
    """The view's permissions, as DRF asks for them.

    ``get_permissions()`` may be overridden to choose by action or by the
    request. An override that cannot answer for the request at hand is
    described by the classes the view declares, as DRF's default would.
    """
    try:
        return list(view.get_permissions())
    except Exception:
        return [cls() for cls in getattr(view, "permission_classes", ())]


def _copies(entries):
    """Distinct entries, copied, so that no caller shares a mutable dict."""
    seen, result = set(), []
    for entry in entries:
        key = json.dumps(entry, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            result.append(json.loads(key))
    return result


def _declarations(view) -> dict:
    """The view's ``access_declarations``, over those of its bases.

    A mixin declares the actions it adds; a class's own entry for an action
    replaces what its bases declare for it.
    """
    declarations = {}
    for klass in reversed(type(view).__mro__):
        own = vars(klass).get("access_declarations")
        if isinstance(own, dict):
            declarations.update(own)
    own = getattr(view, "__dict__", {}).get("access_declarations")
    if isinstance(own, dict):
        declarations.update(own)
    return declarations


def access_for(view, method, url_kwargs=None) -> RouteAccess:
    """What protects ``method`` on ``view``, a view instance with its action set.

    ``url_kwargs`` names the route's URL arguments when the view's ``kwargs``
    do not, so that a route wired by hand with a lookup counts as having an
    object, as it does for ``ActionsPermission``.
    """
    method = method.upper()
    action = getattr(view, "action", None)
    instances = _permission_instances(view)
    requires_authentication = any(
        isinstance(instance, IsAuthenticated) for instance in instances
    )
    has_object = bool(getattr(view, "detail", False)) or has_lookup(view, url_kwargs)

    entries = []
    for instance in instances:
        if hasattr(instance, "get_permission_checks"):
            # ActionsPermission: the checks it would run for this action.
            if action is None:
                continue
            request = SimpleNamespace(method=method)
            checks = instance.get_permission_checks(request, view)
            if not isinstance(checks, list | tuple):
                continue
            for check in checks:
                if needs_object(check) and not has_object:
                    # Called without an object, it lets everyone through.
                    continue
                entries.extend(describe_check(check))
        else:
            entries.extend(
                describe_permission_class(
                    type(instance), view, method, requires_authentication
                )
            )

    describe_create_access = getattr(view, "describe_create_access", None)
    if action == "create" and describe_create_access is not None:
        entries.extend(describe_create_access())

    declarations = _declarations(view)
    entries.extend(declarations.get(action or method.lower(), []))

    visibility = visibility_description = None
    filtered_actions = FILTERED_ACTIONS + tuple(
        getattr(view, "access_filtered_actions", ())
    )
    if method in SAFE_METHODS and action in filtered_actions:
        # The backends this action runs: an action may name its own.
        backends = tuple(getattr(view, "filter_backends", None) or ())
        shown = getattr(getattr(view, "get_queryset", None), "access_shows", None)
        if isinstance(shown, str):
            visibility, visibility_description = CUSTOM_VISIBILITY, shown
        else:
            for backend in backends:
                visibility = getattr(backend, "access_visibility", None)
                if visibility:
                    if visibility == CUSTOM_VISIBILITY:
                        visibility_description = _summary(backend)
                    break
        if visibility == CUSTOM_VISIBILITY and visibility_description:
            notes = [
                note
                for backend in backends
                if isinstance(
                    note := getattr(backend, "access_visibility_note", None), str
                )
            ]
            visibility_description = " ".join([visibility_description, *notes])

    return RouteAccess(
        _copies(entries),
        visibility,
        requires_authentication,
        visibility_description=visibility_description,
    )
