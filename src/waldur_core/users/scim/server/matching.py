"""Match inbound SCIM users to existing Waldur accounts.

A SCIM-provisioned user must end up as the same account the person gets when
they log in, or they end up with two. Deployments differ in which value ties
the two together, so both sides are configurable:

- ``SCIM_USER_MATCH_WALDUR_ATTRIBUTE``: the ``User`` field to compare,
  ``username`` by default or one of the enabled profile attributes.
- ``SCIM_USER_MATCH_SCIM_ATTRIBUTE``: where the value is in the SCIM User,
  ``userName`` by default. Dotted sub-attributes (``name.givenName``) and
  extension paths (``urn:…:User.eduPersonUniqueId``) are accepted.

When the Waldur attribute is ``username``, new accounts are created with that
value as their username, so a later login finds them.
"""

from __future__ import annotations

import re

from constance import config
from django.core.exceptions import FieldDoesNotExist
from django.db.models import CharField, EmailField, TextField

from waldur_core.core.models import User
from waldur_core.core.user_attributes import get_enabled_profile_attributes
from waldur_core.users.scim.server.exceptions import ScimError

DEFAULT_WALDUR_ATTRIBUTE = "username"
DEFAULT_SCIM_ATTRIBUTE = "userName"
CASE_INSENSITIVE_FIELDS = {"username", "email"}
# Only attributes that identify a person. Names or titles would link strangers.
IDENTIFYING_ATTRIBUTES = ("username", "email", "civil_number")


def is_matchable_field(name: str) -> bool:
    """A concrete single-value text column on ``User``."""
    try:
        field = User._meta.get_field(name)
    except FieldDoesNotExist:
        return False
    return field.concrete and isinstance(field, (CharField, EmailField, TextField))


def validate_waldur_attribute(name: str) -> None:
    """Raise ``ValueError`` unless ``name`` may be used for matching."""
    if name == DEFAULT_WALDUR_ATTRIBUTE:
        return
    if name not in IDENTIFYING_ATTRIBUTES:
        raise ValueError(
            f"{name!r} cannot identify a user; use one of "
            f"{', '.join(IDENTIFYING_ATTRIBUTES)}."
        )
    if not is_matchable_field(name):
        raise ValueError(f"{name!r} is not a text attribute of a Waldur user.")
    if name not in get_enabled_profile_attributes():
        raise ValueError(
            f"{name!r} is not an enabled user attribute "
            "(see ENABLED_USER_PROFILE_ATTRIBUTES)."
        )


def waldur_attribute() -> str:
    name = config.SCIM_USER_MATCH_WALDUR_ATTRIBUTE or DEFAULT_WALDUR_ATTRIBUTE
    try:
        validate_waldur_attribute(name)
    except ValueError as exc:
        # The Django admin writes Constance without the API's validation, and
        # the enabled attributes can change later. Refuse rather than match on
        # a field nobody meant.
        raise ScimError(503, f"SCIM_USER_MATCH_WALDUR_ATTRIBUTE: {exc}")
    return name


def scim_attribute() -> str:
    return (config.SCIM_USER_MATCH_SCIM_ATTRIBUTE or DEFAULT_SCIM_ATTRIBUTE).strip()


def _resolve_path(body: dict, path: str):
    node = body
    if path.lower().startswith("urn:"):
        # The schema URN itself may contain dots (``surf.nl``), so match the
        # longest top-level key the path starts with.
        prefixes = [
            key
            for key in body
            if isinstance(key, str)
            and (path.startswith(key + ".") or path.startswith(key + ":"))
        ]
        if not prefixes:
            return None
        key = max(prefixes, key=len)
        node = body.get(key)
        path = path[len(key) + 1 :]
    for part in path.split("."):
        if isinstance(node, list):
            node = _primary(node)
        if not isinstance(node, dict):
            return None
        node = _get_case_insensitive(node, part)
    if isinstance(node, list):
        node = _primary(node)
    if isinstance(node, dict):
        node = node.get("value")
    return node


def _primary(items: list):
    dicts = [item for item in items if isinstance(item, dict)]
    for item in dicts:
        if item.get("primary"):
            return item
    return dicts[0] if dicts else None


def _get_case_insensitive(node: dict, key: str):
    if key in node:
        return node[key]
    lowered = key.lower()
    for name, value in node.items():
        if isinstance(name, str) and name.lower() == lowered:
            return value
    return None


def match_value(body: dict) -> str | None:
    """The configured SCIM attribute's value, or ``None`` when absent."""
    value = _resolve_path(body, scim_attribute())
    if value is None:
        return None
    value = str(value).strip()
    return value or None


def matching_users(body: dict, value: str | None = None):
    """Accounts (active or not) whose configured attribute equals the value."""
    value = value if value is not None else match_value(body)
    if not value:
        return User.all_objects.none()
    field = waldur_attribute()
    lookup = f"{field}__iexact" if field in CASE_INSENSITIVE_FIELDS else field
    if field == DEFAULT_WALDUR_ATTRIBUTE:
        value = normalize_username(value, strict=False) or value
    return User.all_objects.filter(**{lookup: value})


def find_matching_user(body: dict) -> User | None:
    """The single matching account; 409 when the match is ambiguous."""
    users = list(matching_users(body)[:2])
    if len(users) > 1:
        raise ScimError(
            409,
            f"More than one account matches {scim_attribute()} "
            f"on {waldur_attribute()}; resolve the duplicate first.",
            scim_type="uniqueness",
        )
    return users[0] if users else None


def normalize_username(raw: str, strict: bool = True) -> str | None:
    """Waldur usernames must match ``[0-9a-z_.@+-]+``.

    SCIM values may include other characters and uppercase; they are lowercased
    and stripped rather than rejected, so common IdP values still flow through.
    """
    cleaned = "".join(re.findall(r"[0-9a-z_.@+\-]+", raw.lower()))
    if not cleaned and strict:
        raise ScimError(
            400,
            f"Value {raw!r} contains no characters valid for a Waldur username.",
            scim_type="invalidValue",
        )
    return cleaned or None
