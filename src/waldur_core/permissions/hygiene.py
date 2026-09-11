"""Read-only hygiene checks over the role catalogue.

Deployments that predate organization-scoped roles carry hand-made roles, and
nothing surfaces the broken ones. A role can end up malformed without anyone
making a mistake: ``PermissionMixin.get_or_create_role`` accepts a plain string
and turns it into a system role with an arbitrary name, imports do the same with
names taken from foreign data, and role editing validates that a permission
exists but not that it means anything for the role's scope.

Every check here is computed from ``Role``, ``RoleAvailability``,
``CustomerRoleConcealment`` and ``UserRole`` alone — nothing is written, and no
plugin models are imported.
"""

import dataclasses
import re
from typing import Any

from django.contrib.contenttypes.models import ContentType

from waldur_core.permissions import models
from waldur_core.permissions.enums import (
    SCOPE_NAME_PREFIXES,
    SYSTEM_ROLE_SCOPES,
    TYPE_KEY_BY_CT,
    get_permission_scope_types,
)
from waldur_core.permissions.utils import build_org_role_name

ERROR = "error"
WARNING = "warning"
INFO = "info"

SEVERITY_ORDER = (ERROR, WARNING, INFO)

# Severity decides the command's exit code: an error fails the run. Only the
# checks that cannot be legitimately deployment-specific carry it. A name this
# release does not define is a warning, because import_roles marks every role
# in a deployment's own permissions.yaml as a system role, and that deployment
# has no way to add its roles to SYSTEM_ROLE_SCOPES — an error there would be
# permanently non-zero for anyone shipping a role of their own.
CHECK_SEVERITIES: dict[str, str] = {
    "name-not-a-code": ERROR,
    "system-name-unknown": WARNING,
    "system-scope-mismatch": ERROR,
    "clone-name-drift": ERROR,
    "template-without-scope": ERROR,
    "multi-org-binding": ERROR,
    "org-role-unmanaged": WARNING,
    "global-custom-role": WARNING,
    "scope-prefix-mismatch": WARNING,
    "cross-scope-permission": WARNING,
    "label-missing": INFO,
    "label-equals-name": INFO,
}

# Offering catalog roles: their names are chosen by the provider and duplicates
# are allowed by design (see Role.save), so none of the naming checks apply.
CATALOG_SCOPE_MODELS = frozenset({"resource", "resourceproject"})

# A machine code: an upper-case scope prefix followed by one or more
# dot-separated segments. Organization clones add the owner slug as a middle
# segment (CUSTOMER.acme-ltd.OWNER), and the collision suffix (-2) keeps the
# name a code, so both stay valid here.
NAME_CODE_RE = re.compile(r"^[A-Z][A-Z0-9_]*(?:\.[A-Za-z0-9_-]+)+$")

# Trailing "-2", "-3", ... appended by ensure_unique_role_name on a slug clash.
COLLISION_SUFFIX_RE = re.compile(r"-\d+$")


@dataclasses.dataclass(frozen=True)
class Finding:
    check: str
    role_uuid: str
    role_name: str
    role_description: str
    scope_type: str | None
    is_system_role: bool
    message: str
    details: dict[str, Any] = dataclasses.field(default_factory=dict)

    @property
    def severity(self) -> str:
        return CHECK_SEVERITIES[self.check]

    def to_dict(self) -> dict[str, Any]:
        return {
            "check": self.check,
            "severity": self.severity,
            "role_uuid": self.role_uuid,
            "role_name": self.role_name,
            "role_description": self.role_description,
            "scope_type": self.scope_type,
            "is_system_role": self.is_system_role,
            "message": self.message,
            "details": self.details,
        }


def _customer_content_type() -> ContentType:
    return ContentType.objects.get_by_natural_key("structure", "customer")


def _scope_type(role: models.Role) -> str | None:
    """The TYPE_KEYS alias of a role's scope, or None for an unmapped model."""
    content_type = role.content_type
    return TYPE_KEY_BY_CT.get((content_type.app_label, content_type.model))


def _count_organizations(role: models.Role, scope_ids: list[int]) -> int | None:
    """How many distinct organizations hold this role.

    Resolved generically off the scope model: a Customer scope *is* the
    organization, any scope with a ``customer`` foreign key resolves through it,
    and anything else (a call, a proposal) returns None rather than a guess.
    """
    if not scope_ids:
        return 0
    model = role.content_type.model_class()
    if model is None:
        return None
    if model._meta.label_lower == "structure.customer":
        return len(set(scope_ids))
    if not any(field.name == "customer" for field in model._meta.fields):
        return None
    return (
        model.objects.filter(id__in=scope_ids).values("customer_id").distinct().count()
    )


class RoleHygieneChecker:
    """Runs every check over one role, reusing state loaded once per report."""

    def __init__(self, customer_slugs: dict[int, str], customer_content_type_id: int):
        self.customer_slugs = customer_slugs
        self.customer_content_type_id = customer_content_type_id

    def check(self, role: models.Role) -> list[Finding]:
        scope_type = _scope_type(role)
        if role.content_type.model in CATALOG_SCOPE_MODELS:
            return []

        findings: list[Finding] = []
        customer_ids = sorted(
            {
                availability.object_id
                for availability in role.availability.all()
                if availability.content_type_id == self.customer_content_type_id
            }
        )
        has_any_availability = bool(role.availability.all())

        findings += self._check_name(role, scope_type)
        findings += self._check_system_role(role, scope_type)
        findings += self._check_binding(
            role, scope_type, customer_ids, has_any_availability
        )
        findings += self._check_permissions(role, scope_type)
        findings += self._check_label(role, scope_type)
        return findings

    def _finding(self, check, role, scope_type, message, **details) -> Finding:
        return Finding(
            check=check,
            role_uuid=role.uuid.hex,
            role_name=role.name,
            role_description=role.description or "",
            scope_type=scope_type,
            is_system_role=role.is_system_role,
            message=message,
            details=details,
        )

    def _check_name(self, role, scope_type) -> list[Finding]:
        if not NAME_CODE_RE.match(role.name):
            return [
                self._finding(
                    "name-not-a-code",
                    role,
                    scope_type,
                    f"Name {role.name!r} is free-form text rather than a "
                    f"SCOPE.CODE machine code; it is shown as the code column "
                    f"in every role table.",
                )
            ]

        # A name defined by this release makes no scope claim of its own — its
        # scope is checked against SYSTEM_ROLE_SCOPES instead.
        base_name = role.template.name if role.template_id else role.name
        if base_name in SYSTEM_ROLE_SCOPES:
            return []

        prefix = role.name.split(".")[0]
        expected = SCOPE_NAME_PREFIXES.get(scope_type or "")
        if expected and prefix != expected and prefix in SCOPE_NAME_PREFIXES.values():
            return [
                self._finding(
                    "scope-prefix-mismatch",
                    role,
                    scope_type,
                    f"Name claims the {prefix} scope but the role is bound to "
                    f"{scope_type}; expected the {expected} prefix.",
                    actual_prefix=prefix,
                    expected_prefix=expected,
                )
            ]
        return []

    def _check_system_role(self, role, scope_type) -> list[Finding]:
        if not role.is_system_role:
            return []
        canonical = SYSTEM_ROLE_SCOPES.get(role.name)
        if canonical is None:
            return [
                self._finding(
                    "system-name-unknown",
                    role,
                    scope_type,
                    f"Marked as a system role but {role.name!r} is not a system "
                    f"role name this release defines; it was most likely created "
                    f"by passing a label to add_user or by an import.",
                )
            ]
        app_label, model = canonical
        if (role.content_type.app_label, role.content_type.model) != (app_label, model):
            return [
                self._finding(
                    "system-scope-mismatch",
                    role,
                    scope_type,
                    f"System role {role.name} belongs on {app_label}.{model} but "
                    f"is bound to {role.content_type.app_label}."
                    f"{role.content_type.model}.",
                    expected_scope=f"{app_label}.{model}",
                    actual_scope=f"{role.content_type.app_label}.{role.content_type.model}",
                )
            ]
        return []

    def _check_binding(
        self, role, scope_type, customer_ids, has_any_availability
    ) -> list[Finding]:
        findings = []

        if len(customer_ids) > 1:
            findings.append(
                self._finding(
                    "multi-org-binding",
                    role,
                    scope_type,
                    f"Bound to {len(customer_ids)} organizations; the name can "
                    f"only encode one of them.",
                    organization_count=len(customer_ids),
                )
            )

        if role.template_id and not has_any_availability:
            findings.append(
                self._finding(
                    "template-without-scope",
                    role,
                    scope_type,
                    f"Copy of {role.template.name} has no organization binding "
                    f"left, so an organization-private role is grantable "
                    f"everywhere.",
                    template_name=role.template.name,
                )
            )
        elif role.template_id and len(customer_ids) == 1:
            slug = self.customer_slugs.get(customer_ids[0])
            if slug:
                expected = build_org_role_name(role.template, slug)
                actual = COLLISION_SUFFIX_RE.sub("", role.name)
                if actual != expected:
                    findings.append(
                        self._finding(
                            "clone-name-drift",
                            role,
                            scope_type,
                            f"Name no longer matches its template and owner "
                            f"slug; expected {expected}.",
                            expected_name=expected,
                            template_name=role.template.name,
                            customer_slug=slug,
                        )
                    )

        if not role.template_id and len(customer_ids) == 1:
            findings.append(
                self._finding(
                    "org-role-unmanaged",
                    role,
                    scope_type,
                    "Organization-private but not a copy of any role, so its "
                    "name is unmaintained and nothing is concealed for it.",
                )
            )

        if (
            not role.is_system_role
            and not role.template_id
            and not has_any_availability
        ):
            scope_ids = self._scope_ids(role)
            findings.append(
                self._finding(
                    "global-custom-role",
                    role,
                    scope_type,
                    "Custom role with no organization binding: it is offered in "
                    "every organization, and editing or deleting it from one "
                    "changes it for all of them.",
                    organization_count=_count_organizations(role, scope_ids),
                    assignment_count=len(scope_ids),
                )
            )
        return findings

    def _scope_ids(self, role) -> list[int]:
        """Scope ids this role is actively granted on."""
        return list(
            models.UserRole.objects.filter(role=role, is_active=True).values_list(
                "object_id", flat=True
            )
        )

    def _check_permissions(self, role, scope_type) -> list[Finding]:
        if scope_type is None:
            return []
        inert = []
        for role_permission in role.permissions.all():
            allowed = get_permission_scope_types(role_permission.permission)
            if allowed is None:
                continue
            if scope_type not in allowed:
                inert.append(role_permission.permission)
        if not inert:
            return []
        return [
            self._finding(
                "cross-scope-permission",
                role,
                scope_type,
                f"Carries {len(inert)} permission(s) that cannot apply to a "
                f"{scope_type} role and are therefore inert: "
                f"{', '.join(sorted(inert))}.",
                permissions=sorted(inert),
            )
        ]

    def _check_label(self, role, scope_type) -> list[Finding]:
        description = (role.description or "").strip()
        if not description:
            return [
                self._finding(
                    "label-missing",
                    role,
                    scope_type,
                    "No description, so the machine name is what users see in "
                    "the role pickers.",
                )
            ]
        if description == role.name:
            return [
                self._finding(
                    "label-equals-name",
                    role,
                    scope_type,
                    "Description repeats the machine name, so the code leaks "
                    "into the role pickers.",
                )
            ]
        return []


def collect_findings(queryset=None) -> list[Finding]:
    """Run every check over the role catalogue, most severe first."""
    if queryset is None:
        queryset = models.Role.objects.all()
    queryset = queryset.select_related("content_type", "template").prefetch_related(
        "availability", "permissions"
    )
    roles = list(queryset)

    customer_content_type = _customer_content_type()
    customer_ids = {
        availability.object_id
        for role in roles
        for availability in role.availability.all()
        if availability.content_type_id == customer_content_type.id
    }
    customer_model = customer_content_type.model_class()
    customer_slugs = (
        dict(
            customer_model.objects.filter(id__in=customer_ids).values_list("id", "slug")
        )
        if customer_ids and customer_model is not None
        else {}
    )

    checker = RoleHygieneChecker(customer_slugs, customer_content_type.id)

    findings: list[Finding] = []
    for role in roles:
        findings += checker.check(role)

    findings.sort(
        key=lambda finding: (
            SEVERITY_ORDER.index(finding.severity),
            finding.check,
            finding.role_name,
        )
    )
    return findings


def build_report(queryset=None) -> dict[str, Any]:
    """The full report: what was checked, how many findings, and the findings."""
    if queryset is None:
        queryset = models.Role.objects.all()
    findings = collect_findings(queryset)
    counts = {severity: 0 for severity in SEVERITY_ORDER}
    for finding in findings:
        counts[finding.severity] += 1
    return {
        "roles_checked": queryset.exclude(
            content_type__model__in=CATALOG_SCOPE_MODELS
        ).count(),
        "roles_with_findings": len({finding.role_uuid for finding in findings}),
        "error_count": counts[ERROR],
        "warning_count": counts[WARNING],
        "info_count": counts[INFO],
        "findings": [finding.to_dict() for finding in findings],
    }
