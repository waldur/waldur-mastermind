import logging
import re
from string import Template

from constance import config

from waldur_core.core import utils as core_utils

from .client import KeycloakClient
from .enums import KeycloakMembershipState

logger = logging.getLogger(__name__)


def is_keycloak_enabled(offering) -> bool:
    """Check if Keycloak integration is enabled for the offering."""
    return bool(offering.plugin_options.get("keycloak_enabled"))


def get_keycloak_client_for_offering(offering) -> KeycloakClient:
    """Build a KeycloakClient from offering.secret_options."""
    secret = offering.secret_options
    required_keys = [
        "keycloak_url",
        "keycloak_realm",
        "keycloak_username",
        "keycloak_password",
    ]
    missing = [key for key in required_keys if key not in secret]
    if missing:
        logger.error(
            "Missing required Keycloak configuration in offering %s secret_options: %s",
            offering.uuid,
            ", ".join(missing),
        )
        raise ValueError(
            "Keycloak integration is not fully configured for this offering."
        )
    return KeycloakClient(
        {
            "keycloak_url": secret["keycloak_url"],
            "keycloak_realm": secret["keycloak_realm"],
            "keycloak_user_realm": secret.get("keycloak_user_realm", "master"),
            "keycloak_username": secret["keycloak_username"],
            "keycloak_password": secret["keycloak_password"],
            "keycloak_ssl_verify": secret.get("keycloak_ssl_verify", True),
        }
    )


def _get_template_variables(offering, role, resource=None, scope_id=None) -> dict:
    """Build template variables for Keycloak group name generation."""
    customer = offering.customer
    variables = {
        "role_name": role.name,
        "scope_id": scope_id or "",
        "offering_uuid": offering.uuid.hex,
        "offering_name": offering.name,
        "offering_slug": offering.slug,
        "organization_uuid": customer.uuid.hex if customer else "",
        "organization_name": customer.name if customer else "",
        "organization_slug": customer.slug if customer else "",
    }

    if resource:
        project = resource.project
        variables.update(
            {
                "resource_uuid": resource.uuid.hex,
                "resource_name": resource.name,
                "resource_slug": resource.slug,
                "project_uuid": project.uuid.hex,
                "project_name": project.name,
                "project_slug": project.slug,
            }
        )
    else:
        variables.update(
            {
                "resource_uuid": "",
                "resource_name": "",
                "resource_slug": "",
                "project_uuid": "",
                "project_name": "",
                "project_slug": "",
            }
        )

    return variables


_ALLOWED_TEMPLATE_VARS = frozenset(
    {
        "role_name",
        "scope_id",
        "offering_uuid",
        "offering_name",
        "offering_slug",
        "organization_uuid",
        "organization_name",
        "organization_slug",
        "resource_uuid",
        "resource_name",
        "resource_slug",
        "project_uuid",
        "project_name",
        "project_slug",
    }
)

# Matches $var or ${var} placeholders used by string.Template
_TEMPLATE_VAR_RE = re.compile(r"\$\{?(\w+)\}?")


def validate_group_name_template(template: str) -> str | None:
    """Return an error message if the template references disallowed variables."""
    referenced = set(_TEMPLATE_VAR_RE.findall(template))
    unknown = referenced - _ALLOWED_TEMPLATE_VARS
    if unknown:
        return f"Unknown template variables: {', '.join(sorted(unknown))}"
    return None


def get_keycloak_group_name(offering, role, resource=None, scope_id=None) -> str:
    """Generate a Keycloak group name.

    Default: {offering_uuid}_{role_name}
    With resource: {offering_uuid}_{resource_uuid}_{role_name}
    With scope_id: {offering_uuid}_{scope_id}_{role_name}

    Configurable via plugin_options['keycloak_group_name_template'].
    Uses string.Template (safe_substitute) to prevent format-string attacks.
    Available variables: offering_uuid, offering_name, offering_slug,
    resource_uuid, resource_name, resource_slug,
    project_uuid, project_name, project_slug,
    organization_uuid, organization_name, organization_slug,
    role_name, scope_id.
    """
    template_str = offering.plugin_options.get("keycloak_group_name_template")
    variables = _get_template_variables(offering, role, resource, scope_id)

    if template_str:
        error = validate_group_name_template(template_str)
        if error:
            logger.warning(
                "Invalid keycloak_group_name_template for offering %s: %s",
                offering.uuid,
                error,
            )
        return Template(template_str).safe_substitute(variables)

    parts = [offering.uuid.hex]
    if scope_id:
        parts.append(scope_id)
    elif resource:
        parts.append(resource.uuid.hex)
    parts.append(role.name)
    return "_".join(parts)


def get_base_group_name(offering) -> str | None:
    """Return the configured base group name, or None if not set."""
    return offering.plugin_options.get("keycloak_base_group") or None


def get_offering_parent_group_name(offering) -> str:
    """Return the offering-level parent group name (used as a sub-group under base)."""
    return offering.slug or offering.uuid.hex


def ensure_offering_group_hierarchy(keycloak: KeycloakClient, offering) -> str | None:
    """Ensure the group hierarchy exists in Keycloak and return the parent ID
    under which role groups should be created.

    Hierarchy (when keycloak_base_group is set):
        {base_group}/
            {offering_slug}/
                <role groups go here>

    Without base_group:
        {offering_slug}/
            <role groups go here>

    Returns the Keycloak group ID of the offering-level parent group.
    """
    base_group_name = get_base_group_name(offering)
    offering_group_name = get_offering_parent_group_name(offering)

    parent_id = None
    if base_group_name:
        base_group = keycloak.create_group(base_group_name, parent_id=None)
        parent_id = base_group["id"]

    offering_group = keycloak.create_group(offering_group_name, parent_id=parent_id)
    return offering_group["id"]


def create_keycloak_group_with_hierarchy(
    keycloak: KeycloakClient, offering, group_name: str
) -> dict:
    """Create a role group under the offering hierarchy.

    Returns the created group dict with 'id' key.
    """
    parent_id = ensure_offering_group_hierarchy(keycloak, offering)
    return keycloak.create_group(group_name, parent_id=parent_id)


def _flatten_groups(groups: list[dict]) -> list[dict]:
    """Flatten a nested group tree into a single list including all subgroups."""
    result = []
    for g in groups:
        result.append(g)
        result.extend(_flatten_groups(g.get("subGroups", [])))
    return result


def get_offering_groups_from_remote(keycloak: KeycloakClient, offering) -> list[dict]:
    """List remote Keycloak groups that belong to this offering.

    Navigates the hierarchy: base_group / offering_group / <children>.
    Falls back to prefix-matching at root level for backward compatibility.
    When neither hierarchy nor prefix match finds anything, returns all groups
    so they remain visible for import/remap.
    """
    all_groups = keycloak.list_groups()
    base_group_name = get_base_group_name(offering)
    offering_group_name = get_offering_parent_group_name(offering)

    # Try hierarchical lookup first
    search_groups = all_groups
    if base_group_name:
        for g in all_groups:
            if g.get("name") == base_group_name:
                search_groups = g.get("subGroups", [])
                break
        else:
            # Base group not found, fall back to prefix matching
            result = _prefix_match_groups(all_groups, offering)
            if result:
                return result
            # Neither hierarchy nor prefix match — return all groups
            return _flatten_groups(all_groups)

    for g in search_groups:
        if g.get("name") == offering_group_name:
            # Found the offering group, return its children (the role groups)
            return g.get("subGroups", [])

    # Offering group not found in hierarchy, fall back to prefix matching
    result = _prefix_match_groups(all_groups, offering)
    if result:
        return result
    # Neither hierarchy nor prefix match — return all groups
    return _flatten_groups(all_groups)


def _prefix_match_groups(groups: list[dict], offering) -> list[dict]:
    """Legacy fallback: find groups by offering UUID prefix at root level."""
    prefix = offering.uuid.hex
    return [g for g in groups if g.get("name", "").startswith(prefix)]


def send_membership_notification_email(membership, offering):
    """Send notification email when a user is added to a Keycloak group."""
    sync_frequency = offering.plugin_options.get("keycloak_sync_frequency", 15)
    context = {
        "offering_name": offering.name,
        "support_email": config.SITE_EMAIL,
        "role": membership.group.role.name,
        "user_exists": membership.state == KeycloakMembershipState.ACTIVE,
        "sync_frequency_minutes": sync_frequency,
    }

    core_utils.broadcast_mail(
        "keycloak",
        "keycloak_membership_notification",
        context,
        [membership.email],
    )
