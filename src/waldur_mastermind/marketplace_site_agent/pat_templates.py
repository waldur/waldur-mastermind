"""The personal access token template for a site agent.

The scopes are the permissions the agent's own calls are checked against.
``tests/test_pat_template.py`` lists those calls with their permissions and
fails when one is missing here. Change the two together.
"""

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import OfferingRole
from waldur_core.permissions.pat_templates import (
    PatTemplate,
    PatTemplateFeature,
    register,
)
from waldur_mastermind.marketplace import enums as marketplace_enums
from waldur_mastermind.marketplace import models as marketplace_models

SITE_AGENT_TEMPLATE_KEY = "site_agent"

REQUIRED_SCOPES = (
    # Accept, reject and finish orders.
    PermissionEnum.APPROVE_ORDER,
    PermissionEnum.REJECT_ORDER,
    # Record the backend's id, state, limits and metadata of a resource.
    PermissionEnum.SET_RESOURCE_BACKEND_ID,
    PermissionEnum.SET_RESOURCE_STATE,
    PermissionEnum.SET_RESOURCE_BACKEND_METADATA,
    # Report usage, per resource and per user.
    PermissionEnum.SET_RESOURCE_USAGE,
    # Move offering users through account creation and deletion.
    PermissionEnum.UPDATE_OFFERING_USER,
    # Register the agent, its services and queues; report policy commands.
    PermissionEnum.MANAGE_OFFERING_AGENT,
)

# Held by the user's role on the offering, not carried by the token: which
# orders the agent sees follows ORDER.LIST, and a resource's team the right to
# change the offering.
ROLE_PERMISSIONS = (
    PermissionEnum.LIST_ORDERS,
    PermissionEnum.UPDATE_OFFERING,
)

OPTIONAL_FEATURES = (
    PatTemplateFeature(
        key="sync_components",
        name="Sync offering components",
        description="Create and update the offering's components from the backend.",
        scopes=(PermissionEnum.UPDATE_OFFERING_COMPONENTS,),
    ),
    PatTemplateFeature(
        key="backend_resource_import",
        name="Import backend resources",
        description="Report resources found on the backend and process import requests.",
        scopes=(PermissionEnum.MANAGE_OFFERING_BACKEND_RESOURCES,),
    ),
    PatTemplateFeature(
        key="ssh_keys",
        name="SSH keys of the offering's users",
        description=(
            "Read the public SSH keys of the users of the offering's resources, "
            "for backends that resolve SSH key references in resource attributes."
        ),
        scopes=(PermissionEnum.LIST_SERVICE_PROVIDER_KEYS,),
    ),
    PatTemplateFeature(
        key="resource_api_keys",
        name="Resource API keys",
        description="Create, pause and delete the API keys of the offering's resources.",
        scopes=(PermissionEnum.MANAGE_RESOURCE_API_KEY,),
    ),
)


def _agent_offerings():
    # Only an organization registered as a service provider: the agent looks
    # its service provider up at startup and stops without one.
    return (
        marketplace_models.Offering.objects.filter(
            type__in=marketplace_enums.SITE_AGENT_COMPATIBLE_OFFERING_TYPES,
            customer__serviceprovider__isnull=False,
        )
        .exclude(state=marketplace_models.Offering.States.ARCHIVED)
        .select_related("customer")
    )


def _offering_context(offering) -> str:
    return offering.customer.name


SITE_AGENT_TEMPLATE = PatTemplate(
    key=SITE_AGENT_TEMPLATE_KEY,
    name="Site agent",
    description=(
        "Lets a site agent process orders, report usage and manage accounts "
        "for the offering it serves."
    ),
    required_scopes=REQUIRED_SCOPES,
    optional_features=OPTIONAL_FEATURES,
    role_permissions=ROLE_PERMISSIONS,
    # Only offerings. A token bound to an organization (and its service
    # provider, which it needs to see the resources sold) would also pass the
    # checks that read only the user's role and its binding, not the token's
    # scopes: an owner's agent token could then approve the organization's own
    # purchases or read the provider's account directory. A token bound to one
    # offering reaches neither. The offering binding reaches the offering's
    # service provider record through pat_filtering's ServiceProvider rule.
    binding_targets={"offering": _agent_offerings},
    # An organization owner's role counts for a token bound to one of its
    # offerings: the agent's checks accept a role above the binding
    # (binding_on_path), so the token reaches that offering and what belongs to
    # it, not the organization.
    role_ancestors={"offering": (("customer_id", "customer"),)},
    # A dedicated agent user is the offering's manager, so its token is
    # restricted to the offering and passes no owner-only check.
    dedicated_user_role={"offering": lambda: OfferingRole.MANAGER},
    target_context={"offering": _offering_context},
)


def register_templates():
    register(SITE_AGENT_TEMPLATE)
