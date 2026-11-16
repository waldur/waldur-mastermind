"""Who belongs in the Matrix room of each kind of scope.

A ``MatrixRoom`` is attached to any object through ``MatrixRoom.scope``, one
room per object. What matrix_chat cannot know by itself is who belongs in the
room of an object from another app. An app that gives its objects rooms says so
here, from its ``AppConfig.ready()``, so that matrix_chat never imports the app::

    from waldur_mastermind.matrix_chat import scopes

    scopes.register(
        PanelMeeting,
        members=panel_meeting_room_members,
        power_level=panel_meeting_power_level,
        role_label=panel_meeting_role_label,
    )

Member sync (``tasks.sync_room_members``), ``models.has_room_role`` and so
everything that keeps or takes away room access, power levels, the room list
and the room serializer all read the registration. Deleting a registered
object closes its room (``handlers.close_deleted_scope_room``). A model nobody
registered, such as the customer of a legacy organization room, is never
synced; access checks keep the old rule for it: whoever holds an active role
on the object itself belongs in its room, as a moderator never.

Projects register here too (``apps.MatrixChatConfig.ready``), with the rule
their rooms have always had.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from django.contrib.contenttypes.models import ContentType
from django.contrib.contenttypes.prefetch import GenericPrefetch
from django.db.models import Q
from django.db.models.signals import pre_delete
from django.urls import NoReverseMatch, reverse

from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet
    from rest_framework.request import Request

    from .models import MatrixRoom


DEFAULT_ROLE_NOTICE = "{name} has been granted the {role} role."


@dataclasses.dataclass(frozen=True)
class RoomScope:
    """How matrix_chat treats the rooms of one model."""

    # The users who belong in the scope's room now. Inactive users may be in
    # it; nobody inactive is ever invited, and they lose the room.
    members: Callable[[Model], QuerySet[User]]
    # The member's power level in the room: 50 is a moderator, 0 (the
    # default) a member. Staff and support who joined with Join keep 50.
    power_level: Callable[[User, Model], int] | None = None
    # What a member takes part as: shown beside their name in the room's
    # member list (``MatrixRoomMember.role_label``), and a member added to a
    # room that was already welcomed is announced with it (``role_notice``).
    # None: no label, and nothing announced.
    role_label: Callable[[User, Model], str | None] | None = None
    # How that announcement reads, with ``{name}`` and ``{role}``.
    role_notice: str = DEFAULT_ROLE_NOTICE
    # The text of the room's welcome, posted once its first members joined.
    welcome: Callable[[MatrixRoom], str] | None = None
    # The scope's name and API URL in the room serializer. Without them the
    # object's ``name`` and its ``<model>-detail`` route are used, and a model
    # with no such route serializes with no URL.
    name: Callable[[Model], str] | None = None
    url: Callable[[Model, Request | None], str | None] | None = None
    # The ids of the objects whose rooms ``members`` puts the user in, as a
    # QuerySet of ids or a list. A reactivated user's rooms are found by it:
    # a sync while they were inactive wrote down no membership to find them
    # by. Without it only the rooms they were in before are synced again.
    scope_ids_for_user: Callable[[User], Any] | None = None
    # The ids of the scopes whose rooms' history exports the requester may
    # download, besides staff and support: a room's whole history goes to
    # whoever runs its scope, not to its members. Called with the request, so
    # that a personal access token is held to its scopes, or with a bare
    # user. None: nobody else.
    export_readers: Callable[[Any], QuerySet] | None = None
    # The ids of the scopes that have ended, such as a closed discussion: an
    # active room of one is archived, its history exported first, once Matrix
    # chat can do it (tasks.close_rooms_of_ended_scopes). ``closing_reason``
    # is what the room is told it was closed for.
    ended: Callable[[], QuerySet] | None = None
    closing_reason: str = ""
    # Whether deleting an object closes its room (handlers.close_deleted_scope_room
    # at once, tasks.close_rooms_of_ended_scopes for one deleted while Matrix
    # chat was off). False leaves it to the app.
    close_on_delete: bool = True
    # A scope whose record keeps its room's history, such as a step
    # discussion: its room is exported whenever it is closed, whatever
    # MATRIX_HISTORY_EXPORT_ENABLED says.
    export_on_close: bool = False
    # Whether ``user``, with the support flag, may join the scope's room on
    # their own (the room's Join), as support may join any room otherwise.
    # None: they may. Staff always may.
    may_join: Callable[[User, Model], bool] | None = None
    # The ids of the scopes whose rooms ``user``, with the support flag, is
    # kept from, as a QuerySet of ids or a list: support's room list leaves
    # them out, and a support member who joined one with Join is taken out
    # at the next sync once it names them. Keep it in line with ``may_join``.
    # None: none. Staff are never kept from a room.
    kept_from: Callable[[User], Any] | None = None
    # What kind of room this is, for clients that list rooms of every kind
    # together (``MatrixRoom.scope_type``): "project", "step_discussion".
    # Defaults to the model's name.
    kind: str = ""
    # A short line saying what the room is for, under its name in a room
    # list (``MatrixRoom.scope_subtitle``). None: nothing.
    subtitle: Callable[[Model], str | None] | None = None
    # The relations ``name``, ``subtitle`` and ``url`` read, loaded with the
    # scopes of a room list at once (``scope_prefetch``) rather than room by
    # room.
    select_related: tuple[str, ...] = ()


_registry: dict[type, RoomScope] = {}


def register(
    model: type,
    *,
    members: Callable[[Model], QuerySet[User]],
    power_level: Callable[[User, Model], int] | None = None,
    role_label: Callable[[User, Model], str | None] | None = None,
    role_notice: str = DEFAULT_ROLE_NOTICE,
    welcome: Callable[[MatrixRoom], str] | None = None,
    name: Callable[[Model], str] | None = None,
    url: Callable[[Model, Request | None], str | None] | None = None,
    scope_ids_for_user: Callable[[User], Any] | None = None,
    close_on_delete: bool = True,
    export_readers: Callable[[Any], QuerySet] | None = None,
    ended: Callable[[], QuerySet] | None = None,
    closing_reason: str = "",
    export_on_close: bool = False,
    may_join: Callable[[User, Model], bool] | None = None,
    kept_from: Callable[[User], Any] | None = None,
    kind: str = "",
    subtitle: Callable[[Model], str | None] | None = None,
    select_related: tuple[str, ...] = (),
) -> RoomScope:
    """Say who belongs in the rooms of ``model``. Registering it again
    replaces the earlier registration.

    Deleting an object of ``model`` closes its room: its members are removed,
    its history exported when history export is on, and it is archived.
    ``close_on_delete=False`` leaves that to the app, as projects do with
    their own handler. ``export_on_close`` exports it whenever it closes.
    """
    spec = RoomScope(
        members=members,
        power_level=power_level,
        role_label=role_label,
        role_notice=role_notice,
        welcome=welcome,
        name=name,
        url=url,
        scope_ids_for_user=scope_ids_for_user,
        export_readers=export_readers,
        ended=ended,
        closing_reason=closing_reason,
        close_on_delete=close_on_delete,
        export_on_close=export_on_close,
        may_join=may_join,
        kept_from=kept_from,
        kind=kind or model._meta.model_name,
        subtitle=subtitle,
        select_related=tuple(select_related),
    )
    _registry[model] = spec
    uid = _deletion_uid(model)
    if close_on_delete:
        # Imported here: handlers imports models, which imports this module.
        from . import handlers

        pre_delete.connect(
            handlers.close_deleted_scope_room,
            sender=model,
            dispatch_uid=uid,
            weak=False,
        )
    else:
        pre_delete.disconnect(sender=model, dispatch_uid=uid)
    return spec


def unregister(model: type) -> None:
    _registry.pop(model, None)
    pre_delete.disconnect(sender=model, dispatch_uid=_deletion_uid(model))


def _deletion_uid(model: type) -> str:
    return f"waldur_mastermind.matrix_chat.close_room_of_{model._meta.label_lower}"


def get(scope: Any) -> RoomScope | None:
    """The registration of an object, or of a model; None if there is none."""
    model = scope if isinstance(scope, type) else type(scope)
    for cls in model.__mro__:
        if cls in _registry:
            return _registry[cls]
    return None


def is_registered(scope: Any) -> bool:
    return get(scope) is not None


def registered_content_types() -> list[ContentType]:
    return [ContentType.objects.get_for_model(model) for model in _registry]


def registrations() -> list[tuple[type, RoomScope]]:
    return list(_registry.items())


def export_reader_content_types() -> list[ContentType]:
    """The kinds of scope that say who downloads their rooms' exports
    (``export_readers``): support reads those by that rule too."""
    return [
        ContentType.objects.get_for_model(model)
        for model, spec in _registry.items()
        if spec.export_readers is not None
    ]


def may_join(user: User, scope: Any) -> bool:
    """Whether ``user`` may join the scope's room on their own; the scope's
    ``may_join`` decides for a registered kind of scope that has one."""
    if user.is_staff:
        return True
    spec = get(scope) if scope is not None else None
    if spec is None or spec.may_join is None:
        return True
    return spec.may_join(user, scope)


def scope_prefetch() -> GenericPrefetch:
    """A prefetch of ``MatrixRoom.scope`` for a list of rooms: each kind of
    scope in one query, with the relations its registration names."""
    return GenericPrefetch(
        "scope",
        [
            model._default_manager.select_related(*spec.select_related)
            for model, spec in _registry.items()
            if spec.select_related
        ],
    )


def rooms_kept_from(user: User) -> Q | None:
    """A lookup on ``MatrixRoom`` for the rooms of registered scopes that
    keep ``user``, with the support flag, out (``kept_from``); None when no
    registration says. Staff are kept from none."""
    if user.is_staff:
        return None
    lookup = None
    for model, spec in _registry.items():
        if spec.kept_from is None:
            continue
        q = Q(
            content_type=ContentType.objects.get_for_model(model),
            object_id__in=spec.kept_from(user),
        )
        lookup = q if lookup is None else lookup | q
    return lookup


def readable_export_rooms(request: Any) -> Q | None:
    """The rooms of registered scopes whose exports the request -- or a bare
    user -- may download through their scope's ``export_readers``, as a
    lookup on the export's room; None when no scope says anyone may."""
    lookups = None
    for model, spec in _registry.items():
        if spec.export_readers is None:
            continue
        lookup = Q(
            room__content_type=ContentType.objects.get_for_model(model),
            room__object_id__in=spec.export_readers(request),
        )
        lookups = lookup if lookups is None else lookups | lookup
    return lookups


def ended_scopes() -> list[tuple[ContentType, QuerySet, str]]:
    """``(content type, ids of its ended scopes, closing reason)`` of each
    registered kind of scope that says when one ends."""
    return [
        (ContentType.objects.get_for_model(model), spec.ended(), spec.closing_reason)
        for model, spec in _registry.items()
        if spec.ended is not None
    ]


def members(scope: Model) -> QuerySet[User]:
    """The users who belong in the scope's room, inactive ones included.

    For a scope nobody registered, whoever holds an active role on it: the
    rule access checks have always applied to such rooms. Member sync never
    syncs them by it.
    """
    spec = get(scope)
    if spec is not None:
        return spec.members(scope)
    return User.all_objects.filter(
        id__in=UserRole.objects.filter(scope=scope, is_active=True).values("user_id")
    )


def rooms_of_user(user: User) -> Q | None:
    """A lookup on ``MatrixRoom`` for the rooms of registered scopes that say,
    through ``scope_ids_for_user``, they put the user in; None when no
    registration says."""
    lookup = None
    for model, spec in _registry.items():
        if spec.scope_ids_for_user is None:
            continue
        q = Q(
            content_type=ContentType.objects.get_for_model(model),
            object_id__in=spec.scope_ids_for_user(user),
        )
        lookup = q if lookup is None else lookup | q
    return lookup


def power_level(user: User, scope: Model) -> int:
    spec = get(scope)
    if spec is None or spec.power_level is None:
        return 0
    return spec.power_level(user, scope)


def role_label(user: User, scope: Model) -> str | None:
    spec = get(scope)
    if spec is None or spec.role_label is None:
        return None
    return spec.role_label(user, scope)


def role_notice(scope: Model | None) -> str:
    spec = get(scope) if scope is not None else None
    return spec.role_notice if spec is not None else DEFAULT_ROLE_NOTICE


def welcome(room: MatrixRoom) -> str | None:
    spec = get(room.scope)
    if spec is None or spec.welcome is None:
        return None
    return spec.welcome(room)


def scope_name(scope: Model) -> str | None:
    spec = get(scope)
    if spec is not None and spec.name is not None:
        return spec.name(scope)
    return getattr(scope, "name", None)


def scope_type(scope: Model) -> str | None:
    """The kind of room the scope has, or None for an unregistered scope."""
    spec = get(scope)
    return spec.kind if spec is not None else None


def scope_subtitle(scope: Model) -> str | None:
    spec = get(scope)
    if spec is None or spec.subtitle is None:
        return None
    return spec.subtitle(scope)


def content_types_of_kind(kind: str) -> list[ContentType]:
    """The content types of the scopes registered as ``kind``."""
    return [
        ContentType.objects.get_for_model(model)
        for model, spec in _registry.items()
        if spec.kind == kind
    ]


def scope_url(scope: Model, request: Request | None) -> str | None:
    """The scope's API URL, or None for a kind of object with no API route."""
    spec = get(scope)
    if spec is not None and spec.url is not None:
        return spec.url(scope, request)
    # The view name GenericRelatedField reverses, so a project's URL is the
    # same as it always was.
    get_url_name = getattr(type(scope), "get_url_name", None)
    url_name = get_url_name() if get_url_name else type(scope).__name__.lower()
    uuid = getattr(scope, "uuid", None)
    lookup = {"uuid": uuid} if uuid is not None else {"pk": scope.pk}
    try:
        path = reverse(f"{url_name}-detail", kwargs=lookup)
    except NoReverseMatch:
        return None
    return request.build_absolute_uri(path) if request is not None else path
