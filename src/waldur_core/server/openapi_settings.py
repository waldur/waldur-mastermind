from django.db import models

from waldur_core.checklist import enums as checklist_enums
from waldur_core.checklist.enums import ChecklistTypes
from waldur_core.core.enums import CoreStates, ReviewStates
from waldur_core.onboarding.enums import VerificationStatus
from waldur_core.permissions.enums import TYPE_KEYS
from waldur_core.structure.enums import OECD_FOS_2007_LABELS
from waldur_core.users.enums import InvitationState
from waldur_mastermind.common.enums import Units
from waldur_mastermind.invoices.enums import Periods
from waldur_mastermind.marketplace.enums import (
    BillingTypes,
    IntegrationStatusStates,
    MaintenanceState,
    OfferingStates,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
    RobotAccountStates,
    ServiceAccountState,
)
from waldur_mastermind.marketplace_script.enums import DryRunStates
from waldur_mastermind.marketplace_site_agent.enums import AgentServiceState
from waldur_mastermind.proposal.enums import (
    CallStates,
    ProposalStates,
    RequestedOfferingStates,
    RoundStatuses,
)
from waldur_rancher.enums import (
    CatalogScopeType,
    KeycloakUserGroupMembershipState,
    NodeRole,
    RoleScopeType,
    TemplateQuestionType,
)


def transform_enum_overrides(overrides):
    result = {}
    for name, source in overrides.items():
        if isinstance(source, type) and issubclass(source, models.IntegerChoices):
            result[name] = [(label, label) for label in source.labels]
        elif isinstance(source, type) and issubclass(source, models.TextChoices):
            result[name] = source.choices
        elif isinstance(source, list):
            if not all(isinstance(item, str) for item in source):
                raise TypeError(
                    f"Override '{name}' must be a list of strings, got list containing {type(source[0]) if source else 'empty'}"
                )
            result[name] = source
        else:
            raise TypeError(
                f"Override '{name}' must be a list of strings, IntegerChoices or TextChoices subclass. Got {type(source)}"
            )
    return result


SPECTACULAR_SETTINGS = {
    "TITLE": "Waldur Mastermind API",
    "SWAGGER_UI_DIST": "SIDECAR",
    "SWAGGER_UI_FAVICON_HREF": "SIDECAR",
    "POSTPROCESSING_HOOKS": [
        "drf_spectacular.hooks.postprocess_schema_enums",
        "waldur_core.core.openapi_hooks.add_polymorphic_attributes_schema",
        "waldur_core.core.openapi_hooks.extract_enums_to_components",
        "waldur_core.core.openapi_hooks.validate_no_numeric_enums",
        "waldur_core.core.openapi_hooks.postprocess_strip_description",
        "waldur_core.core.openapi_hooks.postprocess_drop_inherited_descriptions",
        "waldur_core.core.openapi_hooks.refactor_pagination_parameters",
        "waldur_core.core.openapi_hooks.transform_paginated_arrays",
        "waldur_core.core.openapi_hooks.make_fields_optional",
        "waldur_core.core.openapi_hooks.remove_waldur_cookie_auth",
        "waldur_core.core.openapi_hooks.preprocess_request_bodies",
        "waldur_core.core.openapi_hooks.add_result_count_header",
    ],
    "DEFAULT_GENERATOR_CLASS": "waldur_core.core.openapi_generators.WaldurSchemaGenerator",
    "ENUM_GENERATE_CHOICE_DESCRIPTION": False,
    "COMPONENT_SPLIT_REQUEST": True,
    "SCHEMA_PATH_PREFIX": "/api",
    "ENUM_NAME_OVERRIDES": transform_enum_overrides(
        {
            "RoleType": TYPE_KEYS,
            "InvitationState": InvitationState,
            "BillingUnit": Units,
            "CoreStates": CoreStates,
            "OfferingState": OfferingStates,
            "OrderState": OrderStates,
            "ResourceState": ResourceStates,
            "CallStates": CallStates,
            "ProposalStates": ProposalStates,
            "RequestedOfferingStates": RequestedOfferingStates,
            "RoundStatus": RoundStatuses,
            "RequestTypes": OrderTypes,
            "RancherTemplateQuestionType": TemplateQuestionType,
            "RancherRoleScopeType": RoleScopeType,
            "KeycloakUserGroupMembershipState": KeycloakUserGroupMembershipState,
            "RancherCatalogScopeType": CatalogScopeType,
            "RobotAccountStates": RobotAccountStates,
            "ChecklistOperators": checklist_enums.Operators,
            "ServiceAccountState": ServiceAccountState,
            "OfferingUserState": OfferingUserStates,
            "OnboardingVerificationStatus": VerificationStatus,
            "AgentServiceState": AgentServiceState,
            "IntegrationStatusStates": IntegrationStatusStates,
            "ChecklistType": ChecklistTypes,
            "RancherNodeRole": NodeRole,
            "DryRunState": DryRunStates,
            "ReviewState": ReviewStates,
            "MaintenanceState": MaintenanceState,
            "OecdFos2007Code": OECD_FOS_2007_LABELS,
            "BillingType": BillingTypes,
            "PeriodName": Periods,
        }
    ),
    "VERSION": None,
}
