"""What an agent configuration import may also change, when the user opts in.

The offering's own components (the fields the file holds that the offering
component holds too) and its SLURM periodic usage policy (the block's
policy_settings). Neither is part of the agent configuration, so each goes
through the path that changes it anywhere else, with its validation,
permission and side effects.
"""

from __future__ import annotations

import copy
from typing import Any

from django.db import transaction
from django.urls import reverse
from django.utils.translation import gettext_lazy as _
from rest_framework import exceptions, serializers
from rest_framework.exceptions import PermissionDenied, ValidationError

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory
from waldur_core.structure import permissions as structure_permissions
from waldur_mastermind.marketplace import (
    agent_configuration as site_agent_configuration,
)
from waldur_mastermind.marketplace import dependents
from waldur_mastermind.marketplace import models as marketplace_models
from waldur_mastermind.marketplace import serializers as marketplace_serializers
from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.enums import BillingTypes
from waldur_mastermind.marketplace_site_agent import agent_import, config_builder
from waldur_mastermind.policy import models as policy_models
from waldur_mastermind.policy import policy_events
from waldur_mastermind.policy import serializers as policy_serializers

# The agent's accounting type, as the offering component's billing type: the
# inverse of config_builder.build_backend_components.
BILLING_TYPES = {
    "usage": BillingTypes.USAGE,
    "one": BillingTypes.ONE_TIME,
    "limit": BillingTypes.LIMIT,
}

# The file's component field, as the offering component's.
COMPONENT_FIELDS = {
    "measured_unit": "measured_unit",
    "unit_factor": "unit_factor",
    "accounting_type": "billing_type",
    "label": "name",
    "limit": "limit_amount",
}

# Who may change an offering's components: as update_offering_component.
can_update_components = permission_factory(
    PermissionEnum.UPDATE_OFFERING_COMPONENTS,
    ["*", "customer", "customer.serviceprovider"],
    binding_on_path=True,
)

# What a policy created by an import starts with, beyond policy_settings:
# the least a policy does, for every organization, as the policy factory and
# the API examples set it.
NEW_POLICY_DEFAULTS = {
    "apply_to_all": True,
    "component_limits_set": [],
}

# What a SLURM periodic usage policy can do when it fires, as the offering's
# policy form names it.
ACTION_LABELS = {
    "notify_organization_owners": _("Notify organization owners"),
    "notify_external_user": _("Notify external user"),
    "block_creation_of_new_resources": _("Block creation of new resources"),
    "request_slurm_resource_downscaling": _(
        "Request SLURM resource downscaling (slowdown QoS)"
    ),
    "request_slurm_resource_pausing": _("Request SLURM resource pausing (blocked QoS)"),
}


def action_choices() -> list[dict[str, str]]:
    available = policy_models.SlurmPeriodicUsagePolicy.available_actions
    ordered = [name for name in ACTION_LABELS if name in available] + sorted(
        name for name in available if name not in ACTION_LABELS
    )
    return [
        {"value": name, "label": str(ACTION_LABELS.get(name, name))} for name in ordered
    ]


def _action_names(actions: str | None) -> list[str]:
    return [name for name in (actions or "").split(",") if name]


def _component_value(field: str, value: Any) -> Any:
    if field == "accounting_type":
        return BILLING_TYPES.get(value)
    return value


def _component_refusal(request, component, rows: list[dict[str, Any]]):
    """Why these fields of a component cannot be set from the file, or None.

    The checks update_offering_component makes: its serializer, and what
    other apps built on the component. A billing type change is refused, as
    removing a component is, once the offering has resources, or while a plan
    prices the component.
    """
    data = {}
    for row in rows:
        value = _component_value(row["field"], row["imported"])
        if value is None and row["field"] == "accounting_type":
            return _("Not an accounting type the agent knows.")
        data[COMPONENT_FIELDS[row["field"]]] = value
    if "billing_type" in data and data["billing_type"] != component.billing_type:
        offering = component.offering
        if marketplace_models.Resource.objects.filter(offering=offering).exists():
            return _(
                "The offering has resources: their billing would change under them."
            )
        if (
            marketplace_models.PlanComponent.objects.filter(component=component)
            .exclude(price=0)
            .exists()
        ):
            return _("A plan prices this component as it is billed now.")
    serializer = marketplace_serializers.UpdateOfferingComponent(
        instance=component,
        data=data,
        partial=True,
        context={"request": request},
    )
    if not serializer.is_valid():
        return " ".join(
            str(message)
            for messages in serializer.errors.values()
            for message in (messages if isinstance(messages, list) else [messages])
        )
    candidate = copy.copy(component)
    for name, value in serializer.validated_data.items():
        setattr(candidate, name, value)
    try:
        dependents.guard(dependents.Changes.COMPONENT_CHANGED, candidate, request.user)
    except dependents.DependentsConflict as error:
        return str(error.detail)
    return None


def annotate_component_differences(request, offering, differences) -> None:
    """Say of each difference whether it can be applied, and why not."""
    components = {component.type: component for component in offering.components.all()}
    owns = marketplace_utils.offering_owns_pricing(offering)
    for row in differences:
        component = components.get(row["component"])
        if component is None:
            reason = _(
                "The offering has no %(type)s component: add it with its price first."
            ) % {"type": row["component"]}
        elif not owns:
            reason = _("The components of a child offering belong to its parent.")
        else:
            reason = _component_refusal(request, component, [row])
        row["applicable"] = reason is None
        row["reason"] = None if reason is None else str(reason)


def chosen_component_differences(differences, chosen) -> tuple[list, dict]:
    """The differences the user opted into, and the refusals, by component.field."""
    rows = {(row["component"], row["field"]): row for row in differences}
    selected, errors = [], {}
    for item in chosen:
        key = (item["component"], item["field"])
        row = rows.get(key)
        name = f"{key[0]}.{key[1]}"
        if row is None:
            errors[name] = [_("The file and the offering do not differ here.")]
        elif not row["applicable"]:
            errors[name] = [row["reason"]]
        elif row not in selected:
            selected.append(row)
    return selected, errors


COMPONENTS_PERMISSION_MESSAGE = _("You may not change the offering's components.")
POLICY_PERMISSION_MESSAGE = _(
    "You may not configure the offering's SLURM periodic usage policy."
)


def may_update_components(request, view, offering) -> bool:
    try:
        can_update_components(request, view, offering)
    except exceptions.PermissionDenied:
        return False
    return True


def apply_component_differences(request, offering, rows) -> None:
    """Set the chosen fields on the offering's components, as
    update_offering_component does; refuses as it does."""
    by_component: dict[str, list] = {}
    for row in rows:
        by_component.setdefault(row["component"], []).append(row)
    for component_type, component_rows in by_component.items():
        component = offering.components.get(type=component_type)
        refusal = _component_refusal(request, component, component_rows)
        if refusal is not None:
            raise serializers.ValidationError(
                {"apply_components": {component_type: [refusal]}}
            )
        serializer = marketplace_serializers.UpdateOfferingComponent(
            instance=component,
            data={
                COMPONENT_FIELDS[row["field"]]: _component_value(
                    row["field"], row["imported"]
                )
                for row in component_rows
            },
            partial=True,
            context={"request": request},
        )
        serializer.is_valid(raise_exception=True)
        serializer.save()


def _uses_slurm(configuration: dict[str, Any]) -> bool:
    backends = {
        configuration.get(field)
        for field in config_builder.AGENT_ROLE_FIELDS
        if configuration.get(field)
    }
    # With no role set the generator runs SLURM for every role.
    return not backends or site_agent_configuration.SLURM_BACKEND in backends


def _may_configure_policy(request, offering, policy) -> bool:
    try:
        structure_permissions.is_owner(request, None, obj=policy or offering)
    except exceptions.PermissionDenied:
        return False
    return True


class PolicyImport:
    """The block's policy_settings against the offering's SLURM periodic usage policy."""

    def __init__(
        self,
        request,
        offering,
        settings: dict[str, Any],
        configuration,
        actions: list[str] | None = None,
    ):
        self.request = request
        self.offering = offering
        self.settings = dict(settings)
        # The names chosen, once each; none chosen is the same as not given.
        self.actions = list(dict.fromkeys(actions or [])) or None
        self.policy = policy_models.SlurmPeriodicUsagePolicy.objects.filter(
            scope=offering
        ).first()
        self.permitted = _may_configure_policy(request, offering, self.policy)
        self.reason = None
        if not _uses_slurm(configuration):
            self.reason = _("The offering's roles do not use SLURM.")
        elif not self.permitted:
            self.reason = POLICY_PERMISSION_MESSAGE

    def describe(self) -> dict[str, Any]:
        changes = []
        for field in agent_import.POLICY_FIELDS:
            if field not in self.settings:
                continue
            current = getattr(self.policy, field) if self.policy else None
            if current != self.settings[field]:
                changes.append(
                    {
                        "field": field,
                        "current": current,
                        "imported": self.settings[field],
                    }
                )
        if self.actions is not None:
            current_actions = (
                _action_names(self.policy.actions) if self.policy else None
            )
            if current_actions is None or set(current_actions) != set(self.actions):
                changes.append(
                    {
                        "field": "actions",
                        "current": current_actions,
                        "imported": self.actions,
                    }
                )
        return {
            "exists": self.policy is not None,
            "needs_actions": self.policy is None,
            "action_choices": action_choices(),
            "changes": changes,
            "applicable": self.reason is None,
            "reason": None if self.reason is None else str(self.reason),
        }

    def serializer(self):
        context = {"request": self.request}
        data = dict(self.settings)
        if self.actions is not None:
            # The policy holds its actions as one comma-separated text.
            data["actions"] = ",".join(self.actions)
        if self.policy is not None:
            return policy_serializers.SlurmPeriodicUsagePolicySerializer(
                self.policy, data=data, partial=True, context=context
            )
        scope = self.request.build_absolute_uri(
            reverse(
                "marketplace-provider-offering-detail",
                kwargs={"uuid": self.offering.uuid.hex},
            )
        )
        return policy_serializers.SlurmPeriodicUsagePolicySerializer(
            data={**NEW_POLICY_DEFAULTS, **data, "scope": scope},
            context=context,
        )

    def errors(self) -> dict[str, Any]:
        """Why the policy cannot be imported; empty when it can."""
        if self.reason is not None:
            return {"non_field_errors": [str(self.reason)]}
        if self.policy is None and self.actions is None:
            # Creating the policy: what it does is the user's choice.
            return {"actions": [str(_("Choose what the policy does when it fires."))]}
        serializer = self.serializer()
        if serializer.is_valid():
            return {}
        return dict(serializer.errors)

    def apply(self) -> None:
        serializer = self.serializer()
        if not serializer.is_valid():
            raise serializers.ValidationError({"policy": serializer.errors})
        # Logged as the policy's own endpoints log it.
        policy_events.save_with_events(self.request.user, serializer)


def run_import(request, view, offering, data: dict[str, Any]) -> dict[str, Any]:
    """Run an import for the offering endpoint, as validated by its request
    serializer; returns what its response serializer renders.

    The agent configuration is saved as update_integration saves it, through
    OfferingIntegrationUpdateSerializer; the opted-in component fields and
    policy go with it, all or nothing.
    """
    imported = agent_import.import_configuration(
        offering,
        data["configuration"],
        data.get("offering_index"),
        data.get("values"),
    )
    current = agent_import.sanitized(offering.agent_configuration)
    update = marketplace_serializers.OfferingIntegrationUpdateSerializer(
        offering,
        data={"agent_configuration": imported.agent_configuration},
        context=view.get_serializer_context(),
    )
    # A preview shows what is wrong next to what would change, so the
    # configuration is shown as mapped when it is not valid.
    if update.is_valid():
        proposed = update.validated_data["agent_configuration"]
        errors = {}
    else:
        proposed = imported.agent_configuration
        errors = agent_import.configuration_errors(update.errors)
    configuration_errors = dict(errors)

    annotate_component_differences(request, offering, imported.component_differences)
    # A missing permission for an opt-in is a 403 when applying; a preview
    # reports it next to the rest, so the user can drop the opt-in.
    refused = []
    chosen = data.get("apply_components") or []
    components, component_errors = chosen_component_differences(
        imported.component_differences, chosen
    )
    if chosen and not may_update_components(request, view, offering):
        refused.append(COMPONENTS_PERMISSION_MESSAGE)
        component_errors = {"non_field_errors": [str(COMPONENTS_PERMISSION_MESSAGE)]}
    if component_errors:
        errors["apply_components"] = component_errors

    policy = None
    if imported.policy_settings is not None:
        policy = PolicyImport(
            request,
            offering,
            imported.policy_settings,
            proposed,
            data.get("policy_actions"),
        )
    if data["apply_policy"]:
        if policy is None:
            errors["policy"] = {
                "non_field_errors": [_("The offering block has no policy_settings.")]
            }
        else:
            if not policy.permitted:
                refused.append(POLICY_PERMISSION_MESSAGE)
                errors["policy"] = {
                    "non_field_errors": [str(POLICY_PERMISSION_MESSAGE)]
                }
            else:
                policy_errors = policy.errors()
                if policy_errors:
                    errors["policy"] = policy_errors
    if refused and not data["dry_run"]:
        raise PermissionDenied(" ".join(str(message) for message in refused))

    # Described before anything is saved: saving updates the policy in place.
    policy_description = policy.describe() if policy is not None else None
    if data["dry_run"]:
        # What saving would check, short of saving; a refusal is shown
        # with the rest of the preview, and applying is the 403.
        try:
            marketplace_serializers.validate_agent_configuration_trust(
                request, offering, None, offering.agent_configuration, proposed
            )
        except PermissionDenied as error:
            errors.setdefault("non_field_errors", []).append(str(error.detail))
    else:
        if errors:
            refusal = {
                key: value
                for key, value in errors.items()
                if key in ("apply_components", "policy")
            }
            if configuration_errors:
                refusal["agent_configuration"] = configuration_errors
            raise ValidationError(refusal)
        # All or nothing: a refusal of any part undoes the others.
        with transaction.atomic():
            update.save()
            apply_component_differences(request, offering, components)
            if data["apply_policy"]:
                policy.apply()
    result = {
        "offerings": imported.offerings,
        "selected_index": imported.selected_index,
        "agent_configuration": proposed,
        "current": current,
        "changes": agent_import.configuration_changes(current, proposed),
        "dropped_secrets": imported.dropped_secrets,
        "unresolved": imported.unresolved,
        "ignored": imported.ignored,
        "component_differences": imported.component_differences,
        "policy": policy_description,
        "errors": errors,
        "applied": not data["dry_run"],
    }
    return result
