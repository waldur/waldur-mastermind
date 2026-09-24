"""Report accounts that a login and SCIM provisioning could confuse.

Usernames that differ only in case, or only by characters a username should not
hold (outside ``[0-9a-z_.@+-]``), may belong to one person: before SCIM kept
match values as sent, it stored them lowercased and stripped, while a login
stores the identifier exactly as the identity provider sends it. This command
lists such groups for an operator to merge by hand. It writes nothing.
"""

from django.core.management.base import BaseCommand
from django.db.models import Count, F, Func, Value
from django.db.models.functions import Lower

from waldur_core.core.models import User

INVALID_USERNAME_CHARS = r"[^0-9a-z_.@+-]"


def collision_key():
    """The username lowercased, with characters a username may not hold removed."""
    return Func(
        Lower("username"),
        Value(INVALID_USERNAME_CHARS),
        Value(""),
        Value("g"),
        function="regexp_replace",
    )


def find_collisions() -> list[tuple[str, list[str]]]:
    """``(kind, usernames)`` per group; kind is ``case`` or ``characters``."""
    accounts = User.all_objects.annotate(key=collision_key())
    keys = (
        accounts.values("key")
        .annotate(count=Count("id"))
        .filter(count__gt=1)
        .values_list("key", flat=True)
    )
    groups: dict[str, list[str]] = {}
    for key, username in (
        accounts.filter(key__in=keys)
        .order_by(F("key"), "id")
        .values_list("key", "username")
    ):
        groups.setdefault(key, []).append(username)
    return [
        (
            "case" if len({name.lower() for name in names}) == 1 else "characters",
            names,
        )
        for names in groups.values()
    ]


class Command(BaseCommand):
    help = (
        "List accounts whose usernames differ only in case or in characters a "
        "username should not hold, so that one person may hold two of them. "
        "Writes nothing."
    )

    def handle(self, *args, **options):
        collisions = find_collisions()
        if not collisions:
            self.stdout.write("No colliding usernames.")
            return
        for kind, names in collisions:
            self.stdout.write(f"{kind:10} {', '.join(names)}")
        self.stdout.write(
            f"{len(collisions)} group(s). Merge them by hand; nothing was changed."
        )
