from importlib import import_module
from unittest import mock

from constance.test import override_config
from django.apps import apps as global_apps
from django.conf import settings
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse
from django.utils import timezone
from rest_framework import test
from rest_framework.authtoken.models import Token
from reversion.models import Revision, Version

from waldur_core.core import handlers as core_handlers
from waldur_core.core import models as core_models
from waldur_core.core import tasks as core_tasks
from waldur_core.permissions import handlers as permission_handlers
from waldur_core.structure.tests import factories as structure_factories


def get_versions(user):
    return Version.objects.get_for_object(user)


def get_comments(user):
    return list(get_versions(user).values_list("revision__comment", flat=True))


class UserRevisionCreationTest(test.APITestCase):
    def test_user_creation_produces_exactly_one_initial_version(self):
        user = structure_factories.UserFactory()

        self.assertEqual(get_versions(user).count(), 1)
        self.assertEqual(get_comments(user), ["Initial version"])

    def test_default_token_lifetime_does_not_add_a_second_version(self):
        """The post_save default is a system default, not a user-visible change."""
        user = structure_factories.UserFactory(token_lifetime=None)
        user.refresh_from_db()

        self.assertIsNotNone(user.token_lifetime)
        self.assertEqual(get_comments(user), ["Initial version"])


class UserRevisionUpdateTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(
            first_name="John",
            last_name="Snow",
            native_name="Jöhn Snöw",
            email="john@example.com",
            username="john",
        )

    def test_profile_change_is_recorded(self):
        self.user.job_title = "Lord Commander"
        self.user.save(update_fields=["job_title"])

        self.assertEqual(get_versions(self.user).count(), 2)
        self.assertEqual(
            get_versions(self.user).first().revision.comment, "Changed: job_title"
        )

    def test_no_op_save_is_not_recorded(self):
        self.user.save()

        self.assertEqual(get_versions(self.user).count(), 1)

    def test_deactivation_is_recorded(self):
        permission_handlers.deactivate_user_with_logging(self.user, "No active roles")

        self.assertEqual(get_versions(self.user).count(), 2)
        comment = get_versions(self.user).first().revision.comment
        self.assertIn("deactivation_reason", comment)
        self.assertIn("is_active", comment)

    def test_change_source_is_recorded_in_comment(self):
        self.user._change_source = "isd:puhuri"
        self.user.organization = "Puhuri"
        self.user.save(update_fields=["organization"])

        self.assertEqual(
            get_versions(self.user).first().revision.comment,
            "Changed: organization (source: isd:puhuri)",
        )


class UserRevisionNoiseFilterTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(
            first_name="John",
            last_name="Snow",
            native_name="Jöhn Snöw",
            email="john@example.com",
            username="john",
        )

    def test_last_login_and_last_sync_are_not_recorded(self):
        from django.utils import timezone

        self.user.last_login = timezone.now()
        self.user.last_sync = timezone.now()
        self.user.save(update_fields=["last_login", "last_sync"])

        self.assertEqual(get_versions(self.user).count(), 1)

    def test_attribute_sources_churn_is_not_recorded(self):
        self.user.attribute_sources = {
            "first_name": {"source": "isd:eduteams", "timestamp": "2026-01-01T00:00:00"}
        }
        self.user.save(update_fields=["attribute_sources"])

        self.assertEqual(get_versions(self.user).count(), 1)

    def test_uppercased_first_name_is_not_recorded(self):
        """eduTEAMS re-casing a name owned by another IdP is noise, not history."""
        self.user.first_name = "JOHN"
        self.user.save(update_fields=["first_name"])

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "JOHN")
        self.assertEqual(get_versions(self.user).count(), 1)

    def test_recased_native_name_is_not_recorded(self):
        self.user.native_name = "JÖHN SNÖW"
        self.user.save(update_fields=["native_name"])

        self.assertEqual(get_versions(self.user).count(), 1)

    def test_recased_last_name_is_not_recorded(self):
        self.user.last_name = "snow"
        self.user.save(update_fields=["last_name"])

        self.assertEqual(get_versions(self.user).count(), 1)

    def test_real_name_change_is_still_recorded(self):
        self.user.first_name = "Jon"
        self.user.save(update_fields=["first_name"])

        self.assertEqual(get_versions(self.user).count(), 2)
        self.assertEqual(
            get_versions(self.user).first().revision.comment, "Changed: first_name"
        )

    def test_case_only_change_is_omitted_from_a_mixed_revision(self):
        self.user.first_name = "JOHN"
        self.user.job_title = "Lord Commander"
        self.user.save(update_fields=["first_name", "job_title"])

        self.assertEqual(get_versions(self.user).count(), 2)
        self.assertEqual(
            get_versions(self.user).first().revision.comment, "Changed: job_title"
        )

    def test_username_case_change_is_recorded(self):
        """Identity-bearing fields are compared case-sensitively."""
        self.user.username = "John"
        self.user.save(update_fields=["username"])

        self.assertEqual(get_versions(self.user).count(), 2)
        self.assertIn("username", get_versions(self.user).first().revision.comment)

    def test_email_case_change_is_recorded(self):
        self.user.email = "John@example.com"
        self.user.save(update_fields=["email"])

        self.assertEqual(get_versions(self.user).count(), 2)
        self.assertIn("email", get_versions(self.user).first().revision.comment)


class IdpSyncRevisionTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(
            first_name="John", last_name="Snow", username="john"
        )

    def test_idp_attribute_sync_is_recorded_with_its_source(self):
        from waldur_auth_social.utils import update_user_attributes_from_source

        update_user_attributes_from_source(
            self.user, {"organization": "Night's Watch"}, source="isd:eduteams"
        )

        self.assertEqual(get_versions(self.user).count(), 2)
        comment = get_versions(self.user).first().revision.comment
        self.assertIn("organization", comment)
        self.assertIn("isd:eduteams", comment)

    def test_idp_recasing_a_name_produces_no_revision(self):
        from waldur_auth_social.utils import update_user_attributes_from_source

        # First sync registers the ISD, which is a genuine change.
        update_user_attributes_from_source(
            self.user, {"first_name": "John"}, source="isd:eduteams"
        )
        baseline = get_versions(self.user).count()

        # Subsequent logins flap the casing back and forth.
        for name in ("JOHN", "John", "JOHN"):
            update_user_attributes_from_source(
                self.user, {"first_name": name}, source="isd:eduteams"
            )

        self.user.refresh_from_db()
        self.assertEqual(self.user.first_name, "JOHN")
        self.assertEqual(get_versions(self.user).count(), baseline)


class UserRevisionRequestPathTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.user = structure_factories.UserFactory(first_name="John")

    def test_rest_api_update_is_recorded_with_the_acting_user(self):
        self.client.force_authenticate(self.staff)
        url = structure_factories.UserFactory.get_url(self.user)

        response = self.client.patch(url, {"job_title": "Lord Commander"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(get_versions(self.user).count(), 2)
        revision = get_versions(self.user).first().revision
        self.assertEqual(revision.comment, "Changed: job_title")
        self.assertEqual(revision.user, self.staff)

    def test_history_endpoint_exposes_the_change(self):
        self.user.job_title = "Lord Commander"
        self.user.save(update_fields=["job_title"])
        self.client.force_authenticate(self.staff)
        url = structure_factories.UserFactory.get_url(self.user, action="history")

        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data), 2)
        self.assertEqual(
            response.data[0]["serialized_data"]["job_title"], "Lord Commander"
        )


class UserAdminRevisionTest(TestCase):
    """Uses the plain Django client: DRF's APIClient posts JSON, which the
    admin does not accept."""

    def setUp(self):
        self.admin = structure_factories.UserFactory(is_staff=True, is_superuser=True)
        self.user = structure_factories.UserFactory()
        self.client.force_login(self.admin)

    def test_bulk_deactivation_is_recorded_and_revokes_tokens(self):
        url = reverse("admin:core_user_changelist")

        response = self.client.post(
            url,
            {
                "action": "administratively_deactivate",
                "_selected_action": [str(self.user.pk)],
            },
            follow=True,
        )

        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)
        self.assertTrue(self.user.is_admin_deactivated)
        self.assertEqual(get_versions(self.user).count(), 2)
        comment = get_versions(self.user).first().revision.comment
        self.assertIn("Administratively deactivated via admin", comment)
        self.assertFalse(
            get_versions(self.user).first().field_dict["is_active"],
            "the snapshot must capture the post-change state",
        )

    def test_bulk_reactivation_is_recorded(self):
        core_models.User.objects.filter(pk=self.user.pk).update(
            is_active=False, is_admin_deactivated=True
        )
        url = reverse("admin:core_user_changelist")

        self.client.post(
            url,
            {
                "action": "reactivate",
                "_selected_action": [str(self.user.pk)],
            },
            follow=True,
        )

        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)
        self.assertFalse(self.user.is_admin_deactivated)
        self.assertEqual(get_versions(self.user).count(), 2)

    def test_admin_change_form_creates_a_single_revision(self):
        """VersionAdmin owns the snapshot; the signal handler must not double it."""
        url = reverse("admin:core_user_change", args=[self.user.pk])
        get = self.client.get(url)
        form = get.context["adminform"].form
        payload = {}
        for name, field in form.fields.items():
            value = form.initial.get(name, field.initial)
            if value is None or value is False:
                continue
            payload[name] = "on" if value is True else str(value)
        payload["username"] = self.user.username
        payload["job_title"] = "Lord Commander"
        for formset in get.context.get("inline_admin_formsets", []):
            for key, value in formset.formset.management_form.initial.items():
                payload[f"{formset.formset.prefix}-{key}"] = value

        self.client.post(url, payload, follow=True)

        self.user.refresh_from_db()
        self.assertEqual(self.user.job_title, "Lord Commander")
        self.assertEqual(get_versions(self.user).count(), 2)


class UserRevisionRetentionTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(job_title="Steward")

    def make_revisions(self, count):
        for index in range(count):
            self.user.job_title = f"Title {index}"
            self.user.save(update_fields=["job_title"])

    def age_all_revisions(self, days):
        Revision.objects.filter(
            pk__in=get_versions(self.user).values_list("revision_id", flat=True)
        ).update(date_created=timezone.now() - timezone.timedelta(days=days))

    @override_config(USER_REVISION_RETENTION_DAYS=30, USER_REVISION_KEEP_MINIMUM=3)
    def test_old_revisions_beyond_the_minimum_are_pruned(self):
        self.make_revisions(9)
        self.age_all_revisions(days=365)
        self.assertEqual(get_versions(self.user).count(), 10)

        core_tasks.delete_stale_user_revisions()

        self.assertEqual(get_versions(self.user).count(), 3)

    @override_config(USER_REVISION_RETENTION_DAYS=30, USER_REVISION_KEEP_MINIMUM=3)
    def test_the_most_recent_revisions_are_the_ones_kept(self):
        self.make_revisions(9)
        self.age_all_revisions(days=365)

        core_tasks.delete_stale_user_revisions()

        kept = [version.field_dict["job_title"] for version in get_versions(self.user)]
        self.assertEqual(kept, ["Title 8", "Title 7", "Title 6"])

    @override_config(USER_REVISION_RETENTION_DAYS=30, USER_REVISION_KEEP_MINIMUM=3)
    def test_recent_revisions_are_not_pruned(self):
        self.make_revisions(9)

        core_tasks.delete_stale_user_revisions()

        self.assertEqual(get_versions(self.user).count(), 10)

    @override_config(USER_REVISION_RETENTION_DAYS=30, USER_REVISION_KEEP_MINIMUM=3)
    def test_a_quiet_user_keeps_its_whole_history(self):
        """Fewer revisions than the minimum: nothing is eligible, however old."""
        self.make_revisions(1)
        self.age_all_revisions(days=3650)

        core_tasks.delete_stale_user_revisions()

        self.assertEqual(get_versions(self.user).count(), 2)

    @override_config(USER_REVISION_RETENTION_DAYS=0, USER_REVISION_KEEP_MINIMUM=3)
    def test_retention_can_be_disabled(self):
        self.make_revisions(9)
        self.age_all_revisions(days=3650)

        core_tasks.delete_stale_user_revisions()

        self.assertEqual(get_versions(self.user).count(), 10)

    @override_config(USER_REVISION_RETENTION_DAYS=30, USER_REVISION_KEEP_MINIMUM=0)
    def test_a_zero_minimum_is_refused_rather_than_wiping_history(self):
        self.make_revisions(9)
        self.age_all_revisions(days=3650)

        core_tasks.delete_stale_user_revisions()

        self.assertEqual(get_versions(self.user).count(), 10)

    @override_config(USER_REVISION_RETENTION_DAYS=30, USER_REVISION_KEEP_MINIMUM=3)
    def test_other_models_history_is_untouched(self):
        offering = structure_factories.CustomerFactory()
        self.make_revisions(9)
        self.age_all_revisions(days=365)
        customer_versions = Version.objects.get_for_object(offering).count()

        core_tasks.delete_stale_user_revisions()

        self.assertEqual(
            Version.objects.get_for_object(offering).count(), customer_versions
        )


class UserRevisionBackfillTest(test.APITestCase):
    """Exercises migration 0041, which gives pre-existing users a baseline."""

    def get_migration(self):
        # Not importable as an attribute path: the module name starts with a
        # digit, so mock.patch(...) on it fails too - patch the object instead.
        return import_module(
            "waldur_core.core.migrations.0041_backfill_user_initial_revisions"
        )

    def run_backfill(self):
        with connection.schema_editor() as schema_editor:
            self.get_migration().backfill_user_initial_revisions(
                global_apps, schema_editor
            )

    def test_a_user_without_history_gets_a_baseline(self):
        user = structure_factories.UserFactory(first_name="John")
        # Simulate an account created before create_initial_revision existed.
        get_versions(user).delete()

        self.run_backfill()

        self.assertEqual(get_comments(user), ["Initial version (backfill)"])
        version = get_versions(user).first()
        self.assertEqual(version.field_dict["first_name"], "John")
        self.assertEqual(version.object_repr, user.username)

    def test_inactive_users_are_backfilled_too(self):
        user = structure_factories.UserFactory(is_active=False)
        get_versions(user).delete()

        self.run_backfill()

        self.assertEqual(get_versions(user).count(), 1)

    def test_users_with_history_are_left_alone(self):
        user = structure_factories.UserFactory()
        user.job_title = "Lord Commander"
        user.save(update_fields=["job_title"])
        before = get_comments(user)

        self.run_backfill()

        self.assertEqual(get_comments(user), before)

    def test_backfill_is_idempotent(self):
        user = structure_factories.UserFactory()
        get_versions(user).delete()

        self.run_backfill()
        self.run_backfill()

        self.assertEqual(get_versions(user).count(), 1)

    def test_more_users_than_one_batch(self):
        users = [structure_factories.UserFactory() for _ in range(5)]
        for user in users:
            get_versions(user).delete()

        with mock.patch.object(self.get_migration(), "BATCH_SIZE", 2):
            self.run_backfill()

        for user in users:
            self.assertEqual(get_versions(user).count(), 1, user.username)


class UserRevisionActorTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        # Editing one's own profile requires an accepted policy, and under
        # impersonation the request is made as the impersonated user.
        self.user = structure_factories.UserFactory(agreement_date=timezone.now())

    def test_impersonated_change_is_credited_to_the_impersonator(self):
        impersonated_header = settings.WALDUR_CORE[
            "REQUEST_HEADER_IMPERSONATED_USER_UUID"
        ]
        token = Token.objects.get(user=self.staff)
        self.client.credentials(
            **{
                "HTTP_AUTHORIZATION": "Token " + token.key,
                impersonated_header: self.user.uuid.hex,
            }
        )
        url = structure_factories.UserFactory.get_url(self.user)

        response = self.client.patch(url, {"job_title": "Lord Commander"})

        self.assertEqual(response.status_code, 200)
        revision = get_versions(self.user).first().revision
        self.assertEqual(
            revision.user,
            self.staff,
            "the impersonator is accountable, not the account acted as",
        )
        self.assertIn(f"impersonating {self.user.username}", revision.comment)

    def test_deactivated_actor_is_still_credited(self):
        self.staff.is_active = False
        self.staff.save(update_fields=["is_active"])
        self.client.force_authenticate(self.staff)
        self.user.job_title = "Lord Commander"
        self.user.save(update_fields=["job_title"])

        self.assertEqual(get_versions(self.user).count(), 2)


class UserRevisionChangeSourceTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory()

    def test_change_source_does_not_leak_into_the_next_save(self):
        self.user._change_source = "isd:eduteams"
        self.user.organization = "Night's Watch"
        self.user.save(update_fields=["organization"])

        self.user.job_title = "Lord Commander"
        self.user.save(update_fields=["job_title"])

        self.assertEqual(
            get_versions(self.user).first().revision.comment, "Changed: job_title"
        )


class UserRevisionDeferredFieldsTest(test.APITestCase):
    def setUp(self):
        self.user = structure_factories.UserFactory(description="Steward")

    def test_saving_a_deferred_instance_records_only_the_loaded_change(self):
        deferred = core_models.User.objects.only("username", "job_title").get(
            pk=self.user.pk
        )
        self.assertIn("description", deferred.get_deferred_fields())

        deferred.job_title = "Lord Commander"
        deferred.save(update_fields=["job_title"])

        self.assertEqual(get_versions(self.user).count(), 2)
        self.assertEqual(
            get_versions(self.user).first().revision.comment, "Changed: job_title"
        )

    def test_snapshotting_a_deferred_instance_issues_no_queries(self):
        """Diffing must not fetch fields the caller never loaded.

        Asserted on the snapshot helper rather than end to end, because the
        pre-existing log_user_save handler reads every whitelisted field on
        every save and loads them regardless of what this handler does.
        """
        deferred = core_models.User.objects.only("username", "job_title").get(
            pk=self.user.pk
        )
        self.assertIn("description", deferred.get_deferred_fields())

        with CaptureQueriesContext(connection) as queries:
            snapshot = core_handlers.get_field_snapshot(
                deferred, exclude=deferred.get_deferred_fields()
            )

        self.assertEqual(len(queries.captured_queries), 0)
        self.assertIn("job_title", snapshot)
        self.assertNotIn("description", snapshot)
