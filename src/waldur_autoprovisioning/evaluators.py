from dataclasses import asdict
from typing import Any

from waldur_autoprovisioning.models import Rule
from waldur_core.core.models import User
from waldur_core.structure.models import Customer


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
    customer_lookup_performed = False
    customer_lookup_ambiguous = False
    customer_candidates: list[Customer] = []

    if not eval_result.matched:
        block_reason = "Rule filters do not match user"

    if eval_result.matched and rule.use_user_organization_as_customer_name:
        customer_lookup_performed = True
        if not user.should_protect_user_details:
            would_provision = False
            block_reason = (
                "User registration method is not listed in "
                "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS, so the user's "
                "organization claim is not trusted for autoprovisioning."
            )
        elif not user.organization:
            would_provision = False
            block_reason = "User has no organization claim from the identity provider."
        else:
            customer_candidates = list(Customer.objects.filter(name=user.organization))
            if len(customer_candidates) == 0:
                would_provision = False
                block_reason = f"No organization found with name='{user.organization}'."
            elif len(customer_candidates) > 1:
                would_provision = False
                customer_lookup_ambiguous = True
                block_reason = (
                    f"Multiple organizations ({len(customer_candidates)}) share "
                    f"name='{user.organization}'; cannot resolve unambiguously."
                )

    if eval_result.matched and not rule.use_user_organization_as_customer_name:
        if not rule.customer:
            would_provision = False
            block_reason = (
                "Rule has no organization configured and "
                "use_user_organization_as_customer_name is disabled."
            )

    return {
        "would_provision": would_provision,
        "block_reason": block_reason,
        "user_username": user.username,
        "user_email": user.email or "",
        "user_organization": user.organization or "",
        "user_registration_method": user.registration_method or "",
        "user_identity_source": user.identity_source or "",
        "user_affiliations": list(user.affiliations or []),
        "user_is_protected": user.should_protect_user_details,
        "filter_results": [asdict(fr) for fr in eval_result.filter_results],
        "customer_lookup_performed": customer_lookup_performed,
        "customer_candidates": customer_candidates,
        "customer_lookup_ambiguous": customer_lookup_ambiguous,
        "resolved_project_name": (
            rule.resolve_project_name(user) if would_provision else None
        ),
    }
