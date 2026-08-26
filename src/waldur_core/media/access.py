"""Access rules for files served by :class:`waldur_core.media.views.MediaView`.

``DatabaseStorage`` is the global default storage, so every ``FileField`` and
``ImageField`` in the codebase lands in :class:`waldur_core.media.models.File`
and is reachable through a single endpoint. The only handle that endpoint has on
a file is its storage path, so access rules are keyed by the ``upload_to``
prefix.

Rules are **deny by default**: a file whose prefix has no registered rule is not
served to anyone. Each app declares the rules for its own prefixes in a
``media_access`` module, which :class:`waldur_core.media.apps.MediaConfig`
imports at startup. This keeps ``waldur_core`` free of imports from the apps
that own the files.
"""

import contextlib
import logging
from collections.abc import Callable

from django.core.exceptions import ImproperlyConfigured
from django.db.models import Model, Q
from django.utils.module_loading import autodiscover_modules

from waldur_core.core.models import User

from . import models

logger = logging.getLogger(__name__)

AccessRule = Callable[[models.File, User], bool]

# prefix -> rule. Lookup takes the longest matching prefix, so a nested prefix
# (``matrix_exports/.../media/``) can be given a rule of its own.
_rules: dict[str, AccessRule] = {}


def autodiscover() -> None:
    """Import every app's ``media_access`` module.

    Called from :class:`waldur_core.media.apps.MediaConfig` at startup. A
    ``media_access`` module that fails to import raises rather than warning:
    under default-deny a swallowed error means every file that app owns
    silently stops being served.

    Constraints on those modules -- ``MediaConfig.ready()`` runs before
    ``constance`` and before the plugin apps' own ``ready()``, so a
    ``media_access`` module must not touch the database or ``constance.config``
    at import time, and must not import its app's ``views`` or ``serializers``.
    Building a closure is fine; evaluating one is not.
    """
    autodiscover_modules("media_access")


def register(prefix: str, check: AccessRule, *, override: bool = False) -> None:
    """Declare who may download files stored under ``prefix``."""
    if not prefix.endswith("/"):
        raise ValueError(f"Media access prefix must end with a slash: {prefix!r}")
    if prefix in _rules and not override:
        raise ImproperlyConfigured(
            f"A media access rule for {prefix!r} is already registered. Two "
            "fields sharing a prefix must share one rule; pass override=True "
            "only if replacing it is intended."
        )
    _rules[prefix] = check


def register_public(prefix: str) -> None:
    """Serve ``prefix`` to everyone, including anonymous users.

    Only for files already exposed through an anonymous API endpoint.
    """
    register(prefix, lambda file, user: True)


def register_authenticated(prefix: str) -> None:
    """Serve ``prefix`` to any logged-in user, without a per-object check."""
    register(prefix, lambda file, user: user.is_authenticated)


def register_staff_only(prefix: str) -> None:
    register(
        prefix,
        lambda file, user: user.is_authenticated and (user.is_staff or user.is_support),
    )


@contextlib.contextmanager
def override_rules():
    """Swap in a throwaway registry for the duration of a test.

    Autodiscovery runs once per process, so a test that clears the registry
    without restoring it would leave every later test in the same worker
    denying everything.
    """
    snapshot = dict(_rules)
    _rules.clear()
    try:
        yield
    finally:
        _rules.clear()
        _rules.update(snapshot)


def get_rules() -> dict[str, AccessRule]:
    """Read-only view of the registry, for tests and introspection."""
    return dict(_rules)


def has_rule(name: str) -> bool:
    """Whether some registered prefix covers ``name``.

    ``name`` may be a full storage path or a bare prefix.
    """
    return _match_prefix(name) is not None


def user_can_access_file(file: models.File, user: User) -> bool:
    prefix = _match_prefix(file.name)
    if prefix is None:
        logger.debug(
            "Denying access to media file %s: no access rule is registered for its prefix.",
            file.name,
        )
        return False
    return _rules[prefix](file, user)


def _match_prefix(name: str) -> str | None:
    matches = [prefix for prefix in _rules if name.startswith(prefix)]
    if not matches:
        return None
    return max(matches, key=len)


def upload_prefix(model: type[Model], field_name: str) -> str:
    """Storage prefix of a ``FileField``, derived from its ``upload_to``.

    Deriving beats repeating the literal: renaming an ``upload_to`` would
    otherwise silently leave the old prefix registered and the new one open.
    A strftime-formatted ``upload_to`` is truncated at the first directory that
    contains a format token.
    """
    upload_to = model._meta.get_field(field_name).upload_to
    if not isinstance(upload_to, str):
        raise ValueError(
            f"{model.__name__}.{field_name} uses a callable upload_to; "
            "use image_prefix() or register an explicit prefix."
        )
    if not upload_to:
        raise ValueError(
            f"{model.__name__}.{field_name} has an empty upload_to, so its files "
            "land at the storage root and have no prefix to key a rule on."
        )
    if "%" in upload_to:
        upload_to = upload_to[: upload_to.index("%")]
    return upload_to.rstrip("/") + "/"


def image_prefix(model: type[Model]) -> str:
    """Storage prefix of a field using :func:`waldur_core.media.mixins.get_upload_path`.

    That helper builds paths from ``instance._meta.model_name`` alone, with no
    app label, so the prefix is the bare model name. Two image-bearing models
    with the same class name in different apps would therefore share a prefix
    and a rule. No such pair exists today, and
    ``CoverageTest.test_model_names_used_as_prefixes_are_unique`` keeps it that
    way.
    """
    return f"{model._meta.model_name}/"


def field_lookup(fields, name: str) -> Q:
    """``Q`` matching a storage path against any of ``fields``."""
    lookup = Q()
    for field_name in fields:
        lookup |= Q(**{field_name: name})
    return lookup


def queryset_rule(model: type[Model], fields, filter_fn) -> AccessRule:
    """Grant access when the owning row survives ``filter_fn`` for the user.

    ``fields`` are the ``FileField`` names on ``model`` that may hold the file's
    storage path. ``filter_fn(queryset, user)`` is the app's existing read rule
    -- usually ``filter_queryset_for_user`` or a manager's ``filter_for_user``
    -- so media access cannot drift from what the API itself allows.
    """

    def check(file: models.File, user: User) -> bool:
        if not user.is_authenticated:
            return False
        queryset = model.objects.filter(field_lookup(fields, file.name))
        return filter_fn(queryset, user).exists()

    return check
