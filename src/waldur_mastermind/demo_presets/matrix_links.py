"""Keep Waldur's links to Matrix accounts and rooms across a demo preset load.

A load that cleans up first deletes every user, and with them the Matrix
profiles, rooms and room memberships Waldur keeps. The homeserver keeps the
accounts and rooms. A profile is Waldur's only record that it created an
account, so without one it refuses to provision the user again ("already
belongs to an account this Waldur did not create"), and a project's room is
created anew, without its alias, which the old room still holds.

So the links are copied before the cleanup and put back after the import, for
whatever the new data has again: a user by username, a room's scope by its
model and UUID (presets give their objects fixed UUIDs). The rest is dropped,
as before. The new data's user of the same name gets the account, which is what
single sign-on does anyway: the identity provider signs that username in to it.
A membership comes back only for a user who still belongs in the room, by the
rule the member sync uses; the sync queued after the load invites the rest.

matrix_chat is an optional extension, so its models and tasks are looked up
when used rather than imported at startup (see "Lazy imports for heavy
optional dependencies" in CLAUDE.md).
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.apps import apps
from django.contrib.contenttypes.models import ContentType
from django.db import transaction

from waldur_core.core.models import User
from waldur_core.permissions.models import UserRole

MATRIX_APP = "waldur_mastermind.matrix_chat"


@dataclass
class MatrixLinks:
    profiles: list[dict] = field(default_factory=list)
    rooms: list[dict] = field(default_factory=list)
    members: list[dict] = field(default_factory=list)


def _models():
    return (
        apps.get_model("matrix_chat", "MatrixUserProfile"),
        apps.get_model("matrix_chat", "MatrixRoom"),
        apps.get_model("matrix_chat", "MatrixRoomMember"),
    )


def _values(obj, exclude) -> dict:
    """Field values to create the row again with. An encrypted field reads
    decrypted and is encrypted again on save."""
    return {
        f.attname: getattr(obj, f.attname)
        for f in obj._meta.concrete_fields
        if f.name not in exclude and not f.primary_key
    }


def _on_homeserver(queryset, prefix=""):
    """Rooms the homeserver has: one still being created has nothing there."""
    return queryset.exclude(**{f"{prefix}room_id__isnull": True}).exclude(
        **{f"{prefix}room_id": ""}
    )


def snapshot() -> MatrixLinks | None:
    """Copies of the Matrix profiles, rooms and memberships, before a cleanup."""
    if not apps.is_installed(MATRIX_APP):
        return None
    profile_model, room_model, member_model = _models()
    links = MatrixLinks()
    for profile in profile_model.objects.select_related("user"):
        links.profiles.append(
            {"username": profile.user.username, "values": _values(profile, {"user"})}
        )

    rooms = list(_on_homeserver(room_model.objects.select_related("created_by")))
    pks_by_model = defaultdict(set)
    for room in rooms:
        pks_by_model[room.content_type_id].add(room.object_id)
    scope_uuids = {}
    for content_type_id, pks in pks_by_model.items():
        model = ContentType.objects.get_for_id(content_type_id).model_class()
        if model is None or not hasattr(model, "uuid"):
            continue
        for pk, uuid in model.objects.filter(pk__in=pks).values_list("pk", "uuid"):
            scope_uuids[content_type_id, pk] = str(uuid)
    for room in rooms:
        scope_uuid = scope_uuids.get((room.content_type_id, room.object_id))
        if scope_uuid is None:
            continue
        links.rooms.append(
            {
                "content_type_id": room.content_type_id,
                "scope_uuid": scope_uuid,
                "created_by": room.created_by.username if room.created_by else None,
                "values": _values(room, {"object_id", "created_by", "closing_export"}),
            }
        )

    for member in _on_homeserver(
        member_model.objects.select_related("room", "user"), prefix="room__"
    ):
        links.members.append(
            {
                "room_id": member.room.room_id,
                "username": member.user.username,
                "values": _values(member, {"room", "user"}),
            }
        )
    return links


def _belonging_user_ids(room) -> set[int]:
    """Who belongs in the room, by the rule sync_project_members_to_room uses."""
    from waldur_mastermind.matrix_chat.models import (
        get_customer_roles_in_project_rooms,
    )

    project = room.project
    if project is None:
        return set()
    direct = UserRole.objects.filter(
        scope=project, is_active=True, user__is_active=True
    ).values_list("user_id", flat=True)
    via_customer = (
        get_customer_roles_in_project_rooms(project.customer)
        .filter(user__is_active=True)
        .values_list("user_id", flat=True)
    )
    return set(direct) | set(via_customer)


def restore(links: MatrixLinks | None, output) -> list[str]:
    """Put back the links whose user or scope the new data has again.

    Returns the UUIDs of the rooms kept, for a member sync once the load is
    done.
    """
    if links is None:
        return []
    profile_model, room_model, member_model = _models()
    usernames = (
        {p["username"] for p in links.profiles}
        | {m["username"] for m in links.members}
        | {r["created_by"] for r in links.rooms if r["created_by"]}
    )
    users = {u.username: u for u in User.objects.filter(username__in=usernames)}

    uuids_by_model = defaultdict(set)
    for room in links.rooms:
        uuids_by_model[room["content_type_id"]].add(room["scope_uuid"])
    scopes = {}
    for content_type_id, uuids in uuids_by_model.items():
        model = ContentType.objects.get_for_id(content_type_id).model_class()
        if model is None:
            continue
        for scope in model.objects.filter(uuid__in=uuids):
            scopes[content_type_id, str(scope.uuid)] = scope

    with transaction.atomic():
        profiles = 0
        for profile in links.profiles:
            user = users.get(profile["username"])
            matrix_user_id = profile["values"]["matrix_user_id"]
            if user is None or (
                profile_model.objects.filter(user=user).exists()
                or profile_model.objects.filter(matrix_user_id=matrix_user_id).exists()
            ):
                continue
            profile_model.objects.create(user=user, **profile["values"])
            profiles += 1

        kept, recreated, dropped = {}, 0, 0
        for room in links.rooms:
            content_type_id = room["content_type_id"]
            scope = scopes.get((content_type_id, room["scope_uuid"]))
            room_id = room["values"]["room_id"]
            # A cleanup that kept the users kept their rooms too.
            survivor = room_model.objects.filter(room_id=room_id).first()
            if scope is None:
                if survivor is not None:
                    survivor.delete()
                    dropped += 1
                continue
            other = room_model.objects.filter(
                content_type_id=content_type_id, object_id=scope.pk
            )
            if survivor is not None:
                other = other.exclude(pk=survivor.pk)
            other = other.first()
            if other is not None:
                if other.room_id:
                    # The scope has a room of its own on the homeserver: keep it.
                    continue
                # One the import has only started to create
                # (MATRIX_AUTO_CREATE_PROJECT_ROOMS): the kept room replaces it,
                # and its queued create_room finds nothing to do.
                other.delete()
            if survivor is not None:
                if survivor.object_id != scope.pk:
                    survivor.object_id = scope.pk
                    survivor.save(update_fields=["object_id"])
                kept[room_id] = survivor
                continue
            creator = users.get(room["created_by"]) if room["created_by"] else None
            kept[room_id] = room_model.objects.create(
                object_id=scope.pk, created_by=creator, **room["values"]
            )
            recreated += 1

        belonging = {}
        members = 0
        for member in links.members:
            room = kept.get(member["room_id"])
            user = users.get(member["username"])
            if room is None or user is None:
                continue
            if room.pk not in belonging:
                belonging[room.pk] = _belonging_user_ids(room)
            if user.id not in belonging[room.pk]:
                continue
            if member_model.objects.filter(room=room, user=user).exists():
                continue
            member_model.objects.create(room=room, user=user, **member["values"])
            members += 1

    if profiles or kept or members or dropped:
        output.write(
            f"Kept the Matrix links of {profiles} user(s) and {len(kept)} room(s) "
            f"({recreated} restored, {len(kept) - recreated} still there), "
            f"with {members} membership(s); dropped {dropped} room(s) whose "
            "scope is gone.\n"
        )
    return [room.uuid.hex for room in kept.values()]


def queue_member_syncs(room_uuids: list[str]) -> None:
    """Invite whoever the new data puts in a kept room, once the load is done
    (a load queues nothing while it runs)."""
    if not room_uuids or not apps.is_installed(MATRIX_APP):
        return
    from waldur_mastermind.matrix_chat import matrix_client, tasks

    if not matrix_client.is_enabled():
        return
    for room_uuid in room_uuids:
        tasks.sync_project_members_to_room.delay(room_uuid)
