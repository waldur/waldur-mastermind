"""What a service provider's account settings produce.

A dry-run preview of a change of the provider's account options, and one GLAuth
directory rendered across the provider's offerings that share accounts.
"""

from django.utils.translation import gettext as _

from waldur_mastermind.marketplace import models, posix_ids, utils
from waldur_mastermind.marketplace.enums import (
    AccountScopes,
    OfferingStates,
    OfferingUserStates,
)
from waldur_mastermind.marketplace.handlers import OFFERING_USER_ALLOWED_OFFERING_TYPES

POLICY = utils.UsernameGenerationPolicy
ACCOUNT_SETTINGS = tuple(models.Offering.ACCOUNT_SETTING_DEFAULTS)

#: The states in which a username-setting change renames an offering account,
#: as the regeneration handler applies it.
RENAMED_STATES = (
    OfferingUserStates.CREATION_REQUESTED,
    OfferingUserStates.CREATING,
    OfferingUserStates.OK,
)

#: What a new person's username looks like under a policy that does not derive
#: it from the POSIX identity.
USERNAME_PLACEHOLDERS = {
    POLICY.SERVICE_PROVIDER.value: "",
    POLICY.FULL_NAME.value: "<first name>_<last name>_00",
    POLICY.WALDUR_USERNAME.value: "<Waldur username>",
    POLICY.FREEIPA.value: "<FreeIPA username>",
    POLICY.IDENTITY_CLAIM.value: "<site username claim>",
}


def account_offerings(provider) -> list:
    """The provider's offerings that hold accounts for people."""
    return list(
        models.Offering.objects.filter(
            customer_id=provider.customer_id,
            type__in=OFFERING_USER_ALLOWED_OFFERING_TYPES,
        )
        .exclude(state=OfferingStates.ARCHIVED)
        .select_related("customer__serviceprovider")
        .order_by("name")
    )


def _resolved(offering, provider_options) -> dict:
    return {
        name: offering.resolve_account_setting_with_source(
            name, provider_options=provider_options
        )
        for name in ACCOUNT_SETTINGS
    }


def _username_settings(settings) -> tuple:
    """The part of the settings a generated username depends on."""
    policy = settings["username_generation_policy"][0]
    if policy == POLICY.ANONYMIZED.value:
        return policy, settings["username_anonymized_prefix"][0]
    return policy, None


def _known_uid(offering, account) -> int | None:
    """The UID an account holds, read without allocating one.

    ``None`` means a rename would allocate a new UID first.
    """
    uid = (account.backend_metadata or {}).get("uidnumber")
    if uid:
        return uid
    options = offering.plugin_options or {}
    if not options.get("enable_posix_account", True):
        return None
    if options.get("uid_source", "pool") == "user_attribute":
        return account.user.uid_number
    pool = posix_ids.resolve(offering)
    if pool is None or not pool.manages(posix_ids.UID):
        return None
    identity = (
        models.PosixIdentity.objects.filter(
            pool=pool, released_at__isnull=True, user_id=account.user_id
        )
        .exclude(uid__isnull=True)
        .first()
    )
    return identity.uid if identity else None


def _preview_username(offering, account, policy, prefix) -> str | None:
    """The username ``account`` would get under ``policy``, writing nothing.

    Mirrors ``utils.generate_username`` for an explicit policy and prefix. The
    anonymized policy only reads a UID the person already holds, because
    allocating one is what a real rename does; ``None`` means it would.
    """
    user = account.user
    if policy == POLICY.ANONYMIZED.value:
        uid = _known_uid(offering, account)
        return f"{prefix}{uid}" if uid is not None else None
    if policy == POLICY.FULL_NAME.value:
        return utils.create_username_from_full_name(user, offering)
    if policy == POLICY.WALDUR_USERNAME.value:
        return user.username
    if policy == POLICY.FREEIPA.value:
        return utils.create_username_from_freeipa_profile(user)
    if policy == POLICY.IDENTITY_CLAIM.value:
        return (user.details or {}).get("site_username", "")
    return ""


def _example(offering, settings) -> dict:
    """What a person new to the offering would get under ``settings``."""
    policy = settings["username_generation_policy"][0]
    if policy == POLICY.ANONYMIZED.value:
        prefix = settings["username_anonymized_prefix"][0]
        options = offering.plugin_options or {}
        uid = None
        if options.get("enable_posix_account", True) and (
            options.get("uid_source", "pool") != "user_attribute"
        ):
            uid = posix_ids.peek_next_value(posix_ids.resolve(offering), posix_ids.UID)
        username = f"{prefix}{uid}" if uid is not None else f"{prefix}<UID>"
    else:
        username = USERNAME_PLACEHOLDERS.get(policy, "")
    return {
        "username": username,
        "home_directory": f"{settings['homedir_prefix'][0]}{username or '<username>'}",
        "login_shell": settings["login_shell"][0],
    }


def _setting(resolved) -> dict:
    value, source = resolved
    return {"value": value, "source": source}


def _preview_offering(offering, before, after) -> dict:
    changed = [name for name in ACCOUNT_SETTINGS if before[name] != after[name]]
    renames = []
    provider_accounts_kept = 0
    keeping_home_or_shell = 0
    if changed:
        renaming = _username_settings(before) != _username_settings(after)
        posix_changed = bool({"homedir_prefix", "login_shell"} & set(changed))
        policy = after["username_generation_policy"][0]
        prefix = after["username_anonymized_prefix"][0]
        home_prefix = after["homedir_prefix"][0]
        accounts = models.OfferingUser.objects.filter(
            offering=offering, state__in=RENAMED_STATES
        ).select_related("user")
        for account in accounts:
            if account.service_provider_account_id:
                # A provider account owns its name and POSIX values; nothing
                # regenerates them when the settings change.
                if renaming or posix_changed:
                    provider_accounts_kept += 1
                continue
            if renaming:
                new_username = _preview_username(offering, account, policy, prefix)
                if new_username != account.username:
                    renames.append(
                        {
                            "username": account.username or "",
                            "new_username": new_username,
                            "home_directory": (account.backend_metadata or {}).get(
                                "homeDir"
                            )
                            or "",
                            "new_home_directory": (
                                f"{home_prefix}{new_username}" if new_username else ""
                            ),
                        }
                    )
                    continue
            if posix_changed and account.username:
                # Home directory and login shell apply to accounts created later.
                keeping_home_or_shell += 1
    return {
        "uuid": offering.uuid.hex,
        "name": offering.name,
        "settings": {
            name: {"before": _setting(before[name]), "after": _setting(after[name])}
            for name in ACCOUNT_SETTINGS
        },
        "changed": changed,
        "example": _example(offering, after),
        "renames": renames,
        "provider_accounts_kept": provider_accounts_kept,
        "accounts_keeping_home_or_shell": keeping_home_or_shell,
    }


def preview_account_options(provider, proposed_options: dict) -> dict:
    """What replacing the provider's account options with ``proposed_options`` does.

    Writes nothing. Offering accounts are renamed as the regeneration handler
    would rename them; provider accounts keep their names, and home directory
    and login shell apply only to accounts created afterwards.
    """
    current_options = dict(provider.account_options or {})
    offerings = [
        _preview_offering(
            offering,
            _resolved(offering, current_options),
            _resolved(offering, proposed_options),
        )
        for offering in account_offerings(provider)
    ]
    conflicts = 0
    if (
        proposed_options.get("account_scope") == AccountScopes.PROVIDER
        and provider.account_scope != AccountScopes.PROVIDER
    ):
        conflicts = len(utils.provider_username_conflicts(provider))
    return {
        "account_options": {
            "current": current_options,
            "proposed": dict(proposed_options),
        },
        "offerings": offerings,
        "renamed": sum(len(o["renames"]) for o in offerings),
        "provider_accounts_kept": sum(o["provider_accounts_kept"] for o in offerings),
        "accounts_keeping_home_or_shell": sum(
            o["accounts_keeping_home_or_shell"] for o in offerings
        ),
        "username_conflicts": conflicts,
    }


def glauth_offerings(provider) -> list:
    """The offerings rendered into the provider's shared directory."""
    return [
        offering
        for offering in account_offerings(provider)
        if offering.uses_provider_accounts
        and (offering.plugin_options or {}).get(
            "service_provider_can_create_offering_user"
        )
    ]


class _Directory:
    """Accumulates per-offering GLAuth output into one directory."""

    def __init__(self):
        self.warnings = []
        self.users = {}
        self.groups = {}
        self.robot_accounts = {}
        self.toml_users = {}
        self.toml_groups = {}
        self.password_hashes = set()

    def warn(self, message):
        if message not in self.warnings:
            self.warnings.append(message)

    def add_user(self, user):
        current = self.users.get(user["username"])
        if current is None:
            self.users[user["username"]] = {
                **user,
                "memberships": list(user["memberships"]),
            }
            return
        if current["uidnumber"] != user["uidnumber"]:
            self.warn(
                _(
                    "%(username)s has UID %(first)s on one offering and %(second)s "
                    "on another; the first is kept."
                )
                % {
                    "username": user["username"],
                    "first": current["uidnumber"],
                    "second": user["uidnumber"],
                }
            )
            return
        # Enabled when any offering grants access.
        current["disabled"] = current["disabled"] and user["disabled"]
        seen = {membership["gid"] for membership in current["memberships"]}
        current["memberships"] += [
            membership
            for membership in user["memberships"]
            if membership["gid"] not in seen
        ]

    def add_group(self, group):
        current = self.groups.get(group["gid"])
        if current is None:
            self.groups[group["gid"]] = {**group, "members": list(group["members"])}
            return
        if current["name"] != group["name"]:
            self.warn(
                _(
                    "GID %(gid)s is named %(first)s on one offering and %(second)s "
                    "on another; the first is kept."
                )
                % {
                    "gid": group["gid"],
                    "first": current["name"],
                    "second": group["name"],
                }
            )
        current["members"] = sorted(set(current["members"]) | set(group["members"]))

    def add_toml_user(self, record):
        current = self.toml_users.get(record["name"])
        if current is None:
            self.toml_users[record["name"]] = {
                **record,
                "otherGroups": list(record.get("otherGroups", [])),
                "customattributes": dict(record.get("customattributes", {})),
            }
            return
        if current["uidnumber"] != record["uidnumber"]:
            return  # Reported by add_user.
        current["disabled"] = current["disabled"] and record["disabled"]
        current["otherGroups"] = sorted(
            set(current["otherGroups"]) | set(record.get("otherGroups", []))
        )
        current["customattributes"].update(record.get("customattributes", {}))

    def add_toml_group(self, name, gid):
        self.toml_groups.setdefault(int(gid), {"name": name, "gidnumber": int(gid)})

    def check_group_names(self):
        gids_by_name = {}
        for group in self.toml_groups.values():
            gids_by_name.setdefault(group["name"], set()).add(group["gidnumber"])
        for name, gids in sorted(gids_by_name.items()):
            if len(gids) > 1:
                self.warn(
                    _("Group name %(name)s is used for GIDs %(gids)s.")
                    % {"name": name, "gids": ", ".join(str(g) for g in sorted(gids))}
                )

    def check_passwords(self):
        if len(self.password_hashes) > 1:
            self.warn(
                _(
                    "The offerings set different shared user passwords, so no "
                    "password is set for their users."
                )
            )
            for record in self.toml_users.values():
                record.pop("passsha256", None)


def build_provider_glauth(provider) -> dict:
    """One GLAuth directory for the offerings of ``provider`` that share accounts.

    Each person appears once, enabled when any offering grants access, with the
    groups of every offering merged. Disagreements that cannot be merged --
    different shared passwords, one name for two groups -- are reported in
    ``warnings`` rather than resolved by guessing. Building an offering's tree
    allocates GIDs for its role groups, as the offering-level view does.
    """
    directory = _Directory()
    offerings = glauth_offerings(provider)
    for offering in offerings:
        tree = utils.build_glauth_tree(offering)
        records = utils.generate_glauth_records_for_offering_users(
            offering, tree["_offering_users"], extra_user_gids=tree["_user_role_gids"]
        )
        robot_records = utils.generate_glauth_records_for_robot_accounts(
            offering, models.RobotAccount.objects.filter(resource__offering=offering)
        )
        directory.password_hashes.add(utils.generate_offering_password_hash(offering))
        for user in tree["users"]:
            directory.add_user(user)
        for group in tree["groups"]:
            directory.add_group(group)
        for robot in tree["robot_accounts"]:
            directory.robot_accounts.setdefault(robot["username"], robot)
        for record in records["users"] + robot_records["users"]:
            directory.add_toml_user(record)
        for group in records["groups"] + robot_records["groups"]:
            directory.add_toml_group(group["name"], group["gidnumber"])
        for group in tree["groups"]:
            if group.get("kind") != "personal":
                directory.add_toml_group(group["name"], group["gid"])
    directory.check_group_names()
    directory.check_passwords()
    return {
        "offerings": [
            {"uuid": o.uuid.hex, "name": o.name, "slug": o.slug or ""}
            for o in offerings
        ],
        "groups": list(directory.groups.values()),
        "users": sorted(directory.users.values(), key=lambda u: u["username"]),
        "robot_accounts": list(directory.robot_accounts.values()),
        "warnings": directory.warnings,
        # Internal: the records the TOML emitter renders.
        "_toml_groups": list(directory.toml_groups.values()),
        "_toml_users": sorted(directory.toml_users.values(), key=lambda u: u["name"]),
    }
