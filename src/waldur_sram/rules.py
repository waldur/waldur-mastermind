"""Project roles for holders of SRAM placeholder roles.

A ``SramProjectRule`` matches SRAM groups by kind, collaboration labels and
group short name. For every matching group it selects projects of the group's
organization by ``backend_id`` or slug, and grants ``project_role`` there to
everyone who holds the group's placeholder role, whoever granted it.

Grants carry ``UserRole.source = "sram-rule:<rule uuid>:<group uuid>"``, so
reconciliation revokes exactly the grants a rule made for a group and nothing
else. Reconciliation runs when a group is pushed, a placeholder role is granted
or revoked, a rule changes, or a project appears or changes its identifiers.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass

from constance import config
from django.contrib.contenttypes.models import ContentType
from django.db import DatabaseError, connection, transaction
from django.db.models import Q
from rest_framework.exceptions import ValidationError

from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole
from waldur_core.permissions.utils import add_user, validate_role_grant
from waldur_core.structure.models import Customer, Project

from . import models
from .roles import RULE_SOURCE_PREFIX

logger = logging.getLogger(__name__)

PLACEHOLDERS = (
    "co_external_id",
    "co_identifier",
    "co_short_name",
    "org_short_name",
    "group_short_name",
)

# While a group's placeholder grants are being synced, per-grant signal handlers
# must not reconcile: the sync reconciles once when it is done.
_syncing = contextvars.ContextVar("waldur_sram_syncing", default=False)


@contextlib.contextmanager
def deferred() -> Iterator[None]:
    token = _syncing.set(True)
    try:
        yield
    finally:
        _syncing.reset(token)


def is_deferred() -> bool:
    return _syncing.get()


def grant_source(rule: models.SramProjectRule, group: models.SramGroup) -> str:
    return f"{RULE_SOURCE_PREFIX}{rule.uuid.hex}:{group.uuid.hex}"


# str.format_map raises these for malformed or unsupported replacement fields.
_FORMAT_ERRORS = (KeyError, IndexError, ValueError, AttributeError, TypeError)


def enabled() -> bool:
    """While SRAM is switched off, nothing SRAM-made is maintained."""
    return bool(config.SRAM_INTEGRATION_ENABLED)


def validate_pattern(pattern: str, match: str) -> None:
    """Raise ``ValueError`` for a pattern that cannot be evaluated."""
    try:
        rendered = pattern.format_map({name: "x" for name in PLACEHOLDERS})
    except _FORMAT_ERRORS as exc:
        raise ValueError(
            f"Invalid placeholder in {pattern!r}: {exc}. Use "
            + ", ".join("{" + name + "}" for name in PLACEHOLDERS)
        )
    if not rendered:
        raise ValueError("The pattern must not be empty.")
    if match == models.SramProjectRule.MatchType.REGEX:
        try:
            re.compile(rendered)
        except re.error as exc:
            raise ValueError(f"Invalid regular expression: {exc}")
        # The pattern runs in PostgreSQL, whose syntax differs from Python's
        # (no named groups, for one): ask the database itself.
        if connection.vendor == "postgresql":
            try:
                with transaction.atomic(), connection.cursor() as cursor:
                    cursor.execute("SELECT '' ~ %s", [rendered])
            except DatabaseError as exc:
                raise ValueError(
                    f"The database rejects this regular expression: {exc}".strip()
                )


@dataclass(frozen=True)
class GroupContext:
    group: models.SramGroup
    collaboration: models.SramGroup | None

    @property
    def labels(self) -> set[str]:
        source = self.collaboration or self.group
        return set(source.labels or [])

    @property
    def group_short_name(self) -> str:
        parts = self.group.urn_parts
        return parts[2] if len(parts) > 2 else ""

    def placeholders(self) -> dict[str, str] | None:
        """Values for the project pattern; ``None`` without a collaboration."""
        if self.collaboration is None:
            return None
        parts = self.group.urn_parts
        external_id = self.collaboration.external_id
        return {
            "co_external_id": external_id,
            "co_identifier": external_id.split("@", 1)[0],
            "co_short_name": parts[1] if len(parts) > 1 else "",
            "org_short_name": parts[0] if parts else "",
            "group_short_name": self.group_short_name,
        }


def group_context(group: models.SramGroup) -> GroupContext:
    if group.kind == models.SramGroup.Kind.COLLABORATION:
        return GroupContext(group, group)
    parts = group.urn_parts
    collaboration = (
        models.SramGroup.objects.filter(
            kind=models.SramGroup.Kind.COLLABORATION, urn=":".join(parts[:2])
        ).first()
        if len(parts) >= 2
        else None
    )
    return GroupContext(group, collaboration)


def _short_name_matches(name: str, patterns: Iterable[str]) -> bool:
    name = name.lower()
    for pattern in patterns:
        pattern = str(pattern).lower()
        if pattern.endswith("*"):
            if name.startswith(pattern[:-1]):
                return True
        elif name == pattern:
            return True
    return False


def rule_matches(rule: models.SramProjectRule, context: GroupContext) -> bool:
    group = context.group
    kind = rule.source_kind
    if kind != models.SramProjectRule.SourceKind.ANY and kind != group.kind:
        return False
    if rule.group_short_name_patterns:
        if group.kind != models.SramGroup.Kind.GROUP:
            return False
        if not _short_name_matches(
            context.group_short_name, rule.group_short_name_patterns
        ):
            return False
    if rule.labels and not (set(rule.labels) & context.labels):
        return False
    return True


def select_projects(rule: models.SramProjectRule, context: GroupContext):
    values = context.placeholders()
    customer = context.group.customer
    if values is None or customer is None:
        return Project.objects.none()
    match = rule.project_match
    if match == models.SramProjectRule.MatchType.REGEX:
        values = {name: re.escape(value) for name, value in values.items()}
    try:
        pattern = rule.project_pattern.format_map(values)
    except _FORMAT_ERRORS:
        logger.warning("SRAM rule %s has an invalid pattern.", rule.uuid.hex)
        return Project.objects.none()
    lookup = {
        models.SramProjectRule.MatchType.EXACT: "exact",
        models.SramProjectRule.MatchType.PREFIX: "startswith",
        models.SramProjectRule.MatchType.REGEX: "regex",
    }[match]
    # Never outside the group's organization, whatever the pattern says.
    return Project.available_objects.filter(
        customer=customer, **{f"{rule.project_field}__{lookup}": pattern}
    )


def _placeholder_holders(group: models.SramGroup):
    if group.role_id is None or group.customer_id is None:
        return []
    user_ids = UserRole.objects.filter(
        role_id=group.role_id,
        content_type=ContentType.objects.get_for_model(Customer),
        object_id=group.customer_id,
        is_active=True,
        user__is_active=True,
    ).values_list("user_id", flat=True)
    return list(User.objects.filter(id__in=user_ids))


def desired_grants(rule, context) -> set[tuple[int, int]] | None:
    """``(project id, user id)`` pairs the rule asserts for this group.

    ``None`` means the projects could not be selected: the caller must then
    leave the rule's grants alone rather than revoke them all.
    """
    if not rule.is_active or not rule_matches(rule, context):
        return set()
    holders = _placeholder_holders(context.group)
    if not holders:
        return set()
    try:
        with transaction.atomic():
            project_ids = list(
                select_projects(rule, context).values_list("id", flat=True)
            )
    except DatabaseError as exc:
        logger.warning("SRAM rule %s: cannot select projects: %s", rule.name, exc)
        return None
    return {(project_id, user.id) for project_id in project_ids for user in holders}


def _customer_ids(project_ids: Iterable[int]) -> set[int]:
    return set(
        Project.all_objects.filter(id__in=set(project_ids)).values_list(
            "customer_id", flat=True
        )
    )


def _revoke(user_roles, reason: str) -> set[int]:
    """Revoke and return the customers whose projects lost a grant."""
    project_ids = set()
    for user_role in user_roles.select_related("user"):
        user_role.revoke(reason=reason)
        project_ids.add(user_role.object_id)
    return _customer_ids(project_ids)


def _reconcile(rule: models.SramProjectRule, group: models.SramGroup) -> set[int]:
    context = group_context(group)
    wanted = desired_grants(rule, context)
    if wanted is None:
        return set()
    source = grant_source(rule, group)
    project_ct = ContentType.objects.get_for_model(Project)

    made = UserRole.objects.filter(
        source=source, is_active=True, content_type=project_ct
    )
    # A grant of another role is stale too: the rule's role was changed.
    stale = [
        user_role.id
        for user_role in made
        if user_role.role_id != rule.project_role_id
        or (user_role.object_id, user_role.user_id) not in wanted
    ]
    touched = _revoke(
        UserRole.objects.filter(id__in=stale),
        f"No longer granted by SRAM project rule {rule.name}",
    )

    held = set(
        UserRole.objects.filter(
            role=rule.project_role,
            content_type=project_ct,
            object_id__in={project_id for project_id, _ in wanted},
            is_active=True,
        ).values_list("object_id", "user_id")
    )
    missing = wanted - held
    if not missing:
        return touched
    projects = Project.objects.in_bulk({project_id for project_id, _ in missing})
    users = User.objects.in_bulk({user_id for _, user_id in missing})
    for project_id, user_id in sorted(missing):
        project, user = projects[project_id], users[user_id]
        try:
            validate_role_grant(project, user, rule.project_role)
            add_user(
                project,
                user,
                rule.project_role,
                source=source,
                reason=f"SRAM project rule {rule.name} for "
                f"{group.get_kind_display().lower()} {group.display_name}",
            )
        except ValidationError as exc:
            logger.warning(
                "SRAM rule %s: skipped granting %s to %s on %s: %s",
                rule.name,
                rule.project_role.name,
                user.uuid.hex,
                project.uuid.hex,
                exc,
            )
    return touched


def _rules():
    return models.SramProjectRule.objects.select_related("project_role")


def _follow_up(customer_ids: set[int]) -> None:
    """Re-check every rule in organizations where grants were revoked.

    A rule does not duplicate a grant another rule or group already made. When
    that grant goes, the rules still asserting the role must re-grant it.
    """
    if not customer_ids:
        return
    rules = list(_rules())
    for group in models.SramGroup.objects.filter(
        customer_id__in=customer_ids
    ).select_related("customer", "role"):
        for rule in rules:
            _reconcile(rule, group)


def _run(pairs: Iterable[tuple[models.SramProjectRule, models.SramGroup]]) -> None:
    touched: set[int] = set()
    with deferred():
        for rule, group in pairs:
            touched |= _reconcile(rule, group)
        _follow_up(touched)


def reconcile(rule: models.SramProjectRule, group: models.SramGroup) -> None:
    _run([(rule, group)])


def reconcile_group(group: models.SramGroup) -> None:
    reconcile_groups([group])


def reconcile_groups(groups: Iterable[models.SramGroup]) -> None:
    rules = list(_rules())
    _run((rule, group) for group in groups for rule in rules)


def reconcile_rule(rule: models.SramProjectRule) -> None:
    groups = models.SramGroup.objects.select_related("customer", "role")
    _run((rule, group) for group in groups)


def related_groups(group: models.SramGroup):
    """The group itself, plus a collaboration's groups (they use its labels)."""
    if group.kind != models.SramGroup.Kind.COLLABORATION or not group.urn:
        return [group]
    return [group] + list(
        models.SramGroup.objects.filter(
            kind=models.SramGroup.Kind.GROUP, urn__startswith=group.urn + ":"
        ).select_related("customer", "role")
    )


def revoke_rule(rule_uuid_hex: str, rule_name: str) -> None:
    with deferred():
        touched = _revoke(
            UserRole.objects.filter(
                source__startswith=f"{RULE_SOURCE_PREFIX}{rule_uuid_hex}:",
                is_active=True,
            ),
            f"SRAM project rule {rule_name} was deleted",
        )
        _follow_up(touched)


def revoke_group(group: models.SramGroup) -> set[int]:
    """Revoke the rule grants made for the group; the caller follows up."""
    return _revoke(
        UserRole.objects.filter(
            source__startswith=RULE_SOURCE_PREFIX,
            source__endswith=f":{group.uuid.hex}",
            is_active=True,
        ),
        f"SRAM {group.get_kind_display().lower()} {group.display_name} was deleted",
    )


def revoke_orphans() -> int:
    """Revoke rule grants whose rule or group no longer exists.

    Deleting a rule or group while SRAM is switched off leaves its grants in
    place; ``sram_resync`` clears them once SRAM is back on.
    """
    rule_ids = {
        uuid.hex
        for uuid in models.SramProjectRule.objects.values_list("uuid", flat=True)
    }
    group_ids = {
        uuid.hex for uuid in models.SramGroup.objects.values_list("uuid", flat=True)
    }
    orphans = []
    for user_role_id, source in UserRole.objects.filter(
        source__startswith=RULE_SOURCE_PREFIX, is_active=True
    ).values_list("id", "source"):
        _, rule_id, group_id = (source.split(":") + ["", ""])[:3]
        if rule_id not in rule_ids or group_id not in group_ids:
            orphans.append(user_role_id)
    with deferred():
        touched = _revoke(
            UserRole.objects.filter(id__in=orphans),
            "The SRAM project rule or group was deleted",
        )
        _follow_up(touched)
    return len(orphans)


def groups_for_customer(customer_id: int):
    return models.SramGroup.objects.filter(customer_id=customer_id).select_related(
        "customer", "role"
    )


def preview(rule: models.SramProjectRule) -> list[dict]:
    """What the rule asserts right now, per matching group."""
    result = []
    groups = models.SramGroup.objects.filter(~Q(customer=None)).select_related(
        "customer", "role"
    )
    for group in groups:
        context = group_context(group)
        if not rule_matches(rule, context):
            continue
        try:
            with transaction.atomic():
                projects = list(select_projects(rule, context))
        except DatabaseError:
            projects = []
        result.append(
            {
                "group": group,
                "projects": projects,
                "users": _placeholder_holders(group),
            }
        )
    return result
