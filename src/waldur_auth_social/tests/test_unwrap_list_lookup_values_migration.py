"""Tests for migration 0016_unwrap_list_lookup_values.

List-valued lookup claims used to be stored as their Python repr. The lookup
now unwraps the claim, so without this migration every affected account would
be missed on its next login and duplicated.
"""

from importlib import import_module

from django.db.migrations.loader import MigrationLoader
from django.test import TestCase, override_settings

from waldur_auth_social import models
from waldur_auth_social.const import PROVIDER_DEFAULTS, ProviderChoices
from waldur_auth_social.utils import create_or_update_oauth_user
from waldur_core.core.models import User
from waldur_core.structure.tests import factories as structure_factories

# The module name starts with a digit, so it cannot be imported by name.
migration = import_module(
    "waldur_auth_social.migrations.0016_unwrap_list_lookup_values"
)

MIGRATION = ("waldur_auth_social", "0016_unwrap_list_lookup_values")


# pytest runs with --no-migrations, which empties MIGRATION_MODULES and with it
# the loader; the historical registry is built from the migration files.
@override_settings(MIGRATION_MODULES={})
class UnwrapListLookupValuesMigrationTest(TestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        loader = MigrationLoader(None)
        cls.historical_apps = loader.project_state(MIGRATION).apps

    def setUp(self):
        self.provider = models.IdentityProvider.objects.create(
            provider=ProviderChoices.EDUTEAMS,
            client_id="test_client_id",
            client_secret="test_client_secret",
            discovery_url="http://eduteams.test/.well-known/openid-configuration",
            **{**PROVIDER_DEFAULTS[ProviderChoices.EDUTEAMS], "user_claim": "mail"},
        )

    def _run(self):
        migration.unwrap_list_lookup_values(self.historical_apps, None)

    def _username(self, user):
        user.refresh_from_db()
        return user.username

    def test_single_value_list_is_unwrapped(self):
        user = structure_factories.UserFactory(username="['a@example.com']")
        self._run()
        self.assertEqual(self._username(user), "a@example.com")

    def test_value_containing_a_quote_is_unwrapped(self):
        # repr switches to double quotes when the value holds a single quote.
        user = structure_factories.UserFactory(username=str(["o'brien@example.com"]))
        self._run()
        self.assertEqual(self._username(user), "o'brien@example.com")

    def test_deactivated_user_is_unwrapped(self):
        user = structure_factories.UserFactory(
            username="['a@example.com']", is_active=False
        )
        self._run()
        self.assertEqual(self._username(user), "a@example.com")

    def test_multi_value_list_is_left_alone(self):
        value = "['a@example.com', 'b@example.com']"
        user = structure_factories.UserFactory(username=value)
        self._run()
        self.assertEqual(self._username(user), value)

    def test_value_taken_by_another_user_is_left_alone(self):
        structure_factories.UserFactory(username="a@example.com")
        user = structure_factories.UserFactory(username="['a@example.com']")
        self._run()
        self.assertEqual(self._username(user), "['a@example.com']")

    def test_bracketed_value_that_is_not_a_list_is_left_alone(self):
        user = structure_factories.UserFactory(username="[admin]")
        self._run()
        self.assertEqual(self._username(user), "[admin]")

    def test_other_lookup_field_is_unwrapped(self):
        self.provider.user_field = "civil_number"
        self.provider.save()
        user = structure_factories.UserFactory(civil_number="['EE60001019906']")
        self._run()
        user.refresh_from_db()
        self.assertEqual(user.civil_number, "EE60001019906")

    def test_login_after_migration_matches_the_existing_account(self):
        user = structure_factories.UserFactory(username="['a@example.com']")
        self._run()

        synced, created = create_or_update_oauth_user(
            self.provider, {"mail": ["a@example.com"], "email": "a@example.com"}
        )

        self.assertFalse(created)
        self.assertEqual(synced.pk, user.pk)
        self.assertEqual(User.objects.count(), 1)
