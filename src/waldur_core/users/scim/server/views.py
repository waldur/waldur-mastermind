"""SCIM 2.0 Service Provider views (RFC 7644).

Discovery endpoints (ServiceProviderConfig, ResourceTypes, Schemas) are
unauthenticated per RFC 7644 §4 — they advertise the server's capabilities and
must be readable by clients before they can authenticate. We still gate them on
SCIM_INBOUND_ENABLED so the deployment can hide the surface entirely.

Resource endpoints (/Users, /Groups) require staff bearer authentication and
will be added alongside the User/Group adapters.
"""

from __future__ import annotations

from drf_spectacular.utils import extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView
from scim2_models import (
    Attribute,
    AuthenticationScheme,
    Bulk,
    ChangePassword,
    EnterpriseUser,
    ETag,
    Filter,
    Group,
    Mutability,
    Patch,
    ResourceType,
    SchemaExtension,
    ServiceProviderConfig,
    Sort,
    User,
)
from scim2_models import (
    Schema as ScimSchema,
)

from waldur_core.users.scim.server.auth import ScimFeatureEnabled
from waldur_core.users.scim.server.exceptions import ScimError
from waldur_core.users.scim.server.renderers import ScimJSONParser, ScimJSONRenderer

WALDUR_USER_EXTENSION_URN = "urn:waldur:params:scim:schemas:extension:User:1.0"


class _ScimBaseView(APIView):
    """Common configuration: SCIM content type + feature-flag gate + SCIM error shape."""

    renderer_classes = [ScimJSONRenderer]
    parser_classes = [ScimJSONParser]
    schema = None  # exclude from drf-spectacular

    def get_exception_handler(self):
        from waldur_core.users.scim.server.exceptions import scim_exception_handler

        return scim_exception_handler


class _ScimDiscoveryView(_ScimBaseView):
    """Discovery endpoints don't require authentication, only the feature flag."""

    authentication_classes: list = []
    permission_classes = [ScimFeatureEnabled]


@extend_schema(exclude=True)
class ServiceProviderConfigView(_ScimDiscoveryView):
    """``GET /scim/v2/ServiceProviderConfig`` — RFC 7644 §4."""

    def get(self, request):
        config_obj = ServiceProviderConfig(
            documentation_uri="https://docs.waldur.com/",
            patch=Patch(supported=True),
            bulk=Bulk(supported=False, max_operations=0, max_payload_size=0),
            filter=Filter(supported=True, max_results=200),
            change_password=ChangePassword(supported=False),
            sort=Sort(supported=False),
            etag=ETag(supported=False),
            authentication_schemes=[
                AuthenticationScheme(
                    type="oauthbearertoken",
                    name="OAuth Bearer Token",
                    description=(
                        "Authentication via bearer token tied to a staff "
                        "service-account in the Authorization header."
                    ),
                )
            ],
        )
        return Response(config_obj.model_dump(by_alias=True, exclude_none=True))


@extend_schema(exclude=True)
class ResourceTypesView(_ScimDiscoveryView):
    """``GET /scim/v2/ResourceTypes`` — RFC 7644 §4."""

    def get(self, request):
        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
                "totalResults": len(_RESOURCE_TYPES),
                "Resources": [
                    rt.model_dump(by_alias=True, exclude_none=True)
                    for rt in _RESOURCE_TYPES
                ],
            }
        )


@extend_schema(exclude=True)
class ResourceTypeDetailView(_ScimDiscoveryView):
    def get(self, request, name):
        for rt in _RESOURCE_TYPES:
            if rt.id == name:
                return Response(rt.model_dump(by_alias=True, exclude_none=True))
        raise ScimError(404, f"ResourceType {name!r} not found")


@extend_schema(exclude=True)
class SchemasView(_ScimDiscoveryView):
    """``GET /scim/v2/Schemas`` — RFC 7644 §4."""

    def get(self, request):
        return Response(
            {
                "schemas": ["urn:ietf:params:scim:api:messages:2.0:ListResponse"],
                "totalResults": len(_SCHEMAS),
                "Resources": [
                    s.model_dump(by_alias=True, exclude_none=True) for s in _SCHEMAS
                ],
            }
        )


@extend_schema(exclude=True)
class SchemaDetailView(_ScimDiscoveryView):
    def get(self, request, urn):
        for s in _SCHEMAS:
            if s.id == urn:
                return Response(s.model_dump(by_alias=True, exclude_none=True))
        raise ScimError(404, f"Schema {urn!r} not found")


@extend_schema(exclude=True)
class ScimNotFoundView(_ScimDiscoveryView):
    """Catch-all for unknown paths under /scim/v2/ — emits a SCIM-shaped 404
    instead of the site-wide HTML error page (RFC 7644 §3.12)."""

    def _not_found(self, request, *args, **kwargs):
        raise ScimError(404, f"Unknown SCIM endpoint {request.path!r}.")

    get = post = put = patch = delete = _not_found


def _build_resource_types() -> list[ResourceType]:
    user_rt = ResourceType(
        id="User",
        name="User",
        endpoint="/Users",
        description="User Account",
        schema="urn:ietf:params:scim:schemas:core:2.0:User",
        # Clients read schemaExtensions to know which extension URNs may
        # appear in resources — omitting them breaks response validation.
        schema_extensions=[
            SchemaExtension(
                schema_="urn:ietf:params:scim:schemas:extension:enterprise:2.0:User",
                required=False,
            ),
            SchemaExtension(schema_=WALDUR_USER_EXTENSION_URN, required=False),
        ],
    )
    group_rt = ResourceType(
        id="Group",
        name="Group",
        endpoint="/Groups",
        description="Group",
        schema="urn:ietf:params:scim:schemas:core:2.0:Group",
    )
    return [user_rt, group_rt]


def _build_schemas() -> list[ScimSchema]:
    """Return the schemas this service provider advertises.

    SCIM clients use the published attribute definitions (required/mutability
    flags) to build payloads, so publishing stubs breaks schema-driven
    provisioning. We start from the canonical RFC 7643 definitions generated
    by scim2-models, trimmed to the attributes Waldur actually persists and
    with mutability adjusted to Waldur semantics (RFC 7643 allows service
    providers to define a subset). The Waldur extension covers fields specific
    to research / federated deployments.
    """
    waldur_schema = ScimSchema(
        id=WALDUR_USER_EXTENSION_URN,
        name="WaldurUserExtension",
        description=(
            "Waldur-specific user attributes for federated research deployments."
        ),
        attributes=[
            Attribute(
                name="civilNumber",
                type="string",
                multi_valued=False,
                required=False,
                case_exact=True,
                description="National identity number.",
            ),
            Attribute(
                name="affiliations",
                type="string",
                multi_valued=True,
                required=False,
                description="eduPerson affiliations.",
            ),
            Attribute(
                name="edupersonAssurance",
                type="string",
                multi_valued=True,
                required=False,
                description="eduPersonAssurance identity assurance values.",
            ),
            Attribute(
                name="sshPublicKeys",
                type="complex",
                multi_valued=True,
                required=False,
                mutability=Mutability.read_write,
                description=(
                    "User SSH public keys. Managed when "
                    "SCIM_INBOUND_SSH_KEYS_ENABLED is set."
                ),
                sub_attributes=[
                    Attribute(
                        name="value",
                        type="string",
                        multi_valued=False,
                        required=True,
                        case_exact=True,
                        description="SSH public key material.",
                    ),
                    Attribute(
                        name="display",
                        type="string",
                        multi_valued=False,
                        required=False,
                        description="Human-readable key name.",
                    ),
                    Attribute(
                        name="primary",
                        type="boolean",
                        multi_valued=False,
                        required=False,
                    ),
                ],
            ),
        ],
    )
    return [
        _trimmed_user_schema(),
        Group.to_schema(),
        _trimmed_enterprise_schema(),
        waldur_schema,
    ]


_SUPPORTED_USER_ATTRIBUTES = {
    "userName",
    "name",
    "displayName",
    "emails",
    "phoneNumbers",
    "active",
}


def _trimmed_user_schema() -> ScimSchema:
    schema = User.to_schema()
    schema.attributes = [
        attr for attr in schema.attributes if attr.name in _SUPPORTED_USER_ATTRIBUTES
    ]
    for attr in schema.attributes:
        if attr.name == "userName":
            # Immutable post-creation; changes are rejected with 400 mutability.
            attr.mutability = Mutability.immutable
        elif attr.name == "displayName":
            # Derived from name/userName, never written directly.
            attr.mutability = Mutability.read_only
    return schema


def _trimmed_enterprise_schema() -> ScimSchema:
    schema = EnterpriseUser.to_schema()
    schema.attributes = [
        attr for attr in schema.attributes if attr.name == "organization"
    ]
    return schema


_RESOURCE_TYPES = _build_resource_types()
_SCHEMAS = _build_schemas()
