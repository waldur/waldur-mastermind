from waldur_core.checklist import enums as checklist_enums
from waldur_core.core.enums import CoreStates
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.users.enums import InvitationState
from waldur_mastermind.common.enums import Units
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
    ServiceAccountState,
)
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
)
from waldur_rancher.enums import (
    RANCHER_TEMPLATE_QUESTION_TYPE,
    CatalogScopeTypeChoices,
    KeycloakUserGroupMembershipState,
    RoleScopeType,
)

SPECTACULAR_SETTINGS = {
    "TITLE": "Waldur API",
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "waldur_core.core.schema_hooks.add_polymorphic_attributes_schema",
        "waldur_core.core.schema_hooks.postprocess_drop_description",
        "waldur_core.core.schema_hooks.postprocess_fix_enum",
        "waldur_core.core.schema_hooks.refactor_pagination_parameters",
        "waldur_core.core.schema_hooks.transform_paginated_arrays",
        "waldur_core.core.schema_hooks.make_fields_optional",
        "waldur_core.core.schema_hooks.remove_waldur_cookie_auth",
        "waldur_core.core.schema_hooks.preprocess_request_bodies",
        "waldur_core.core.schema_hooks.add_result_count_header",
    ],
    "DEFAULT_GENERATOR_CLASS": "waldur_core.core.openapi_generators.WaldurSchemaGenerator",
    "ENUM_GENERATE_CHOICE_DESCRIPTION": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/",
    "ENUM_NAME_OVERRIDES": {
        "RoleType": TYPE_MAP.keys(),
        "InvitationState": InvitationState.VALUES,
        "BillingUnit": Units.CHOICES,
        "CoreStates": CoreStates.VALUES,
        "OfferingState": OfferingStates.VALUES,
        "OrderState": OrderStates.VALUES,
        "ResourceState": ResourceStates.VALUES,
        "CallStates": CallStates.CHOICES,
        "ProposalStates": ProposalStates.CHOICES,
        "RequestedOfferingStates": RequestedOfferingStates.CHOICES,
        "RequestTypes": OrderTypes.VALUES,
        "RancherTemplateQuestionType": RANCHER_TEMPLATE_QUESTION_TYPE,
        "RancherRoleScopeType": RoleScopeType.CHOICES,
        "KeycloakUserGroupMembershipState": KeycloakUserGroupMembershipState.CHOICES,
        "RancherCatalogScopeType": CatalogScopeTypeChoices,
        "RobotAccountStates": RobotAccountStates.CHOICES,
        "ChecklistOperators": checklist_enums.OPERATORS,
        "ServiceAccountState": ServiceAccountState.VALUES,
        "OfferingUserState": OfferingUserStates.VALUES,
    },
    "VERSION": None,
}
