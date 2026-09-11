from dataclasses import asdict
from typing import Any

from waldur_autoprovisioning.models import Rule
from waldur_autoprovisioning.reconciliation import resolve_customer
from waldur_core.core.models import _CLAIM_FALLBACK_USER_FIELDS, User


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
        "unconfigured_claims": _get_unconfigured_claims(rule),
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


def _get_unconfigured_claims(rule: Rule) -> list[str]:
    """Claims the rule matches on that no identity provider actually passes through.

    An empty user value in the dry-run reads the same whether the provider sent
    a different value or never sent the claim at all — but the fixes are
    opposite: adjust the rule, versus add the claim to the provider's extra
    fields. This tells the two apart.

    Imported inside the function: ``waldur_auth_social`` imports this app (for
    ``matches_autoprovisioning_rule``), so a module-level import would be a
    cycle. ``waldur_auth_social`` is also an optional extension, so a missing
    app must not break the dry-run.
    """
    claims = list(rule.user_claims or {})
    if not claims:
        return []

    try:
        from waldur_auth_social.models import IdentityProvider
    except ImportError:  # pragma: no cover - extension not installed
        return []

    passed_through = set(_CLAIM_FALLBACK_USER_FIELDS)
    for provider in IdentityProvider.objects.filter(is_active=True):
        passed_through.update((provider.extra_fields or "").split())
        # A claim mapped onto a profile field arrives that way instead.
        for mapped in (provider.attribute_mapping or {}).values():
            passed_through.update(str(mapped).split())

    return [claim for claim in claims if claim not in passed_through]
