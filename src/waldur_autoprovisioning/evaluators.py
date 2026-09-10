from dataclasses import asdict
from typing import Any

from waldur_autoprovisioning.models import Rule
from waldur_autoprovisioning.reconciliation import resolve_customer
from waldur_core.core.models import User


def compute_test_match(rule: Rule, user: User) -> dict[str, Any]:
    """Read-only dry-run of the autoprovisioning pipeline for a single rule+user.

    Returns a payload matching ``RuleTestMatchResponseSerializer``: top-line
    ``would_provision`` verdict, ``block_reason``, per-filter outcomes, customer
    lookup result (when the rule uses ``use_user_organization_as_customer_name``)
    and a project-name preview. No database writes are performed.
    """
    eval_result = Rule.evaluate_for_user(rule, user)

    would_provision = eval_result.matched
    block_reason = ""
    resolution = None

    if not eval_result.matched:
        block_reason = "Rule filters do not match user"
    else:
        # Shared with the provisioning handler and reconciliation, so the dry-run
        # verdict cannot drift from what actually happens at login.
        resolution = resolve_customer(rule, user)
        if resolution.customer is None:
            would_provision = False
            block_reason = resolution.block_reason

    return {
        "would_provision": would_provision,
        "block_reason": block_reason,
        "user_username": user.username,
        "user_email": user.email or "",
        "user_organization": user.organization or "",
        "user_registration_method": user.registration_method or "",
        "user_identity_source": user.identity_source or "",
        "user_affiliations": list(user.affiliations or []),
        "user_claims": {
            claim: Rule._get_user_claim_values(user, claim)
            for claim in (rule.user_claims or {})
        },
        "user_is_protected": user.should_protect_user_details,
        "filter_results": [asdict(fr) for fr in eval_result.filter_results],
        "customer_lookup_performed": bool(resolution and resolution.lookup_performed),
        "customer_candidates": list(resolution.candidates) if resolution else [],
        "customer_lookup_ambiguous": bool(resolution and resolution.ambiguous),
        "resolved_project_name": (
            rule.resolve_project_name(user)
            if would_provision and rule.create_project
            else None
        ),
    }
