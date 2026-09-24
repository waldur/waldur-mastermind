"""SCIM discovery documents for ``/scim/v2/sram/``.

The generic documents describe ``/scim/v2/``: PATCH, Waldur's own extension as
the only custom schema. The SRAM profile differs -- users and groups are sent
whole with PUT (PATCH is refused), and resources carry the SRAM extensions and
``x509Certificates`` -- so it advertises its own capabilities, and it is gated
on the SRAM switch like the resource endpoints it describes.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from scim2_models import (
    Attribute,
    Group,
    Mutability,
    ResourceType,
    SchemaExtension,
)
from scim2_models import Schema as ScimSchema

from waldur_core.users.scim.server import views as scim_views
from waldur_core.users.scim.server.auth import ScimFeatureEnabled
from waldur_core.users.scim.server.mapping import (
    CORE_USER_URN,
    ENTERPRISE_USER_URN,
    WALDUR_USER_EXTENSION_URN,
)

from .mapping import CORE_GROUP_URN, SRAM_GROUP_EXTENSION_URN, SRAM_USER_EXTENSION_URN
from .views import SramIntegrationEnabled

# What the SRAM user views accept and echo back (see ``mapping.render_user``).
SRAM_USER_ATTRIBUTES = {
    "userName",
    "name",
    "displayName",
    "emails",
    "phoneNumbers",
    "active",
    "x509Certificates",
}


def _string(name: str, description: str, multi_valued: bool = False) -> Attribute:
    return Attribute(
        name=name,
        type="string",
        multi_valued=multi_valued,
        required=False,
        case_exact=True,
        description=description,
    )


def _sram_user_core_schema() -> ScimSchema:
    schema = scim_views.trimmed_user_schema(SRAM_USER_ATTRIBUTES)
    for attr in schema.attributes:
        if attr.name in ("userName", "displayName"):
            # Stored as sent and echoed back, so SRAM's sweep sees its own value.
            attr.mutability = Mutability.read_write
    return schema


def _sram_user_extension_schema() -> ScimSchema:
    return ScimSchema(
        id=SRAM_USER_EXTENSION_URN,
        name="SramUser",
        description="SRAM attributes of a user, stored and echoed back unchanged.",
        attributes=[
            _string("eduPersonUniqueId", "SRAM's persistent identifier."),
            _string("eduPersonScopedAffiliation", "Scoped affiliations."),
            _string("voPersonExternalId", "Identifier at the home institution."),
            _string("voPersonExternalAffiliation", "Home-institution affiliation."),
            Attribute(
                name="sramInactiveDays",
                type="integer",
                multi_valued=False,
                required=False,
                description="Days since the user was last active in SRAM.",
            ),
        ],
    )


def _sram_group_extension_schema() -> ScimSchema:
    return ScimSchema(
        id=SRAM_GROUP_EXTENSION_URN,
        name="SramGroup",
        description="SRAM attributes of a collaboration or group.",
        attributes=[
            _string("urn", "organisation:collaboration[:group]."),
            _string("description", "Description."),
            _string("labels", "Collaboration labels.", multi_valued=True),
            Attribute(
                name="links",
                type="complex",
                multi_valued=True,
                required=False,
                description="Links to the collaboration in SRAM.",
                sub_attributes=[
                    _string("name", "Link name."),
                    _string("value", "Link target."),
                ],
            ),
        ],
    )


def _build_schemas() -> list[ScimSchema]:
    return [
        _sram_user_core_schema(),
        Group.to_schema(),
        scim_views.trimmed_enterprise_schema(),
        scim_views.waldur_extension_schema(),
        _sram_user_extension_schema(),
        _sram_group_extension_schema(),
    ]


def _build_resource_types() -> list[ResourceType]:
    return [
        ResourceType(
            id="User",
            name="User",
            endpoint="/Users",
            description="User provisioned by SRAM",
            schema=CORE_USER_URN,
            schema_extensions=[
                SchemaExtension(schema_=SRAM_USER_EXTENSION_URN, required=False),
                SchemaExtension(schema_=ENTERPRISE_USER_URN, required=False),
                SchemaExtension(schema_=WALDUR_USER_EXTENSION_URN, required=False),
            ],
        ),
        ResourceType(
            id="Group",
            name="Group",
            endpoint="/Groups",
            description="SRAM collaboration or group",
            schema=CORE_GROUP_URN,
            schema_extensions=[
                SchemaExtension(schema_=SRAM_GROUP_EXTENSION_URN, required=False),
            ],
        ),
    ]


SCHEMAS = _build_schemas()
RESOURCE_TYPES = _build_resource_types()


class _SramDiscovery:
    # Unauthenticated per RFC 7644 §4, but hidden while SRAM is switched off.
    permission_classes = [ScimFeatureEnabled, SramIntegrationEnabled]


@extend_schema(exclude=True)
class ServiceProviderConfigView(_SramDiscovery, scim_views.ServiceProviderConfigView):
    patch_supported = False


@extend_schema(exclude=True)
class ResourceTypesView(_SramDiscovery, scim_views.ResourceTypesView):
    def get_resource_types(self):
        return RESOURCE_TYPES


@extend_schema(exclude=True)
class ResourceTypeDetailView(_SramDiscovery, scim_views.ResourceTypeDetailView):
    def get_resource_types(self):
        return RESOURCE_TYPES


@extend_schema(exclude=True)
class SchemasView(_SramDiscovery, scim_views.SchemasView):
    def get_schemas(self):
        return SCHEMAS


@extend_schema(exclude=True)
class SchemaDetailView(_SramDiscovery, scim_views.SchemaDetailView):
    def get_schemas(self):
        return SCHEMAS
