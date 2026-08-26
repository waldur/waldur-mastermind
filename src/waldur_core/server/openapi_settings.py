from waldur_core.checklist import enums as checklist_enums
from waldur_core.core.enums import GENDER_CHOICES, CoreStates
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.onboarding.enums import VerificationStatus
from waldur_core.permissions.enums import TYPE_MAP
from waldur_core.server.constance_settings import (
    NOTIFY_SYSTEM_CHOICES,
    OFFERING_TYPE_CHOICES,
    ONBOARDING_VALIDATION_CHOICES,
    PROPOSAL_CONFIGURABLE_FIELD_CHOICES,
    USER_ATTRIBUTE_CHOICES,
)
from waldur_core.users.enums import InvitationState
from waldur_mastermind.chat.enums import FeedbackCategory
from waldur_mastermind.chat.input_guards.base import SeverityLevel
from waldur_mastermind.common.enums import Units
from waldur_mastermind.marketplace.attribute_types import ATTRIBUTE_TYPES
from waldur_mastermind.marketplace.enums import (
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceApiKeyStates,
    ResourceStates,
    RobotAccountStates,
    ServiceAccountState,
)
from waldur_mastermind.marketplace_site_agent.enums import AgentServiceState
from waldur_mastermind.proposal.enums import (
    WORKFLOW_STEPS_CHOICES,
    AssignmentBatchStatuses,
    AssignmentItemStatuses,
    AssignmentSources,
    CallStates,
    COISeverityLevels,
    COITypes,
    MatchingAlgorithms,
    ProposalFieldStates,
    ProposalStates,
    RequestedOfferingStates,
    RoundStatuses,
)
from waldur_mastermind.support.enums import ISSUE_STATUS_TYPE_CHOICES
from waldur_rancher.enums import (
    RANCHER_TEMPLATE_QUESTION_TYPE,
    ROLE_CHOICES,
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
        "waldur_core.core.schema_hooks.make_readonly_fields_required",
        "waldur_core.core.schema_hooks.relax_conditionally_optional_fields",
        "waldur_core.core.schema_hooks.remove_waldur_cookie_auth",
        "waldur_core.core.schema_hooks.mark_optional_request_bodies",
        "waldur_core.core.schema_hooks.preprocess_request_bodies",
        "waldur_core.core.schema_hooks.add_result_count_header",
        "waldur_core.core.schema_hooks.inject_waldur_operation_ids",
        "waldur_core.core.schema_hooks.validate_waldur_operation_ids",
        "waldur_core.core.schema_hooks.validate_go_sdk_naming_collisions",
        "waldur_core.core.schema_hooks.extract_query_enums",
        "waldur_core.core.schema_hooks.sanitize_schema",
        "waldur_core.core.schema_hooks.check_action_responses",
    ],
    "DEFAULT_GENERATOR_CLASS": "waldur_core.core.openapi_generators.WaldurSchemaGenerator",
    "ENUM_GENERATE_CHOICE_DESCRIPTION": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api/",
    "ENUM_NAME_OVERRIDES": {
        "RoleType": TYPE_MAP.keys(),
        "InvitationState": InvitationState.values,
        "BillingUnit": Units.CHOICES,
        "CoreStates": CoreStates.labels,
        "OfferingState": OfferingStates.VALUES,
        "OrderState": OrderStates.VALUES,
        "ResourceState": ResourceStates.VALUES,
        "CallStates": CallStates.CHOICES,
        "ProposalStates": ProposalStates.CHOICES,
        "RequestedOfferingStates": RequestedOfferingStates.CHOICES,
        "RoundStatus": RoundStatuses.VALUES,
        "RequestTypes": OrderTypes.VALUES,
        "RancherTemplateQuestionType": RANCHER_TEMPLATE_QUESTION_TYPE,
        "RancherRoleScopeType": RoleScopeType.CHOICES,
        "KeycloakUserGroupMembershipState": KeycloakUserGroupMembershipState.CHOICES,
        "RancherCatalogScopeType": CatalogScopeTypeChoices,
        "ResourceApiKeyState": ResourceApiKeyStates.VALUES,
        "RobotAccountStates": RobotAccountStates.VALUES,
        "ChecklistOperators": checklist_enums.OPERATORS,
        "ServiceAccountState": ServiceAccountState.VALUES,
        "OfferingUserState": OfferingUserStates.VALUES,
        "OnboardingVerificationStatus": VerificationStatus.VALUES,
        "AgentServiceState": AgentServiceState.VALUES,
        # Rename Rancher role enum to avoid conflict with permissions RoleEnum
        "RancherNodeRoleEnum": ROLE_CHOICES,
        # Matching algorithm choices
        "MatchingAlgorithm": MatchingAlgorithms.CHOICES,
        # COI severity levels
        "COISeverityLevel": COISeverityLevels.CHOICES,
        # COI type codes are shared by the ConflictOfInterest.coi_type field and
        # the three CallCOIConfiguration rule lists;
        "CoiTypeEnum": COITypes.CHOICES,
        # All four CallProposalFieldConfig columns and the per-field metadata
        # carry the same three states; name the set once so drf-spectacular
        # does not emit one enum per column.
        "ProposalFieldStateEnum": ProposalFieldStates.CHOICES,
        # CallWorkflowStep.step and Proposal.workflow_step share this choice
        # set; name it once so drf-spectacular doesn't emit two enums for it.
        "StepEnum": WORKFLOW_STEPS_CHOICES,
        # Assignment batch and item statuses
        "AssignmentBatchStatus": AssignmentBatchStatuses.CHOICES,
        "AssignmentItemStatus": AssignmentItemStatuses.CHOICES,
        "AssignmentSource": AssignmentSources.CHOICES,
        "IssueStatusType": ISSUE_STATUS_TYPE_CHOICES,
        "ObservableObjectTypeEnum": ObservableObjectType.choices(),
        "GlauthGroupKind": [
            "project",
            "resource_role",
            "resource_project_role",
            "personal",
        ],
        # GLAuth uid_source and gid_source share the same choice set; give it a
        # single enum name so drf-spectacular doesn't emit clashing
        # Uid/GidSourceEnum names for the identical choices.
        "PosixIdSourceEnum": ["pool", "user_attribute"],
        "GenderEnum": GENDER_CHOICES,
        "InjectionSeverityEnum": SeverityLevel.choices(),
        "FeedbackCategoryEnum": FeedbackCategory.choices,
        "GrowthPeriodEnum": ["weekly", "monthly"],
        "PolicyPeriodEnum": (
            (1, "Total"),
            (2, "1 month"),
            (3, "3 month"),
            (4, "12 month"),
        ),
        "UserAttributeEnum": USER_ATTRIBUTE_CHOICES,
        # Shared by the two DEFAULT_PROPOSAL_*_FIELDS Constance settings.
        "ProposalConfigurableFieldEnum": PROPOSAL_CONFIGURABLE_FIELD_CHOICES,
        "OfferingTypeEnum": OFFERING_TYPE_CHOICES,
        "OnboardingValidationEnum": ONBOARDING_VALIDATION_CHOICES,
        "NotifySystemEnum": NOTIFY_SYSTEM_CHOICES,
        # Protocol fields - avoid collision between Pool/Listener (TCP/UDP) and SecurityGroupRule (tcp/udp/icmp)
        # Pool and Listener share the same TCP/UDP choices - use single enum name to avoid duplication
        # Lazy import path (no waldur_openstack.models at settings load — AppRegistryNotReady)
        "LoadBalancerProtocolEnum": "waldur_openstack.models.PROTOCOL_CHOICES",
        # SecurityGroupRule.protocol is free-form: "tcp", "udp", "icmp", "" or any
        # IANA protocol number 0-255 — too large for an enum.
        # Marketplace attribute type (string, integer, boolean, choice, list, etc.)
        "AttributeTypeEnum": ATTRIBUTE_TYPES,
        # Three distinct ``direction`` enum sources need explicit names so
        # drf-spectacular doesn't fall back to opaque hash-suffixed
        # ``DirectionXxxEnum`` names that change on every schema regen:
        #  - SecurityGroupRule field (ingress / egress)
        #  - NetworkRBACPolicy filter param (outbound / inbound / all)
        #  - NetworkRBACPolicy serializer field (outbound / inbound)
        "SecurityGroupRuleDirectionEnum": ["ingress", "egress"],
        "RbacPolicyDirectionFilterEnum": (
            ("outbound", "Outbound"),
            ("inbound", "Inbound"),
            ("all", "All"),
        ),
        "RbacPolicyDirectionEnum": ["outbound", "inbound"],
    },
    "VERSION": None,
}
