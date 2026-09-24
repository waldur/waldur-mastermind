from io import StringIO

from django.core.management import call_command
from django.test import TestCase

from waldur_core.core.management.commands.find_username_collisions import (
    find_collisions,
)
from waldur_core.core.models import User
from waldur_core.structure.tests import factories as structure_factories


def named(username):
    # Logins store identifiers as sent, so set them without validation.
    user = structure_factories.UserFactory()
    User.objects.filter(pk=user.pk).update(username=username)
    return username


class FindUsernameCollisionsTest(TestCase):
    def test_reports_case_and_character_groups(self):
        case = [named("alice@idp.example.org"), named("Alice@idp.example.org")]
        both_invalid = [named("Carol@idp"), named("CAROL@idp")]
        characters = [named("bob:1"), named("bob;1")]
        named("dave")  # no counterpart

        found = {(kind, tuple(sorted(names))) for kind, names in find_collisions()}
        self.assertEqual(
            found,
            {
                ("case", tuple(sorted(case))),
                ("case", tuple(sorted(both_invalid))),
                ("characters", tuple(sorted(characters))),
            },
        )

    def test_writes_nothing(self):
        named("alice")
        named("Alice")
        before = list(User.all_objects.values_list("id", "username", "is_active"))
        out = StringIO()
        call_command("find_username_collisions", stdout=out)
        self.assertIn("1 group(s)", out.getvalue())
        self.assertEqual(
            before, list(User.all_objects.values_list("id", "username", "is_active"))
        )

    def test_reports_none(self):
        named("alice")
        out = StringIO()
        call_command("find_username_collisions", stdout=out)
        self.assertIn("No colliding usernames.", out.getvalue())
