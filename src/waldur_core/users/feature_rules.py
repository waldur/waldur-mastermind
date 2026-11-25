"""Who a user-scoped feature flag, or a micro-app, is turned on for.

Global values stay in ``core.Feature`` and are served to everyone by the
anonymous configuration endpoint. A ``FeatureRule`` adds users on top of that:
the effective value of a user-scoped flag is the global value or any active
rule that matches the user. ``waldur_core.core.features.is_enabled`` reaches
this module through the resolver registered in ``UserConfig.ready``.
"""

from dataclasses import dataclass, field

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from waldur_core.core import features
from waldur_core.core.models import (
    FilterCheckResult,
    RuleEvaluationResult,
    User,
    UserDetailsMatchMixin,
)
from waldur_core.permissions.models import UserRole
from waldur_core.structure import managers as structure_managers
from waldur_core.structure.models import Customer, Project
from waldur_core.users import models


@dataclass
class UserContext:
    """What a rule's criteria are checked against, loaded once per user."""

    user: User
    customer_ids: set[int] = field(default_factory=set)
    organization_group_ids: set[int] = field(default_factory=set)
    role_names: set[str] = field(default_factory=set)


def load_user_context(user: User) -> UserContext:
    """The organizations the user belongs to, their groups and the user's roles.

    A user belongs to an organization when holding an active role on it or on
    one of its projects.
    """
    direct = set(structure_managers.get_connected_customers(user))
    via_projects = set(
        Project.available_objects.filter(
            id__in=structure_managers.get_connected_projects(user)
        ).values_list("customer_id", flat=True)
    )
    customer_ids = direct | via_projects
    group_ids = set(
        Customer.organization_groups.through.objects.filter(
            customer_id__in=customer_ids
        ).values_list("organizationgroup_id", flat=True)
    )
    role_names = set(
        UserRole.objects.filter(user=user, is_active=True).values_list(
            "role__name", flat=True
        )
    )
    return UserContext(
        user=user,
        customer_ids=customer_ids,
        organization_group_ids=group_ids,
        role_names=role_names,
    )


def live_feature_keys() -> set[str]:
    """The keys a rule can have an effect on: user-scoped flags, and the
    micro-apps that have a setting."""
    return set(features.USER_SCOPED_FEATURES) | {
        models.micro_app_feature_key(name)
        for name in models.MicroAppSetting.objects.values_list("name", flat=True)
    }


def is_orphaned_key(feature_key: str) -> bool:
    """Whether a rule with this key has no effect: its flag stopped being
    user-scoped or left the code, or its micro-app has no setting."""
    if feature_key.startswith(models.MICRO_APP_KEY_PREFIX):
        name = feature_key[len(models.MICRO_APP_KEY_PREFIX) :]
        return not models.MicroAppSetting.objects.filter(name=name).exists()
    return not features.is_user_scoped(feature_key)


def _check(name, configured, matched, user_value, rule_value, reason_on, reason_off):
    return FilterCheckResult(
        name=name,
        configured=configured,
        matched=matched,
        user_value=user_value,
        rule_value=rule_value,
        reason=(
            "Not configured"
            if not configured
            else (reason_on if matched else reason_off)
        ),
    )


# How many names a test-match breakdown shows for one criterion.
BREAKDOWN_NAMES_LIMIT = 20


def _names(values) -> list[str]:
    values = sorted(values)
    if len(values) > BREAKDOWN_NAMES_LIMIT:
        return values[:BREAKDOWN_NAMES_LIMIT] + [
            f"… {len(values) - BREAKDOWN_NAMES_LIMIT} more"
        ]
    return values


_IDENTITY_FIELDS = (
    "user_affiliations",
    "user_email_patterns",
    "user_identity_sources",
    "user_nationalities",
    "user_organization_types",
    "user_assurance_levels",
    "user_claims",
)


def _has_identity_criteria(rule: models.FeatureRule) -> bool:
    return any(getattr(rule, name) for name in _IDENTITY_FIELDS)


def has_criteria(rule: models.FeatureRule) -> bool:
    """Whether ``rule`` configures any criterion.

    A rule without one matches no one: turning a feature on for everyone is
    what its global switch is for. A rule also ends up without criteria when
    every user, organization or group it lists is deleted, and it then stops
    matching instead of widening to everyone.
    """
    return (
        bool(rule.roles)
        or _has_identity_criteria(rule)
        or rule.users.exists()
        or rule.customers.exists()
        or rule.organization_groups.exists()
    )


def evaluate(
    rule: models.FeatureRule,
    context: UserContext,
    listed_user_ids=None,
    describe: bool = True,
) -> RuleEvaluationResult:
    """Every configured criterion group must match; the breakdown names each.

    A rule that configures no criterion at all matches no one (see
    ``has_criteria``).

    ``listed_user_ids`` lets a caller that evaluates many rules pass the rule's
    listed users it already loaded; otherwise they are queried. With
    ``describe`` the breakdown names the users, organizations and groups on
    both sides, for a person to read; without it only the verdict counts.
    """
    user = context.user
    listed_names: dict[int, str] = {}
    if listed_user_ids is None:
        listed_names = dict(rule.users.values_list("id", "username"))
        listed_user_ids = set(listed_names)
    customers = list(rule.customers.all())
    groups = list(rule.organization_groups.all())
    rule_customer_ids = {c.id for c in customers}
    rule_group_ids = {g.id for g in groups}
    rule_roles = set(rule.roles or [])

    users_configured = bool(listed_user_ids)
    users_matched = not users_configured or user.id in listed_user_ids

    orgs_configured = bool(rule_customer_ids or rule_group_ids)
    orgs_matched = not orgs_configured or bool(
        rule_customer_ids & context.customer_ids
        or rule_group_ids & context.organization_group_ids
    )

    roles_configured = bool(rule_roles)
    roles_matched = not roles_configured or bool(rule_roles & context.role_names)

    users_rule_value = orgs_rule_value = orgs_user_value = None
    if describe:
        if users_configured:
            users_rule_value = _names(listed_names.values())
        if orgs_configured:
            orgs_rule_value = _names(
                [c.name for c in customers]
                + [f"{g.name} (organization group)" for g in groups]
            )
            orgs_user_value = _names(
                Customer.objects.filter(id__in=context.customer_ids).values_list(
                    "name", flat=True
                )
            )

    own = [
        _check(
            "users",
            users_configured,
            users_matched,
            user.username,
            users_rule_value,
            "User is listed",
            "User is not listed",
        ),
        _check(
            "organizations",
            orgs_configured,
            orgs_matched,
            orgs_user_value,
            orgs_rule_value,
            "User belongs to a listed organization or organization group",
            "User belongs to none of the listed organizations or groups",
        ),
        _check(
            "roles",
            roles_configured,
            roles_matched,
            sorted(context.role_names),
            sorted(rule_roles) or None,
            "User holds a listed role",
            "User holds none of the listed roles",
        ),
    ]
    details = UserDetailsMatchMixin.evaluate_for_user(rule, user, required=True)
    configured = (
        users_configured
        or orgs_configured
        or roles_configured
        or _has_identity_criteria(rule)
    )
    return RuleEvaluationResult(
        matched=configured
        and users_matched
        and orgs_matched
        and roles_matched
        and details.matched,
        filter_results=own + details.filter_results,
    )


def _active_rules(feature_key=None):
    rules = models.FeatureRule.objects.filter(is_active=True)
    if feature_key is not None:
        rules = rules.filter(feature_key=feature_key)
    return rules.prefetch_related("customers", "organization_groups")


def matching_rules(user: User, feature_key=None) -> list[models.FeatureRule]:
    """The active rules, optionally for one key, that match ``user``."""
    if user is None or not user.is_authenticated:
        return []
    rules = list(_active_rules(feature_key))
    if not rules:
        return []
    # Listed users are only needed to know whether this user is one of them,
    # so a rule's full list is never loaded.
    listing = models.FeatureRule.users.through.objects.filter(
        featurerule_id__in=[rule.pk for rule in rules]
    )
    rules_listing_anyone = set(listing.values_list("featurerule_id", flat=True))
    rules_listing_user = set(
        listing.filter(user_id=user.id).values_list("featurerule_id", flat=True)
    )
    context = load_user_context(user)
    matched = []
    for rule in rules:
        if rule.pk in rules_listing_anyone and rule.pk not in rules_listing_user:
            continue
        listed = {user.id} if rule.pk in rules_listing_user else set()
        if evaluate(rule, context, listed_user_ids=listed, describe=False).matched:
            matched.append(rule)
    return matched


def targeted_keys(user: User) -> frozenset[str]:
    """Keys that active rules turn on for ``user``."""
    return frozenset(rule.feature_key for rule in matching_rules(user))


def user_feature_values(user: User, keys=None) -> dict[str, dict[str, bool]]:
    """Effective values of the user-scoped flags, as ``section -> key -> on``.

    ``keys`` is the set of keys rules turn on for the user, when the caller has
    already computed it.
    """
    if keys is None:
        keys = targeted_keys(user)
    global_values = features.global_values()
    result: dict[str, dict[str, bool]] = {}
    for dotted in sorted(features.USER_SCOPED_FEATURES):
        section, key = dotted.split(".", 1)
        result.setdefault(section, {})[key] = bool(
            global_values.get(section, {}).get(key) or dotted in keys
        )
    return result


def user_micro_apps(user: User, keys=None) -> dict[str, bool]:
    """Whether each micro-app the operator has a setting for is offered to ``user``.

    An app without a setting is absent: it follows user.show_micro_apps.
    """
    if keys is None:
        keys = targeted_keys(user)
    result = {}
    for setting in models.MicroAppSetting.objects.all():
        if setting.mode == models.MicroAppSetting.Modes.ALL:
            result[setting.name] = True
        elif setting.mode == models.MicroAppSetting.Modes.AUDIENCE:
            result[setting.name] = setting.feature_key in keys
        else:
            result[setting.name] = False
    return result


# How many candidate users matched_users() checks against identity attributes,
# which only Python can evaluate, before it answers with a lower bound.
MATCHED_USERS_SCAN_LIMIT = 5000
MATCHED_USERS_PREVIEW = 10


@dataclass
class MatchedUsers:
    count: int
    checked: int
    complete: bool
    users: list[User]


def _rule_customer_ids(rule: models.FeatureRule) -> set[int]:
    ids = set(rule.customers.values_list("id", flat=True))
    ids |= set(
        Customer.objects.filter(
            organization_groups__in=rule.organization_groups.all()
        ).values_list("id", flat=True)
    )
    return ids


def matched_users(rule: models.FeatureRule) -> MatchedUsers:
    """The active users ``rule`` matches, whether or not it is active itself.

    Listed users, organizations and roles narrow the candidates in SQL; the
    identity attributes are checked in Python, for at most
    MATCHED_USERS_SCAN_LIMIT candidates. A rule without criteria matches no one.
    """
    if not has_criteria(rule):
        return MatchedUsers(count=0, checked=0, complete=True, users=[])
    candidates = User.objects.filter(is_active=True).order_by("username")
    listed = list(rule.users.values_list("id", flat=True))
    if listed:
        candidates = candidates.filter(id__in=listed)
    if rule.customers.exists() or rule.organization_groups.exists():
        customer_ids = _rule_customer_ids(rule)
        project_ids = Project.available_objects.filter(
            customer_id__in=customer_ids
        ).values("id")
        members = UserRole.objects.filter(is_active=True).filter(
            Q(
                content_type=ContentType.objects.get_for_model(Customer),
                object_id__in=customer_ids,
            )
            | Q(
                content_type=ContentType.objects.get_for_model(Project),
                object_id__in=project_ids,
            )
        )
        candidates = candidates.filter(id__in=members.values("user_id"))
    if rule.roles:
        candidates = candidates.filter(
            id__in=UserRole.objects.filter(
                is_active=True, role__name__in=rule.roles
            ).values("user_id")
        )

    if not _has_identity_criteria(rule):
        return MatchedUsers(
            count=candidates.count(),
            checked=candidates.count(),
            complete=True,
            users=list(candidates[:MATCHED_USERS_PREVIEW]),
        )

    matched: list[User] = []
    count = checked = 0
    for user in candidates[: MATCHED_USERS_SCAN_LIMIT + 1].iterator():
        if checked == MATCHED_USERS_SCAN_LIMIT:
            return MatchedUsers(count, checked, False, matched)
        checked += 1
        if UserDetailsMatchMixin.evaluate_for_user(rule, user, required=True).matched:
            count += 1
            if len(matched) < MATCHED_USERS_PREVIEW:
                matched.append(user)
    return MatchedUsers(count, checked, True, matched)


def explain(user: User) -> dict:
    """Each flag's and each set-up micro-app's value for ``user``, and why."""
    by_key: dict[str, list] = {}
    for rule in matching_rules(user):
        by_key.setdefault(rule.feature_key, []).append(rule.uuid)
    global_values = features.global_values()

    feature_states = []
    for section in features.FEATURES:
        for item in section["items"]:
            dotted = f"{section['key']}.{item['key']}"
            rules = (
                by_key.get(dotted, []) if item["scope"] == features.SCOPE_USER else []
            )
            if global_values[section["key"]][item["key"]]:
                source = "global"
            elif rules:
                source = "rule"
            else:
                source = "off"
            feature_states.append(
                {
                    "key": dotted,
                    "scope": item["scope"],
                    "enabled": source != "off",
                    "source": source,
                    "rules": rules,
                }
            )

    keys = frozenset(by_key)
    enabled_apps = user_micro_apps(user, keys)
    app_states = [
        {
            "name": setting.name,
            "mode": setting.mode,
            "enabled": enabled_apps[setting.name],
            "rules": by_key.get(setting.feature_key, [])
            if setting.mode == models.MicroAppSetting.Modes.AUDIENCE
            else [],
        }
        for setting in models.MicroAppSetting.objects.all()
    ]
    return {"features": feature_states, "micro_apps": app_states}
