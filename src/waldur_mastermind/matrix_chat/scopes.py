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
from django.db.models import Q
from django.db.models.signals import pre_delete
from django.urls import NoReverseMatch, reverse

from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole

if TYPE_CHECKING:
    from django.db.models import Model, QuerySet
    from rest_framework.request import Request

    from .models import MatrixRoom


@dataclasses.dataclass(frozen=True)
class RoomScope:
    """How matrix_chat treats the rooms of one model."""

    # The users who belong in the scope's room now. Inactive users may be in
    # it; nobody inactive is ever invited, and they lose the room.
    members: Callable[[Model], QuerySet[User]]
    # The member's power level in the room: 50 is a moderator, 0 (the
    # default) a member. Staff and support who joined with Join keep 50.
    power_level: Callable[[User, Model], int] | None = None
    # What a member added to a room that was already welcomed is announced
    # as: "<name> has been granted the <label> role." None announces nothing.
    role_label: Callable[[User, Model], str | None] | None = None
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


_registry: dict[type, RoomScope] = {}


def register(
    model: type,
    *,
    members: Callable[[Model], QuerySet[User]],
    power_level: Callable[[User, Model], int] | None = None,
    role_label: Callable[[User, Model], str | None] | None = None,
    welcome: Callable[[MatrixRoom], str] | None = None,
    name: Callable[[Model], str] | None = None,
    url: Callable[[Model, Request | None], str | None] | None = None,
    scope_ids_for_user: Callable[[User], Any] | None = None,
    close_on_delete: bool = True,
) -> RoomScope:
    """Say who belongs in the rooms of ``model``. Registering it again
    replaces the earlier registration.

    Deleting an object of ``model`` closes its room: its members are removed,
    its history exported when history export is on, and it is archived.
    ``close_on_delete=False`` leaves that to the app, as projects do with
    their own handler.
    """
    spec = RoomScope(
        members=members,
        power_level=power_level,
        role_label=role_label,
        welcome=welcome,
        name=name,
        url=url,
        scope_ids_for_user=scope_ids_for_user,
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
