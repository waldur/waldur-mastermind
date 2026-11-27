"""Policy preview helpers used by ``rebill_historical_usage``."""

from typing import Any

from waldur_mastermind.marketplace import utils as marketplace_utils
from waldur_mastermind.marketplace.models import Resource
from waldur_mastermind.policy import models as policy_models
from waldur_mastermind.policy.policy_actions import (
    POLICY_ACTIONS,
    _filter_resources_by_scope,
    _resources_locked_by_other_policies,
)

_POLICY_SCOPE_LABELS = {
    policy_models.ProjectEstimatedCostPolicy: "Project",
    policy_models.CustomerEstimatedCostPolicy: "Customer",
    policy_models.OfferingEstimatedCostPolicy: "Offering",
}


def get_policy_scope_label(policy) -> str:
    """Return human-readable scope label ('Project', 'Customer', 'Offering') for a cost policy."""
    return _POLICY_SCOPE_LABELS[type(policy)]


def snapshot_estimated_cost_policies(resource) -> list[tuple[Any, bool]]:
    """Return ``[(policy, has_fired)]`` for every cost policy covering ``resource``."""
    project = resource.project
    policies = (
        list(policy_models.ProjectEstimatedCostPolicy.objects.filter(scope=project))
        + list(
            policy_models.CustomerEstimatedCostPolicy.objects.filter(
                scope=project.customer
            )
        )
        + list(
            policy_models.OfferingEstimatedCostPolicy.objects.filter(
                scope=resource.offering
            )
        )
    )
    return [(policy, policy.has_fired) for policy in policies]


def preview_policy_action_resources(
    policy, action_name: str, meta: dict[str, Any], now_triggered: bool
) -> tuple[bool, list[Resource] | None]:
    """Preview resources affected by ``action_name`` on ``policy``.

    Returns ``(has_reset, affected)`` where ``has_reset=False`` signals that
    the action has no reset method on a fired -> clear transition.
    """
    if not now_triggered and POLICY_ACTIONS[action_name].reset_method is None:
        return False, None

    candidates = Resource.objects.filter(**meta["filter_kwargs"]).exclude(
        state__in=meta["exclude_states"]
    )
    affected = _filter_resources_by_scope(candidates, policy)
    if affected is None:
        return True, None

    field_name = meta["field_name"]
    if field_name is not None:
        # Mirror _apply_generic_action exactly: a resource already at the target
        # value is a no-op the real action skips, and (only when clearing) one
        # another still-firing policy wants kept set is left alone too.
        affected = affected.exclude(**{field_name: now_triggered})
        if not now_triggered:
            locked = _resources_locked_by_other_policies(policy, field_name)
            if locked:
                affected = affected.exclude(pk__in=locked)
        if field_name == "paused" and not now_triggered:
            # ...and one its project's grace period keeps paused.
            affected = [
                r for r in affected if not marketplace_utils.is_held_by_project_grace(r)
            ]

    return True, list(affected)
