from django.test import TestCase

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.chat.prompts.rejection import (
    CATEGORY_PHRASES,
    build_canned_rejection,
)
from waldur_mastermind.chat.tools.enums import ToolCategory


class BuildCannedRejectionTest(TestCase):
    def test_includes_organization_and_capabilities(self):
        user = structure_factories.UserFactory()
        message = build_canned_rejection(user, "MyOrg")
        self.assertIn("I can't help with that request", message)
        self.assertIn("MyOrg", message)
        for phrase in CATEGORY_PHRASES.values():
            self.assertIn(phrase, message)

    def test_staff_message_matches_end_user(self):
        # Today staff and end_user differ in specific tools, not categories.
        # If categories ever diverge, this test guards the rejection text
        # against accidentally widening capability claims for non-staff users.
        end_user = structure_factories.UserFactory()
        staff_user = structure_factories.UserFactory(is_staff=True)
        self.assertEqual(
            build_canned_rejection(staff_user, "MyOrg"),
            build_canned_rejection(end_user, "MyOrg"),
        )

    def test_none_user_lists_all_capabilities(self):
        # Internal callers without a user context fall through to the
        # full capability list so the rejection isn't silently empty.
        message = build_canned_rejection(None, "MyOrg")
        for phrase in CATEGORY_PHRASES.values():
            self.assertIn(phrase, message)

    def test_phrases_joined_with_oxford_and(self):
        user = structure_factories.UserFactory()
        message = build_canned_rejection(user, "MyOrg")
        # The last phrase in enum order is preceded by ", and ".
        last_phrase = CATEGORY_PHRASES[ToolCategory.PROPOSALS_REVIEWER]
        self.assertIn(f", and {last_phrase}", message)

    def test_every_category_has_a_phrase(self):
        # Guards against silently dropping a phrase when a new ToolCategory is added.
        for category in ToolCategory:
            self.assertIn(category, CATEGORY_PHRASES)
