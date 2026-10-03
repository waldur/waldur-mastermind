"""Provider project groups: one POSIX group per project using a service provider.

A :class:`marketplace.models.ServiceProviderProjectGroup` is created when a
project starts using an offering of a provider that has
``project_groups_enabled`` in its account options, and its GID comes from the
provider's :class:`marketplace.models.PosixIdPool` (the group GID range when
the pool has one). A group is never deleted or renumbered because its
project's resources go away: files may still carry the GID.

A project *uses* the provider while it has a resource that is

* on an offering with offering users (Basic, Script, site agent) that does not
  set ``enable_posix_account: false``,
* not terminated -- creating, updating, terminating and erred all count,
* past approval: a resource whose create order still waits for the consumer,
  the provider, the project or a start date, or was rejected or cancelled,
  does not count, so such an order never takes a GID,

and the project itself is not (soft-)deleted.
"""

import logging
import re
import uuid
from collections import defaultdict
from functools import partial

from django.contrib.contenttypes.models import ContentType
from django.db import IntegrityError, transaction
from django.db.models import Exists, OuterRef, Q, QuerySet
from django.utils import timezone

from waldur_core.core.middleware import get_skip_side_effects
from waldur_core.core.utils import is_uuid_like
from waldur_core.logging import event_dispatch
from waldur_core.logging import tasks as logging_tasks
from waldur_core.logging.enums import ObservableObjectType
from waldur_core.permissions import utils as permission_utils
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.models import UserRole
from waldur_core.structure import models as structure_models
from waldur_mastermind.marketplace.enums import (
    OFFERING_USER_ALLOWED_OFFERING_TYPES,
    OfferingUserStates,
    OrderStates,
    OrderTypes,
    ResourceStates,
)

from . import models, posix_ids

logger = logging.getLogger(__name__)

# A create order in one of these states has not been approved yet.
AWAITING_APPROVAL_STATES = (
    OrderStates.PENDING_CONSUMER,
    OrderStates.PENDING_PROVIDER,
    OrderStates.PENDING_PROJECT,
    OrderStates.PENDING_START_DATE,
)
# Its resource does not count while its create order is in one of these.
UNAPPROVED_STATES = AWAITING_APPROVAL_STATES + (
    OrderStates.REJECTED,
    OrderStates.CANCELED,
)
# An order entering one of these has been approved.
APPROVED_STATES = (OrderStates.EXECUTING, OrderStates.DONE)

NAME_RE = re.compile(models.ServiceProviderProjectGroup.NAME_PATTERN)
NAME_MAX_LENGTH = models.ServiceProviderProjectGroup.NAME_MAX_LENGTH
_INVALID_NAME_CHARS = re.compile(r"[^a-z0-9_-]+")


class GroupNameTaken(Exception):
    """An explicitly requested group name is already used at the provider."""


class ProjectNotAdoptable(Exception):
    """A project reference that cannot be adopted; the message says why."""


def can_pin(user, service_provider) -> bool:
    """Staff and the owners of the provider's organization pin GIDs."""
    return user.is_staff or service_provider.customer.has_user(user, CustomerRole.OWNER)


def adoptable_projects(service_provider) -> QuerySet:
    """Projects related to the provider: a resource or an order, in any state.

    A provider owner may adopt groups only for these, so the endpoint cannot be
    used to look up arbitrary projects. A project with only a pending or a
    rejected order qualifies: the directory may already hold its group.
    """
    customer_id = service_provider.customer_id
    return structure_models.Project.available_objects.filter(
        Q(
            id__in=models.Resource.objects.filter(
                offering__customer_id=customer_id
            ).values("project_id")
        )
        | Q(
            id__in=models.Order.objects.filter(
                offering__customer_id=customer_id
            ).values("project_id")
        )
    )


def resolve_project(service_provider, reference: str, user):
    """The project a UUID or slug refers to, among those ``user`` may adopt for.

    Raises :class:`ProjectNotAdoptable` with a readable reason.
    """
    reference = (reference or "").strip()
    projects = structure_models.Project.available_objects.all()
    allowed = projects if user.is_staff else adoptable_projects(service_provider)
    if is_uuid_like(reference):
        project = projects.filter(uuid=reference).first()
        if project is None:
            raise ProjectNotAdoptable("There is no project with this UUID.")
        if not allowed.filter(pk=project.pk).exists():
            raise ProjectNotAdoptable(
                "The project has no resource or order at this service provider."
            )
        return project
    matches = list(allowed.filter(slug=reference)[:2])
    if len(matches) == 1:
        return matches[0]
    if matches:
        raise ProjectNotAdoptable(
            f"More than one project has the slug {reference}; give the project UUID."
        )
    raise ProjectNotAdoptable(
        f"No project with the slug {reference} has a resource or order at this "
        "service provider; give the project UUID."
    )


def offering_qualifies(offering) -> bool:
    """Whether resources on ``offering`` put their project in a provider group."""
    return offering.type in OFFERING_USER_ALLOWED_OFFERING_TYPES and bool(
        (offering.plugin_options or {}).get("enable_posix_account", True)
    )


def offering_type_qualifies(obj) -> bool:
    """Whether the offering of a resource or an order can qualify, cheaply.

    Reads the type from an offering already loaded on ``obj``, else fetches
    just that column, so the signal handlers that run on every resource and
    order save never load an unrelated offering.
    """
    offering = obj._state.fields_cache.get("offering")
    if offering is not None:
        offering_type = offering.type
    else:
        offering_type = (
            models.Offering.objects.filter(pk=obj.offering_id)
            .values_list("type", flat=True)
            .first()
        )
    return offering_type in OFFERING_USER_ALLOWED_OFFERING_TYPES


def _qualifying_offering_filter(prefix: str = "") -> Q:
    # Containment rather than a key lookup: a negated key lookup is NULL, not
    # true, for the offerings that do not set the option at all.
    return Q(**{f"{prefix}type__in": OFFERING_USER_ALLOWED_OFFERING_TYPES}) & ~Q(
        **{f"{prefix}plugin_options__contains": {"enable_posix_account": False}}
    )


def _awaiting_approval():
    return Exists(
        models.Order.objects.filter(
            resource_id=OuterRef("pk"),
            type=OrderTypes.CREATE,
            state__in=UNAPPROVED_STATES,
        )
    )


def active_resources() -> QuerySet:
    """Resources that make their project use the offering's provider."""
    return (
        models.Resource.objects.filter(
            _qualifying_offering_filter("offering__"), project__is_removed=False
        )
        .exclude(state=ResourceStates.TERMINATED)
        .exclude(_awaiting_approval())
    )


def resource_counts(resource) -> bool:
    """Whether this one resource makes its project use the provider."""
    return active_resources().filter(pk=resource.pk).exists()


def annotate_in_use(queryset: QuerySet) -> QuerySet:
    """Annotate ``in_use``: the project uses the group's provider."""
    return queryset.annotate(
        in_use=Exists(
            active_resources().filter(
                project_id=OuterRef("project_id"),
                offering__customer_id=OuterRef("service_provider__customer_id"),
            )
        )
    )


def switch_warnings(service_provider, options: dict) -> list[str]:
    """What to settle before turning project groups on with ``options``."""
    if not (options or {}).get("project_groups_enabled"):
        return []
    pool = posix_ids.provider_pool(service_provider)
    if pool is None or posix_ids.group_range(pool) is None:
        return [
            "The provider has no POSIX ID pool with a GID range: project groups "
            "get no GID until one exists."
        ]
    if not pool.manages(posix_ids.GROUP_GID):
        return [
            "The provider's POSIX ID pool has no group GID range: project groups "
            "would take GIDs from the range shared with users' primary GIDs. Set "
            "the group range before enabling project groups."
        ]
    return []


def is_valid_name(name: str) -> bool:
    return bool(NAME_RE.fullmatch(name or ""))


def _base_name(project) -> str:
    """A valid group name derived from the project slug."""
    name = _INVALID_NAME_CHARS.sub("-", (project.slug or "").lower()).strip("-")
    name = name[:NAME_MAX_LENGTH]
    if not name or not name[0].isalpha():
        # Non-ASCII names slugify to nothing or to a leading digit or dash.
        return f"p{project.uuid.hex[:8]}"
    return name


def _free_name(service_provider, project) -> str:
    """The base name, or the first ``<base>-N`` free at the provider."""
    base = _base_name(project)
    taken = {
        name.lower()
        for name in models.ServiceProviderProjectGroup.objects.filter(
            service_provider=service_provider, name__istartswith=base[:8]
        ).values_list("name", flat=True)
    }
    name = base
    suffix = 1
    while name in taken:
        suffix += 1
        tail = f"-{suffix}"
        name = base[: NAME_MAX_LENGTH - len(tail)].rstrip("-") + tail
    return name


def get_or_create_group(service_provider, project, name=None):
    """The provider's group for ``project``, created when missing.

    Returns ``(group, created)``. The name is fixed here: ``name`` when given
    (raises :class:`GroupNameTaken` when the provider already uses it),
    otherwise derived from the project slug, with a ``-N`` suffix when another
    group holds it.
    """
    groups = models.ServiceProviderProjectGroup.objects.filter(
        service_provider=service_provider
    )
    for _attempt in range(5):
        group = groups.filter(project=project).first()
        if group is not None:
            return group, False
        candidate = name or _free_name(service_provider, project)
        if name and groups.filter(name__iexact=name).exists():
            raise GroupNameTaken(name)
        try:
            with transaction.atomic():
                group = models.ServiceProviderProjectGroup.objects.create(
                    service_provider=service_provider,
                    project=project,
                    name=candidate,
                )
        except IntegrityError:
            # Lost a race: either the project's group or the name was created
            # concurrently. The next round sees which.
            continue
        logger.info(
            "Created POSIX project group %s for project %s at %s.",
            group.name,
            project,
            service_provider,
        )
        return group, True
    raise IntegrityError(
        f"Could not create a POSIX project group for project {project.pk} at "
        f"{service_provider}."
    )


def build_messages(service_provider, payload: dict) -> list[dict]:
    """``service_provider_project_group`` messages for one provider.

    A project group belongs to the provider, so the event is anchored on the
    provider's customer **and** on each of its live offerings with offering
    users. Site agents bind their queue to their offering, not to the
    customer, and a group is written to a directory those offerings share:
    anchoring on the customer alone would reach none of them.
    """
    customer = service_provider.customer
    offering_ct = ContentType.objects.get_for_model(models.Offering)
    scope_keys = permission_utils.scope_keys_for(customer) + [
        (offering_ct.id, offering_id)
        for offering_id in models.Offering.objects.filter(
            customer=customer, type__in=OFFERING_USER_ALLOWED_OFFERING_TYPES
        )
        .exclude(state=models.Offering.States.ARCHIVED)
        .values_list("id", flat=True)
    ]

    def _build_payload():
        message = dict(payload)
        message["service_provider_uuid"] = service_provider.uuid.hex
        message["customer_uuid"] = customer.uuid.hex
        message["object_type"] = (
            ObservableObjectType.SERVICE_PROVIDER_PROJECT_GROUP.value
        )
        return message

    return event_dispatch.build_messages(
        scope_keys,
        _build_payload,
        ObservableObjectType.SERVICE_PROVIDER_PROJECT_GROUP,
        include_global=True,
    ).messages


def publish(service_provider, payload: dict) -> None:
    """Send a project group event once the current transaction commits.

    The messages are built now, while the provider's offerings can still be
    read; nothing is sent if the transaction rolls back.
    """
    if get_skip_side_effects():
        return
    messages = build_messages(service_provider, payload)
    if messages:
        transaction.on_commit(partial(logging_tasks.publish_messages.delay, messages))


def change_payload(group, action: str, changed_fields=None) -> dict:
    """The wire payload for one project group change.

    ``create`` means the group became writable -- it has a GID for the first
    time, whether it was created with one or numbered later; ``update`` that
    a numbered group's name or GID changed (``gid`` is None when the GID was
    cleared by hand); ``delete`` that it is gone.
    """
    # An importer may save a group with its UUID still a string.
    payload = {
        "action": action,
        "project_group_uuid": uuid.UUID(str(group.uuid)).hex,
        "project_uuid": group.project.uuid.hex if group.project_id else None,
        "name": group.name,
        "gid": group.gid,
    }
    if changed_fields is not None:
        payload["changed_fields"] = sorted(changed_fields)
    return payload


def publish_change(group, action: str, changed_fields=None) -> None:
    """Announce a change to ``group`` to its provider's offerings."""
    publish(group.service_provider, change_payload(group, action, changed_fields))


def allocate_gid(group) -> int | None:
    """Give ``group`` a GID from the provider's pool if it has none yet."""
    if group.gid is not None:
        return group.gid
    try:
        gid = posix_ids.allocate_project_group_gid(group)
    except posix_ids.PosixIdPoolExhausted:
        logger.warning(
            "The POSIX ID pool of %s is exhausted; project group %s has no GID.",
            group.service_provider,
            group.name,
        )
        return None
    if gid is None:
        logger.info(
            "No POSIX ID pool with a GID range for %s; project group %s has no "
            "GID yet.",
            group.service_provider,
            group.name,
        )
        return None
    # Conditional, so that a pin that landed meanwhile is not overwritten.
    updated = models.ServiceProviderProjectGroup.objects.filter(
        pk=group.pk, gid__isnull=True
    ).update(gid=gid, modified=timezone.now())
    if updated:
        group.gid = gid
        group.tracker.set_saved_fields(fields=["gid"])
        # Numbered without a save, so no post_save announces it.
        publish_change(group, "create")
    else:
        group.refresh_from_db(fields=["gid", "modified"])
    return group.gid


def run_safely(func, *args) -> None:
    """Run an on-commit callback without letting it break the caller.

    The callbacks run after the resource, order or move has been committed;
    a failure here must not turn that request into an error.
    """
    try:
        func(*args)
    except Exception:
        logger.exception("Provider project group update failed: %s%r", func, args)


def ensure_group(service_provider, project):
    """Create the project's group at the provider if needed and give it a GID."""
    group, _ = get_or_create_group(service_provider, project)
    allocate_gid(group)
    return group


def ensure_group_for_resource(resource) -> None:
    """Make sure the resource's project has a group at the offering's provider."""
    # Cheapest checks first: offerings without POSIX accounts -- OpenStack
    # tenant instances and volumes, support offerings -- never get further.
    if not offering_qualifies(resource.offering):
        return
    provider = resource.offering.service_provider
    if provider is None or not provider.project_groups_enabled:
        return
    if not resource_counts(resource):
        return
    ensure_group(provider, resource.project)


def ensure_groups_for_offering(offering) -> None:
    """Groups for every project using ``offering``, e.g. after it moved provider."""
    if not offering_qualifies(offering):
        return
    provider = offering.service_provider
    if provider is None or not provider.project_groups_enabled:
        return
    project_ids = (
        active_resources()
        .filter(offering=offering)
        .values_list("project_id", flat=True)
        .distinct()
    )
    for project in structure_models.Project.available_objects.filter(
        id__in=project_ids
    ).order_by("id"):
        ensure_group(provider, project)


def register_existing_gids(pool) -> int:
    """Reserve in a new provider pool the GIDs its groups already carry.

    A group keeps the GID it was given even when the pool that supplied it is
    gone; the new pool must never hand that GID to anyone else.
    """
    if not pool.service_provider_id:
        return 0
    group_ct = ContentType.objects.get_for_model(models.ServiceProviderProjectGroup)
    registered = 0
    groups = models.ServiceProviderProjectGroup.objects.filter(
        service_provider_id=pool.service_provider_id, gid__isnull=False
    )
    for group in groups.order_by("id"):
        active = models.PosixIdentity.objects.filter(
            pool=pool, released_at__isnull=True
        )
        if active.filter(content_type=group_ct, object_id=group.pk).exists():
            continue
        if active.filter(gid=group.gid).exists():
            logger.warning(
                "GID %s of project group %s is already held in %s; not registered.",
                group.gid,
                group.name,
                pool,
            )
            continue
        models.PosixIdentity.objects.create(
            pool=pool, content_type=group_ct, object_id=group.pk, gid=group.gid
        )
        registered += 1
    return registered


def backfill(service_provider, dry_run=False) -> list[dict]:
    """Create the groups of projects already using the provider, allocate GIDs.

    Projects using the provider get a group; every group without a GID gets
    one. Pinned GIDs are left alone and skipped by the allocator. Returns one
    row per group touched; a second run returns nothing.
    """
    rows = []
    project_ids = (
        active_resources()
        .filter(offering__customer_id=service_provider.customer_id)
        .values_list("project_id", flat=True)
        .distinct()
    )
    existing = set(
        models.ServiceProviderProjectGroup.objects.filter(
            service_provider=service_provider, project__isnull=False
        ).values_list("project_id", flat=True)
    )
    projects = structure_models.Project.available_objects.filter(
        id__in=project_ids
    ).exclude(id__in=existing)
    for project in projects.order_by("id"):
        if dry_run:
            rows.append(
                {
                    "project": project,
                    "name": _free_name(service_provider, project),
                    "gid": None,
                    "action": "create",
                }
            )
            continue
        group = ensure_group(service_provider, project)
        rows.append(
            {
                "project": project,
                "name": group.name,
                "gid": group.gid,
                "action": "create",
            }
        )

    unnumbered = models.ServiceProviderProjectGroup.objects.filter(
        service_provider=service_provider, gid__isnull=True
    ).select_related("project")
    for group in unnumbered.order_by("id"):
        gid = None if dry_run else allocate_gid(group)
        if not dry_run and gid is None:
            continue
        rows.append(
            {
                "project": group.project,
                "name": group.name,
                "gid": gid,
                "action": "allocate",
            }
        )
    return rows


def describe(groups) -> dict:
    """``{group.pk: {"offerings": [...], "members": [...]}}`` in a few queries.

    ``offerings`` are the provider's offerings the project uses; ``members``
    the sorted usernames, at the provider, of the active users holding an
    unexpired role in the project. A username counts while at least one of the
    user's live accounts carrying it is not restricted.
    """
    groups = list(groups)
    details = {group.pk: {"offerings": [], "members": []} for group in groups}
    project_ids = {group.project_id for group in groups if group.project_id}
    if not project_ids:
        return details
    customer_ids = {group.service_provider.customer_id for group in groups}

    offerings = defaultdict(dict)
    for row in (
        active_resources()
        .filter(project_id__in=project_ids, offering__customer_id__in=customer_ids)
        .values(
            "project_id", "offering__customer_id", "offering__uuid", "offering__name"
        )
        .distinct()
    ):
        key = (row["project_id"], row["offering__customer_id"])
        offerings[key][row["offering__uuid"].hex] = row["offering__name"]

    project_ct = ContentType.objects.get_for_model(structure_models.Project)
    project_users = defaultdict(set)
    for row in (
        UserRole.objects.filter(
            is_active=True,
            content_type=project_ct,
            object_id__in=project_ids,
            user__is_active=True,
        )
        .filter(Q(expiration_time__isnull=True) | Q(expiration_time__gt=timezone.now()))
        .values("object_id", "user_id")
    ):
        project_users[row["object_id"]].add(row["user_id"])
    user_ids = {uid for uids in project_users.values() for uid in uids}

    usernames = defaultdict(set)
    for row in (
        models.OfferingUser.objects.filter(
            user_id__in=user_ids,
            offering__customer_id__in=customer_ids,
            state__in=OfferingUserStates.LIVE_STATES,
            is_restricted=False,
        )
        .exclude(username__isnull=True)
        .exclude(username="")
        .values("user_id", "offering__customer_id", "username")
    ):
        usernames[(row["user_id"], row["offering__customer_id"])].add(row["username"])

    for group in groups:
        if not group.project_id:
            continue
        customer_id = group.service_provider.customer_id
        in_project = offerings.get((group.project_id, customer_id), {})
        details[group.pk]["offerings"] = [
            {"uuid": uuid, "name": name}
            for uuid, name in sorted(in_project.items(), key=lambda item: item[1])
        ]
        members = set()
        for user_id in project_users.get(group.project_id, ()):
            members |= usernames.get((user_id, customer_id), set())
        details[group.pk]["members"] = sorted(members)
    return details
