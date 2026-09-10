"""Reconcile the roles auto-provisioning rules assert for a user.

Rules used to be grant-only: a matching user gained a role and nothing ever took
it away, which put them at odds with inbound SCIM, where a group membership PUT
revokes what the identity provider no longer asserts. This module closes that
gap for rules that opt in via ``Rule.revoke_when_unmatched``.

The safety property that makes automatic revocation acceptable is
``UserRole.source``: reconciliation only ever revokes rows tagged with the
``grant_source`` of a rule it is currently evaluating. A grant made by a person
carries an empty source and is untouchable here, even when it happens to name
the same (user, scope, role) triple as a rule.

Naming note: :func:`reconcile_autoprovisioned_roles` is deliberately not called
``reconcile_user_roles`` — ``waldur_core.permissions.tasks`` already owns that
name for reconciliation against ``RoleAvailability``, which is a different thing.
"""

import logging
from dataclasses import dataclass

from django.contrib.contenttypes.models import ContentType
from rest_framework.exceptions import ValidationError

from waldur_autoprovisioning.models import Rule
from waldur_core.core.models import User
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import check_grant_policy
from waldur_core.structure.models import Customer, Project

logger = logging.getLogger(__name__)

GRANT_REASON = "Auto-provisioning rule '{name}' matched the user"
REVOKE_REASON = "Auto-provisioning rule '{name}' no longer matches the user"


@dataclass(frozen=True)
class CustomerResolution:
    """Outcome of working out which organization a rule applies to for a user."""

    customer: Customer | None
    block_reason: str = ""
    candidates: tuple = ()
    ambiguous: bool = False
    lookup_performed: bool = False


def resolve_customer(rule: Rule, user: User) -> CustomerResolution:
    """Which organization does ``rule`` target for ``user``?

    Either the rule's own customer, or — with
    ``use_user_organization_as_customer_name`` — the organization whose name
    equals the user's ``organization`` claim. Shared by the provisioning
    handler, the dry-run evaluator and reconciliation so all three agree on
    the verdict and on the wording of the reason.
    """
    if not rule.use_user_organization_as_customer_name:
        if not rule.customer:
            return CustomerResolution(
                None,
                block_reason=(
                    "Rule has no organization configured and "
                    "use_user_organization_as_customer_name is disabled."
                ),
            )
        return CustomerResolution(rule.customer)

    if not user.should_protect_user_details:
        return CustomerResolution(
            None,
            block_reason=(
                "User registration method is not listed in "
                "PROTECT_USER_DETAILS_FOR_REGISTRATION_METHODS, so the user's "
                "organization claim is not trusted for autoprovisioning."
            ),
            lookup_performed=True,
        )

    if not user.organization:
        return CustomerResolution(
            None,
            block_reason="User has no organization claim from the identity provider.",
            lookup_performed=True,
        )

    candidates = list(Customer.objects.filter(name=user.organization))
    if not candidates:
        return CustomerResolution(
            None,
            block_reason=f"No organization found with name='{user.organization}'.",
            lookup_performed=True,
        )
    if len(candidates) > 1:
        return CustomerResolution(
            None,
            block_reason=(
                f"Multiple organizations ({len(candidates)}) share "
                f"name='{user.organization}'; cannot resolve unambiguously."
            ),
            candidates=tuple(candidates),
            ambiguous=True,
            lookup_performed=True,
        )
    return CustomerResolution(
        candidates[0], candidates=tuple(candidates), lookup_performed=True
    )


def _scope_key(scope, role_id: int) -> tuple[int, int, int]:
    content_type = ContentType.objects.get_for_model(type(scope))
    return (content_type.id, scope.id, role_id)


def get_asserted_grants(user: User, rules=None) -> dict:
    """The ``(scope, role)`` pairs the currently-matching rules assert for a user.

    Projects are only *considered*, never created: creation (and the marketplace
    order that may accompany it) stays in the provisioning handler, which runs
    once when the account appears. Reconciliation runs on every login and must
    stay side-effect free apart from the role writes themselves.
    """
    if rules is None:
        rules = Rule.objects.all()

    asserted: dict[tuple[int, int, int], tuple] = {}
    for rule in rules:
        if not Rule.evaluate_for_user(rule, user).matched:
            continue
        resolution = resolve_customer(rule, user)
        customer = resolution.customer
        if customer is None:
            logger.debug(
                "Auto-provisioning rule '%s' matches user %s but resolves no "
                "organization: %s",
                rule.name,
                user.username,
                resolution.block_reason,
            )
            continue

        if rule.customer_role_id:
            asserted[_scope_key(customer, rule.customer_role_id)] = (
                customer,
                rule.customer_role,
                rule,
            )

        if rule.create_project:
            # The API requires a project role, but rules created by fixtures or
            # by hand may omit it; ADMIN has always been the implicit default.
            project_role = rule.project_role or ProjectRole.ADMIN
            project = Project.available_objects.filter(
                name=rule.resolve_project_name(user), customer=customer
            ).first()
            if project is not None:
                asserted[_scope_key(project, project_role.id)] = (
                    project,
                    project_role,
                    rule,
                )

    return asserted


def reconcile_autoprovisioned_roles(user: User, dry_run: bool = False) -> dict:
    """Bring a user's rule-issued roles in line with what the rules now assert.

    Returns ``{"granted": [...], "revoked": [...]}`` of human-readable labels,
    for the management command and for tests.
    """
    rules = list(
        Rule.objects.select_related("customer", "customer_role", "project_role")
    )
    if not rules:
        return {"granted": [], "revoked": []}

    asserted = get_asserted_grants(user, rules)
    granted: list[str] = []
    revoked: list[str] = []

    existing = {
        (role.content_type_id, role.object_id, role.role_id): role
        for role in UserRole.objects.filter(user=user, is_active=True)
    }
    # Grants this rule set has issued before and later revoked. A user who
    # regains a claim gets the same row back rather than a second one:
    # ``(user, scope, role)`` is treated as unique while active elsewhere in the
    # codebase (``validate_role_grant`` refuses a duplicate), and User follows
    # the same reactivate-in-place convention when it regains a role.
    revoked_by_us = {}
    sources = {rule.grant_source for rule in rules}
    for row in UserRole.objects.filter(
        user=user, is_active=False, source__in=sources
    ).order_by("created"):
        # Latest wins for rows predating this behaviour.
        revoked_by_us[(row.content_type_id, row.object_id, row.role_id)] = row

    for key, (scope, role, rule) in asserted.items():
        if key in existing:
            continue
        label = f"{role.name} on {scope}"
        if dry_run:
            granted.append(label)
            continue

        reason = GRANT_REASON.format(name=rule.name)
        previous = revoked_by_us.get(key)
        if previous is not None and previous.source == rule.grant_source:
            # restore() does not re-check the org-scoping policy, so do it here:
            # the role may have been concealed for this organization since the
            # grant was made, and coming back must not smuggle it past that.
            try:
                check_grant_policy(scope, role)
            except ValidationError as exc:
                logger.warning(
                    "Skipped restoring role %s for user %s on %s: %s",
                    role,
                    user.uuid.hex,
                    scope,
                    exc,
                )
                continue
            previous.restore(reason=reason)
            granted.append(label)
            continue

        # add_user_or_skip, not add_user: a role concealed for this organization
        # or unavailable for the scope is skipped and logged, so one rejected
        # grant does not abort the rest of the reconciliation.
        user_role = scope.add_user_or_skip(
            user,
            role,
            source=rule.grant_source,
            reason=reason,
        )
        if user_role is not None:
            granted.append(label)

    # Only rules that opted in may revoke, and only rows they themselves issued:
    # a grant made by a person carries source="" and is never a candidate.
    revocable = {
        rule.grant_source: rule for rule in rules if rule.revoke_when_unmatched
    }
    if revocable:
        stale = UserRole.objects.filter(
            user=user, is_active=True, source__in=revocable.keys()
        ).select_related("role")
        for user_role in stale:
            key = (user_role.content_type_id, user_role.object_id, user_role.role_id)
            if key in asserted:
                continue
            revoked.append(f"{user_role.role.name} on {user_role.scope}")
            if not dry_run:
                # Name the rule in the audit event: "a system revoked this" is
                # not an answer anyone can act on.
                user_role.revoke(
                    reason=REVOKE_REASON.format(name=revocable[user_role.source].name)
                )

    if (granted or revoked) and not dry_run:
        logger.info(
            "Auto-provisioning reconciliation for user %s: granted %s, revoked %s.",
            user.username,
            granted or "nothing",
            revoked or "nothing",
        )
    return {"granted": granted, "revoked": revoked}
