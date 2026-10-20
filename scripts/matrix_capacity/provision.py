#!/usr/bin/env python
"""Create, or remove, the synthetic users and rooms of the Matrix capacity test.

For a TEST deployment only: `create` writes an organization, projects, users,
rooms and Matrix accounts into the Waldur and the homeserver it runs against.
`cleanup` removes from Waldur exactly what `create` made, found by the prefix
and a marker that only this script sets.

Runs where Waldur runs, e.g. in the API container:

    python /usr/src/waldur/scripts/matrix_capacity/provision.py create \\
        --prefix cap --homeserver-domain matrix.test.example.org \\
        --users 1000 --out /tmp/sessions.jsonl

    python /usr/src/waldur/scripts/matrix_capacity/provision.py cleanup \\
        --prefix cap --homeserver-domain matrix.test.example.org [--yes]

See README.md next to this file.
"""

import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import time
from unittest import mock

import django

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "waldur_core.server.settings")
django.setup()

from django.contrib.contenttypes.models import ContentType
from django.db import connection

from waldur_core.configuration import config
from waldur_core.core.models import User
from waldur_core.permissions.fixtures import ProjectRole
from waldur_core.structure.models import Customer, Project
from waldur_mastermind.matrix_chat import matrix_client, models, tasks

# Set on every user, organization and project this script creates; cleanup
# deletes nothing without it, so a real user who happens to match the naming
# pattern is never touched.
MARKER = "matrix_capacity_test"
PREFIX_RE = re.compile(r"^[a-z][a-z0-9]{2,15}$")

t0 = time.time()


def log(msg):
    print(f"[{time.time() - t0:7.1f}s] {msg}", flush=True)


def scope_marker(prefix):
    return (
        f"{MARKER}={prefix}: synthetic, made by "
        "scripts/matrix_capacity/provision.py. Remove with its cleanup command."
    )


def customer_name(prefix):
    return f"{prefix}-capacity"


def project_name(prefix, j):
    return f"{prefix}-p{j:04d}"


def username(prefix, i):
    return f"{prefix}-u{i:05d}"


def marked_customer(prefix):
    return Customer.objects.filter(
        name=customer_name(prefix), description=scope_marker(prefix)
    ).first()


def marked_projects(prefix, customer):
    return Project.objects.filter(
        customer=customer,
        name__regex=rf"^{prefix}-p[0-9]{{4}}$",
        description=scope_marker(prefix),
    )


def marked_users(prefix):
    # all_objects: User.objects leaves out deactivated users.
    return User.all_objects.filter(
        username__regex=rf"^{prefix}-u[0-9]{{5}}$", **{f"details__{MARKER}": prefix}
    )


def check_deployment(args):
    if not PREFIX_RE.match(args.prefix):
        sys.exit("--prefix: 3-16 lowercase letters and digits, starting with a letter")
    # Cleanup still works with chat switched off; it then skips the homeserver.
    if args.command == "create" and not matrix_client.is_enabled():
        sys.exit("Matrix chat is not enabled on this deployment")
    if args.command == "create" and config.MATRIX_AUTO_CREATE_PROJECT_ROOMS:
        sys.exit(
            "MATRIX_AUTO_CREATE_PROJECT_ROOMS is on: Celery would create a room "
            "for every project this script makes, racing the script's own room "
            "creation, and leave duplicate rooms or rooms in ERROR. Switch it "
            "off for the test."
        )
    # Typing the domain is the confirmation that this is the deployment meant.
    if args.homeserver_domain != config.MATRIX_HOMESERVER_DOMAIN:
        sys.exit(
            f"--homeserver-domain {args.homeserver_domain!r} is not this "
            f"deployment's MATRIX_HOMESERVER_DOMAIN {config.MATRIX_HOMESERVER_DOMAIN!r}"
        )


def refuse_unmarked(prefix, n_projects, n_users):
    """Stop before writing anything if a name this run would use belongs to
    something the script did not make."""
    marker = scope_marker(prefix)
    customer = Customer.objects.filter(name=customer_name(prefix)).first()
    if customer and customer.description != marker:
        sys.exit(
            f"Organization {customer.name!r} exists and was not made by this script"
        )
    if customer:
        names = [project_name(prefix, j) for j in range(n_projects)]
        foreign = (
            Project.objects.filter(customer=customer, name__in=names)
            .exclude(description=marker)
            .values_list("name", flat=True)
        )
        if foreign:
            sys.exit(f"Projects not made by this script: {sorted(foreign)[:5]}")
    names = [username(prefix, i) for i in range(n_users)]
    # Not .exclude() on the marker: a user without the key gives NULL there,
    # and NOT NULL leaves the user out of the result.
    existing = User.all_objects.filter(username__in=names)
    foreign = set(existing.values_list("username", flat=True)) - set(
        marked_users(prefix).values_list("username", flat=True)
    )
    if foreign:
        sys.exit(f"Users not made by this script: {sorted(foreign)[:5]}")


def make_room(project, project_ct):
    room = models.MatrixRoom.objects.filter(
        content_type=project_ct, object_id=project.id
    ).first()
    if room is None:
        room = models.MatrixRoom.objects.create(
            room_name=project.name, content_type=project_ct, object_id=project.id
        )
    if room.state == models.RoomStates.ERROR:
        room.retry_creating()
        room.save(update_fields=["state", "error_message"])
    if room.state == models.RoomStates.CREATING:
        # The member sync runs right below, not on Celery.
        with mock.patch.object(tasks.sync_project_members_to_room, "delay"):
            tasks.create_room(str(room.uuid))
        room.refresh_from_db()
        tasks.sync_project_members_to_room(str(room.uuid))
    return room


def create(args):
    prefix = args.prefix
    marker = scope_marker(prefix)
    n_projects = (args.users + args.room_size - 1) // args.room_size
    refuse_unmarked(prefix, n_projects, args.users)

    customer, _ = Customer.objects.get_or_create(
        name=customer_name(prefix),
        defaults={"abbreviation": prefix, "description": marker},
    )
    projects = []
    for j in range(n_projects):
        project, _ = Project.available_objects.get_or_create(
            customer=customer,
            name=project_name(prefix, j),
            defaults={"description": marker},
        )
        projects.append(project)
    log(f"{len(projects)} projects in {customer.name}")

    created_users = 0
    for i in range(args.users):
        name = username(prefix, i)
        user, created = User.all_objects.get_or_create(
            username=name,
            defaults={
                "email": f"{name}@capacity-test.invalid",
                "details": {MARKER: prefix},
            },
        )
        created_users += created
        project = projects[i // args.room_size]
        if not project.has_user(user, ProjectRole.MEMBER):
            project.add_user(user, ProjectRole.MEMBER)
    log(f"{args.users} users ({created_users} new)")

    # One room at a time, as a precaution: room version 12 derives the room ID
    # from the create event, and before Waldur made each create event unique,
    # concurrent createRoom calls by the bot could get the same room ID.
    project_ct = ContentType.objects.get_for_model(Project)
    rooms = {}
    failed = []
    for project in projects:
        room = make_room(project, project_ct)
        rooms[project.id] = room.room_id
        if room.state != models.RoomStates.ACTIVE:
            failed.append(project.name)
            log(f"room of {project.name} is {room.state}: {room.error_message}")
    log(f"{len(rooms)} rooms")
    # A session without a room syncs but never sends, so a step would carry
    # less traffic than its user count says.
    if failed:
        sys.exit(
            f"Rooms not active: {', '.join(failed)}. No session was written; "
            "run create again to retry them."
        )

    done = set()
    if os.path.exists(args.out):
        with open(args.out) as f:
            done = {json.loads(line)["username"] for line in f}

    def make_session(i):
        try:
            name = username(prefix, i)
            if name in done:
                return None
            user = User.objects.get(username=name)
            mxid = matrix_client.ensure_user_exists(user)
            # As POST /api/matrix/session/ does for the drawer: a new device
            # with its own refresh token.
            session = matrix_client.create_web_session(mxid)
            return {
                "username": name,
                "user_id": mxid,
                "room_id": rooms[projects[i // args.room_size].id],
                "created_at": time.time(),
                **session,
            }
        finally:
            connection.close()

    # The file holds live tokens of the synthetic users.
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    new = 0
    with os.fdopen(fd, "a") as f, cf.ThreadPoolExecutor(args.threads) as pool:
        for row in pool.map(make_session, range(args.users)):
            if row:
                f.write(json.dumps(row) + "\n")
                new += 1
    log(f"{new} new sessions written to {args.out}")


def cleanup(args):
    prefix = args.prefix
    customer = marked_customer(prefix)
    projects = list(marked_projects(prefix, customer)) if customer else []
    project_ct = ContentType.objects.get_for_model(Project)
    rooms = list(
        models.MatrixRoom.objects.filter(
            content_type=project_ct, object_id__in=[p.id for p in projects]
        )
    )
    users = list(marked_users(prefix))
    others = (
        Project.objects.filter(customer=customer).exclude(
            id__in=[p.id for p in projects]
        )
        if customer
        else Project.objects.none()
    )

    print(f"Organization: {customer.name if customer else 'none'}")
    print(f"Projects:     {len(projects)}")
    print(f"Rooms:        {len(rooms)}")
    print(f"Users:        {len(users)}")
    if others.exists():
        print(
            f"The organization also holds {others.count()} projects this script "
            "did not make; they and the organization are kept."
        )
    if not args.yes:
        print("Nothing deleted. Run again with --yes to delete the above.")
        return

    archived, kept = [], []
    for room in rooms:
        if room.state in (models.RoomStates.ACTIVE, models.RoomStates.ERROR):
            room.begin_disabling()
            room.save(update_fields=["state"])
        if room.state == models.RoomStates.DISABLING:
            # Inline and with delete_history: the project deletion below would
            # otherwise queue the same task with a history export, leaving a
            # file behind.
            tasks.disable_room(
                str(room.uuid), delete_history=True, reason="capacity test clean-up"
            )
            room.refresh_from_db()
        # Without a room ID it never reached the homeserver: nothing to archive.
        if room.state == models.RoomStates.ARCHIVED or not room.room_id:
            archived.append(room)
        else:
            # Its members are still in it on the homeserver. A rerun disables
            # it again from ERROR, which needs the row and its project.
            kept.append(room)
            log(f"room {room.room_id} not archived: {room.error_message}")

    # Each deletion queues end_deleted_user_access on Celery, which signs the
    # Matrix account out, locks it and removes it from the rooms its member
    # rows still list. That covers a kick that failed while archiving, whose
    # own retry would die with the room's rows, so those go after the users.
    for user in users:
        user.delete()
    log(f"{len(users)} users deleted")
    for room in archived:
        room.delete()
    log(f"{len(archived)} rooms deleted")

    kept_projects = {room.object_id for room in kept}
    keep_customer = others.exists() or bool(kept)
    for project in projects:
        if project.id not in kept_projects:
            project.delete()
    if customer and not keep_customer:
        # Hard-deletes the soft-deleted projects with it.
        customer.delete()
    log(
        f"{len(projects) - len(kept_projects)} projects deleted, organization "
        f"{'kept' if keep_customer else 'deleted'}"
    )
    if kept:
        sys.exit(
            f"{len(kept)} rooms could not be archived; they, their projects and "
            "the organization are kept. Run cleanup again once the homeserver "
            "answers."
        )


def main():
    ap = argparse.ArgumentParser(
        description="Create or remove the synthetic users and rooms of the "
        "Matrix capacity test. For a test deployment only."
    )
    sub = ap.add_subparsers(dest="command", required=True)
    for name, help_text in (
        ("create", "create users, projects, rooms and web sessions"),
        ("cleanup", "delete what create made (lists it without --yes)"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument(
            "--prefix",
            required=True,
            help="what everything made is named after: <prefix>-capacity, "
            "<prefix>-p0000, <prefix>-u00000",
        )
        p.add_argument(
            "--homeserver-domain",
            required=True,
            help="this deployment's MATRIX_HOMESERVER_DOMAIN, as a confirmation",
        )
    p = sub.choices["create"]
    p.add_argument("--users", type=int, required=True)
    p.add_argument("--room-size", type=int, default=25, help="members per room")
    p.add_argument(
        "--out",
        required=True,
        help="JSON lines file for the sessions, appended to; users already in "
        "it get no new session",
    )
    p.add_argument(
        "--threads", type=int, default=8, help="web sessions started in parallel"
    )
    sub.choices["cleanup"].add_argument(
        "--yes", action="store_true", help="delete; without it, only list"
    )
    args = ap.parse_args()

    check_deployment(args)
    if args.command == "create":
        create(args)
    else:
        cleanup(args)


if __name__ == "__main__":
    main()
