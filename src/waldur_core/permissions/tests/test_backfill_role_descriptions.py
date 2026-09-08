import pkgutil
from importlib import import_module

from django.apps import apps as real_apps
from django.contrib.contenttypes.models import ContentType
from django.db import connection
from django.db.migrations.state import ProjectState
from django.db.models import Q
from django.test import TestCase

from waldur_core.permissions.enums import ROLE_DESCRIPTIONS, RoleEnum
from waldur_core.permissions.models import Role, RoleManager
from waldur_core.permissions.serializers import RoleDetailsSerializer

# Located by suffix rather than by number: migrations get renumbered on rebase
# when develop ships a conflicting leaf, and a hardcoded name would break
# collection for the whole module.
MIGRATION_SUFFIX = "_backfill_system_role_descriptions"


def load_migration():
    package = import_module("waldur_mastermind.proposal.migrations")
    names = [
        name
        for _, name, _ in pkgutil.iter_modules(package.__path__)
        if name.endswith(MIGRATION_SUFFIX)
    ]
    if len(names) != 1:
        raise AssertionError(f"expected exactly one *{MIGRATION_SUFFIX}, got {names}")
    return import_module(f"{package.__name__}.{names[0]}")


migration = load_migration()

# The last proposal migration that creates a system role.
LAST_ROLE_SEEDING_MIGRATION = "0030_proposal_manager_role"


class BackfillRoleDescriptionsTest(TestCase):
    """The rows this repairs are the ones the seeding migrations left behind.

    Those migrations wrote Role.description through a historical model, which
    has no modeltranslation descriptors, so the value never reached
    description_en -- the field the API actually serves.
    """

    def setUp(self):
        RoleManager.clear_cache()
        self.content_type = ContentType.objects.get_by_natural_key(
            "proposal", "proposal"
        )

    def tearDown(self):
        RoleManager.clear_cache()

    def run_migration(self):
        # The app registry as the migration itself sees it: rebuilt models with
        # no modeltranslation descriptors, so writing `description` lands in the
        # raw column exactly as it does during a real migration. Built from the
        # app registry rather than the migration graph, because the sharded CI
        # suite runs with --no-migrations and has no graph to walk.
        apps = ProjectState.from_apps(real_apps).apps
        migration.backfill_descriptions(apps, None)

    def make_role(self, name, content_type=None):
        # Migrations already seed some of these, so start from a known row.
        Role.objects.filter(name=name).delete()
        return Role.objects.create(
            name=name,
            content_type=content_type or self.content_type,
            is_system_role=True,
        )

    def break_role(self, role, raw_description):
        """Reproduce what a seeding migration leaves behind: raw column only."""
        with connection.cursor() as cursor:
            cursor.execute(
                "UPDATE permissions_role SET description = %s, description_en = NULL"
                " WHERE id = %s",
                [raw_description, role.id],
            )

    def served_description(self, name):
        role = Role.objects.get(name=name)
        return RoleDetailsSerializer(role).data["description"]

    def test_description_that_never_reached_the_api_is_repaired(self):
        # The state the seeding migrations leave: the label is in the row, but
        # not in the field the API reads.
        role = self.make_role(RoleEnum.PROPOSAL_MEMBER)
        self.break_role(role, "Proposal member")
        self.assertEqual(self.served_description(RoleEnum.PROPOSAL_MEMBER), "")

        self.run_migration()

        self.assertEqual(
            self.served_description(RoleEnum.PROPOSAL_MEMBER), "Proposal member"
        )

    def test_operator_customisation_is_promoted_not_replaced(self):
        # The localized columns arrived in 2023 and nothing backfilled them, so
        # a description customised before that is indistinguishable from one a
        # seeding migration left behind. Imposing the canonical label here would
        # silently revert a site's own terminology.
        role = self.make_role(RoleEnum.PROJECT_MEMBER)
        self.break_role(role, "Allocation user")

        self.run_migration()

        self.assertEqual(
            self.served_description(RoleEnum.PROJECT_MEMBER), "Allocation user"
        )
        role.refresh_from_db()
        self.assertEqual(role.description, "Allocation user")

    def test_stale_seeded_description_is_promoted_verbatim(self):
        # permissions/0002_import_data seeded "Project administator"; correcting
        # spelling is import_roles' job, not this migration's. Guessing which
        # differing values are mistakes is what would endanger real ones.
        role = self.make_role(RoleEnum.PROJECT_ADMIN)
        self.break_role(role, "Project administator")

        self.run_migration()

        self.assertEqual(
            self.served_description(RoleEnum.PROJECT_ADMIN), "Project administator"
        )

    def test_entirely_blank_description_is_filled(self):
        role = self.make_role(RoleEnum.PROPOSAL_MEMBER)
        self.break_role(role, "")

        self.run_migration()

        self.assertEqual(
            self.served_description(RoleEnum.PROPOSAL_MEMBER), "Proposal member"
        )

    def test_customised_description_is_preserved(self):
        role = self.make_role(RoleEnum.PROPOSAL_MEMBER)
        role.description = "Grant partner"
        role.save()

        self.run_migration()

        self.assertEqual(
            self.served_description(RoleEnum.PROPOSAL_MEMBER), "Grant partner"
        )

    def test_missing_role_is_not_created(self):
        # Seeding roles is get_system_role's job; this only repairs existing rows.
        Role.objects.filter(name=RoleEnum.PROPOSAL_MEMBER).delete()

        self.run_migration()

        self.assertFalse(Role.objects.filter(name=RoleEnum.PROPOSAL_MEMBER).exists())

    def test_non_system_role_is_untouched(self):
        # A locally defined role that happens to share a system role's name
        # is not ours to relabel.
        customer_ct = ContentType.objects.get_by_natural_key("structure", "customer")
        Role.objects.filter(name=RoleEnum.PROPOSAL_MEMBER).delete()
        custom = Role.objects.create(
            name=RoleEnum.PROPOSAL_MEMBER,
            content_type=customer_ct,
            is_system_role=False,
        )
        self.break_role(custom, "")

        self.run_migration()

        custom.refresh_from_db()
        self.assertFalse(custom.description_en)

    def test_is_idempotent(self):
        role = self.make_role(RoleEnum.PROPOSAL_MEMBER)
        self.break_role(role, "Proposal member")

        self.run_migration()
        first = self.served_description(RoleEnum.PROPOSAL_MEMBER)
        self.run_migration()

        self.assertEqual(self.served_description(RoleEnum.PROPOSAL_MEMBER), first)

    def test_other_languages_are_left_empty(self):
        role = self.make_role(RoleEnum.PROPOSAL_MEMBER)
        self.break_role(role, "Proposal member")

        self.run_migration()

        role.refresh_from_db()
        self.assertEqual(role.description_en, "Proposal member")
        self.assertFalse(role.description_de)

    def test_every_known_role_is_repaired(self):
        customer_ct = ContentType.objects.get_by_natural_key("structure", "customer")
        call_ct = ContentType.objects.get_by_natural_key("proposal", "call")
        scopes = {
            RoleEnum.PROPOSAL_MEMBER: self.content_type,
            RoleEnum.CUSTOMER_READER: customer_ct,
            RoleEnum.CALL_PANEL_MEMBER: call_ct,
        }
        for name, content_type in scopes.items():
            self.break_role(self.make_role(name, content_type), "")

        self.run_migration()

        for name in scopes:
            with self.subTest(role=name):
                self.assertEqual(self.served_description(name), ROLE_DESCRIPTIONS[name])


class SeededRoleDescriptionTest(TestCase):
    """Guards the migration's position in the graph, not just its logic.

    The test database is built by running every migration, so a backfill that
    runs before the roles it targets are seeded shows up here as a role the API
    would serve without a description.
    """

    def test_backfill_is_ordered_after_the_roles_are_seeded(self):
        # The call and proposal roles are seeded by proposal migrations up to
        # 0030_proposal_manager_role. A backfill ordered before those runs
        # against rows that do not exist yet and silently repairs nothing, so
        # depending on the proposal app is not enough -- the dependency has to
        # sit after the last one that creates a role.
        proposal_deps = [
            name for app, name in migration.Migration.dependencies if app == "proposal"
        ]

        self.assertTrue(proposal_deps, "must depend on the proposal app")
        # Migration names are zero-padded, so this compares by number.
        self.assertTrue(
            all(name >= LAST_ROLE_SEEDING_MIGRATION for name in proposal_deps),
            f"{proposal_deps} must be at or after {LAST_ROLE_SEEDING_MIGRATION}",
        )

    def test_no_seeded_system_role_is_served_without_a_description(self):
        seeded = Role.objects.filter(is_system_role=True)
        if not seeded.exists():
            self.skipTest("run with migrations enabled; no roles are seeded here")

        blank = seeded.filter(Q(description_en__isnull=True) | Q(description_en=""))

        self.assertEqual(sorted(blank.values_list("name", flat=True)), [])
