"""Bidirectional mapping between SCIM 2.0 User resources and the Waldur User model.

Shared by:
- the inbound SCIM Service Provider (writes incoming SCIM resources into Waldur)
- the on-demand outbound SCIM pull (reads remote SCIM resources into Waldur)

The mapping deliberately covers the subset of attributes also accepted by
``waldur_auth_social.utils.update_user_attributes_from_source`` so that writes
flow through the existing multi-source attribute-merge policy.
"""

from __future__ import annotations

from typing import Any

from waldur_core.core.models import User

# Waldur-specific SCIM schema extension. Carries fields used by federated
# research deployments (civil_number, affiliations, eduperson_assurance).
WALDUR_USER_EXTENSION_URN = "urn:waldur:params:scim:schemas:extension:User:1.0"

# Canonical RFC 7643 schema URNs we honour.
CORE_USER_URN = "urn:ietf:params:scim:schemas:core:2.0:User"
ENTERPRISE_USER_URN = "urn:ietf:params:scim:schemas:extension:enterprise:2.0:User"


def scim_to_waldur_payload(scim_user: dict) -> dict:
    """Translate a SCIM User dict into a Waldur-flat-field payload.

    The output keys match attributes on ``waldur_core.core.models.User`` and is
    suitable as the ``payload`` argument to
    ``update_user_attributes_from_source``. Empty / absent attributes are
    omitted so the merge helper's preserve-other-sources policy can apply.
    """
    out: dict[str, Any] = {}

    name = scim_user.get("name") or {}
    if name.get("givenName"):
        out["first_name"] = name["givenName"]
    if name.get("familyName"):
        out["last_name"] = name["familyName"]

    primary_email = _pick_primary(scim_user.get("emails"))
    if primary_email:
        out["email"] = primary_email

    primary_phone = _pick_primary(scim_user.get("phoneNumbers"))
    if primary_phone:
        out["phone_number"] = primary_phone

    enterprise = scim_user.get(ENTERPRISE_USER_URN) or {}
    if enterprise.get("organization"):
        out["organization"] = enterprise["organization"]

    extension = scim_user.get(WALDUR_USER_EXTENSION_URN) or {}
    for scim_key, waldur_field in (
        ("civilNumber", "civil_number"),
        ("affiliations", "affiliations"),
        ("edupersonAssurance", "eduperson_assurance"),
    ):
        value = extension.get(scim_key)
        if value:
            out[waldur_field] = value

    return out


def waldur_to_scim_user(user: User, location: str | None = None) -> dict:
    """Render a Waldur User as a SCIM User resource (RFC 7643 §4.1).

    Used by GET / POST / PUT responses on /Users.
    """
    body: dict[str, Any] = {
        "schemas": [CORE_USER_URN],
        "id": user.uuid.hex,
        "userName": user.username,
        "active": user.is_active,
        "meta": {
            "resourceType": "User",
            "created": _iso(user.date_joined),
            "lastModified": _iso(getattr(user, "modified", None) or user.date_joined),
        },
    }
    if location:
        body["meta"]["location"] = location

    external_id = _get_external_id(user)
    if external_id:
        body["externalId"] = external_id

    if user.first_name or user.last_name:
        body["name"] = {
            "givenName": user.first_name or "",
            "familyName": user.last_name or "",
        }
    display = f"{user.first_name} {user.last_name}".strip() or user.username
    body["displayName"] = display

    if user.email:
        body["emails"] = [{"value": user.email, "primary": True}]
    if getattr(user, "phone_number", None):
        body["phoneNumbers"] = [{"value": user.phone_number, "primary": True}]

    if user.organization:
        body["schemas"].append(ENTERPRISE_USER_URN)
        body[ENTERPRISE_USER_URN] = {"organization": user.organization}

    extension: dict[str, Any] = {}
    if user.civil_number:
        extension["civilNumber"] = user.civil_number
    if user.affiliations:
        extension["affiliations"] = list(user.affiliations)
    if user.eduperson_assurance:
        extension["edupersonAssurance"] = list(user.eduperson_assurance)
    if extension:
        body["schemas"].append(WALDUR_USER_EXTENSION_URN)
        body[WALDUR_USER_EXTENSION_URN] = extension

    return body


def get_scim_external_id(user: User) -> str | None:
    return _get_external_id(user)


def set_scim_external_id(user: User, external_id: str, source: str) -> None:
    """Store SCIM externalId in attribute_sources without a schema migration.

    Stored shape: ``attribute_sources["externalId"] = {"value": "...", "source": "..."}``.
    """
    sources = dict(user.attribute_sources or {})
    sources["externalId"] = {"value": external_id, "source": source}
    user.attribute_sources = sources


def _pick_primary(items: list[dict] | None) -> str | None:
    """Return value of the first ``primary=True`` entry, else first entry."""
    if not items:
        return None
    primary = next(
        (item for item in items if isinstance(item, dict) and item.get("primary")),
        None,
    )
    chosen = primary if primary else (items[0] if isinstance(items[0], dict) else None)
    if not chosen:
        return None
    return chosen.get("value")


def _get_external_id(user: User) -> str | None:
    sources = user.attribute_sources or {}
    entry = sources.get("externalId")
    if isinstance(entry, dict):
        return entry.get("value")
    return None


def _iso(value) -> str | None:
    if value is None:
        return None
    try:
        return value.isoformat()
    except AttributeError:
        return str(value)
