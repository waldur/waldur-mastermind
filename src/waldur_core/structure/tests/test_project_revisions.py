import datetime
from importlib import import_module
from unittest import mock

import reversion
from django.apps import apps as global_apps
from django.db import connection
from django.utils import timezone
from rest_framework import status, test
from reversion.models import Version

from waldur_core.structure import models
from waldur_core.structure.tests import factories, fixtures


def get_versions(project):
    return Version.objects.get_for_object(project)


def get_comments(project):
    return list(get_versions(project).values_list("revision__comment", flat=True))


class ProjectRevisionCreationTest(test.APITestCase):
    def test_project_creation_produces_exactly_one_initial_version(self):
        project = factories.ProjectFactory()

        self.assertEqual(get_comments(project), ["Initial version"])

    def test_project_created_through_the_api_gets_one_initial_version(self):
        fixture = fixtures.CustomerFixture()
        self.client.force_authenticate(fixture.staff)

        response = self.client.post(
            factories.ProjectFactory.get_list_url(),
            {
                "name": "Night's Watch",
                "customer": factories.CustomerFactory.get_url(fixture.customer),
            },
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        project = models.Project.objects.get(uuid=response.data["uuid"])
        self.assertEqual(get_comments(project), ["Initial version"])


class ProjectRevisionUpdateTest(test.APITestCase):
    def setUp(self):
        self.project = factories.ProjectFactory(name="Castle Black")

    def test_field_change_is_recorded(self):
        self.project.grace_period_days = 7
        self.project.save()

        self.assertEqual(get_versions(self.project).count(), 2)
        self.assertEqual(
            get_versions(self.project).first().revision.comment,
            "Changed: grace_period_days",
        )

    def test_several_changed_fields_are_named_in_one_version(self):
        self.project.slug = "castle-black"
        self.project.end_date = datetime.date(2030, 1, 1)
        self.project.save()

        self.assertEqual(get_versions(self.project).count(), 2)
        self.assertEqual(
            get_versions(self.project).first().revision.comment,
            "Changed: end_date, slug",
        )

    def test_foreign_keys_are_named_by_field_not_attname(self):
        user = factories.UserFactory()
        self.project.end_date = datetime.date(2030, 1, 1)
        self.project.end_date_requested_by = user
        self.project.save(update_fields=["end_date", "end_date_requested_by"])

        self.assertEqual(
            get_versions(self.project).first().revision.comment,
            "Changed: end_date, end_date_requested_by",
        )

    def test_end_date_snapshot_carries_its_update_timestamp(self):
        self.project.end_date = datetime.date(2030, 1, 1)
        self.project.save(update_fields=["end_date"])

        self.project.refresh_from_db()
        self.assertIsNotNone(self.project.end_date_updated_at)
        version = get_versions(self.project).first()
        self.assertEqual(version.revision.comment, "Changed: end_date")
        # The JSON serializer keeps milliseconds only.
        self.assertAlmostEqual(
            version.field_dict["end_date_updated_at"],
            self.project.end_date_updated_at,
            delta=datetime.timedelta(milliseconds=1),
        )

    def test_unsaved_end_date_change_is_not_stamped(self):
        self.project.end_date = datetime.date(2030, 1, 1)
        self.project.name = "The Nightfort"
        self.project.save(update_fields=["name"])

        self.project.refresh_from_db()
        self.assertIsNone(self.project.end_date)
        self.assertIsNone(self.project.end_date_updated_at)

    def test_snapshot_captures_the_post_change_state(self):
        self.project.staff_notes = "Beyond the Wall"
        self.project.save()

        version = get_versions(self.project).first()
        self.assertEqual(version.field_dict["staff_notes"], "Beyond the Wall")

    def test_no_op_save_is_not_recorded(self):
        self.project.save()

        self.assertEqual(get_versions(self.project).count(), 1)

    def test_modified_and_termination_metadata_alone_are_not_recorded(self):
        self.project.termination_metadata = {"users": []}
        self.project.save(update_fields=["termination_metadata", "modified"])

        self.assertEqual(get_versions(self.project).count(), 1)

    def test_change_left_out_of_update_fields_is_not_named(self):
        self.project.name = "The Nightfort"
        self.project.staff_notes = "Not saved"
        self.project.save(update_fields=["name"])

        self.assertEqual(
            get_versions(self.project).first().revision.comment, "Changed: name"
        )

    def test_explicit_revision_block_owns_the_snapshot(self):
        with reversion.create_revision():
            self.project.name = "The Nightfort"
            self.project.save()
            reversion.set_comment("Renamed by hand")

        self.assertEqual(
            get_comments(self.project), ["Renamed by hand", "Initial version"]
        )

    def test_soft_delete_is_recorded(self):
        self.project.delete()

        self.assertEqual(get_versions(self.project).count(), 2)
        version = get_versions(self.project).first()
        self.assertEqual(version.revision.comment, "Changed: is_removed")
        self.assertTrue(version.field_dict["is_removed"])


class ProjectRevisionRequestPathTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.staff = self.fixture.staff

    def test_rest_api_update_is_recorded_with_the_acting_user(self):
        self.client.force_authenticate(self.staff)

        response = self.client.patch(
            factories.ProjectFactory.get_url(self.project), {"name": "The Nightfort"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_versions(self.project).count(), 2)
        revision = get_versions(self.project).first().revision
        self.assertEqual(revision.comment, "Changed: name")
        self.assertEqual(revision.user, self.staff)

    def test_set_slug_is_recorded(self):
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            factories.ProjectFactory.get_url(self.project, "set_slug"),
            {"slug": "the-nightfort"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_versions(self.project).count(), 2)
        version = get_versions(self.project).first()
        self.assertEqual(version.revision.comment, "Changed: slug")
        self.assertEqual(version.revision.user, self.staff)
        self.assertEqual(version.field_dict["slug"], "the-nightfort")

    def test_move_project_is_recorded(self):
        new_customer = factories.CustomerFactory()
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            factories.ProjectFactory.get_url(self.project, "move_project"),
            {
                "customer": factories.CustomerFactory.get_url(new_customer),
                "preserve_permissions": False,
            },
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_versions(self.project).count(), 2)
        version = get_versions(self.project).first()
        self.assertEqual(version.revision.comment, "Changed: customer")
        self.assertEqual(version.revision.user, self.staff)
        self.assertEqual(version.field_dict["customer_id"], new_customer.pk)

    def test_soft_delete_through_the_api_is_recorded(self):
        self.client.force_authenticate(self.staff)

        response = self.client.delete(factories.ProjectFactory.get_url(self.project))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertEqual(get_versions(self.project).count(), 2)
        version = get_versions(self.project).first()
        self.assertEqual(version.revision.comment, "Changed: is_removed")
        self.assertEqual(version.revision.user, self.staff)

    def test_recover_is_recorded(self):
        self.project.delete()
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            factories.ProjectFactory.get_url(self.project, "recover"),
            {"end_date": "2030-01-01"},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_versions(self.project).count(), 3)
        version = get_versions(self.project).first()
        self.assertEqual(version.revision.comment, "Changed: end_date, is_removed")
        self.assertFalse(version.field_dict["is_removed"])

    def test_end_date_change_request_approval_is_recorded(self):
        review_request = factories.ProjectEndDateChangeRequestFactory(
            project=self.project, created_by=self.fixture.manager
        )
        self.client.force_authenticate(self.staff)

        response = self.client.post(
            factories.ProjectEndDateChangeRequestFactory.get_url(
                review_request, "approve"
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(get_versions(self.project).count(), 2)
        revision = get_versions(self.project).first().revision
        self.assertEqual(revision.comment, "Changed: end_date, end_date_requested_by")
        self.assertEqual(revision.user, self.staff)


class ProjectHistoryEndpointTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project

    def history_url(self, action="history"):
        return factories.ProjectFactory.get_url(self.project, action)

    def test_staff_and_support_can_read_history(self):
        for user in (self.fixture.staff, self.fixture.global_support):
            self.client.force_authenticate(user)
            response = self.client.get(self.history_url())
            self.assertEqual(response.status_code, status.HTTP_200_OK, user)

    def test_other_users_are_forbidden(self):
        for user in (self.fixture.owner, self.fixture.manager, self.fixture.admin):
            self.client.force_authenticate(user)
            response = self.client.get(self.history_url())
            self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN, user)

    def test_history_is_listed_newest_first(self):
        self.project.name = "The Nightfort"
        self.project.save()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.history_url())

        self.assertEqual(
            [entry["revision_comment"] for entry in response.data],
            ["Changed: name", "Initial version"],
        )
        self.assertEqual(response.data[0]["serialized_data"]["name"], "The Nightfort")

    def test_history_can_be_filtered_by_date(self):
        boundary = timezone.now()
        self.project.name = "The Nightfort"
        self.project.save()
        self.client.force_authenticate(self.fixture.staff)

        after = self.client.get(
            self.history_url(), {"created_after": boundary.isoformat()}
        )
        before = self.client.get(
            self.history_url(), {"created_before": boundary.isoformat()}
        )

        self.assertEqual(
            [entry["revision_comment"] for entry in after.data], ["Changed: name"]
        )
        self.assertEqual(
            [entry["revision_comment"] for entry in before.data], ["Initial version"]
        )

    def test_history_of_a_soft_deleted_project_is_readable(self):
        self.project.delete()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.history_url())

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data[0]["revision_comment"], "Changed: is_removed")

    def test_history_at_returns_the_state_at_that_time(self):
        original_name = self.project.name
        boundary = timezone.now()
        self.project.name = "The Nightfort"
        self.project.save()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(
            self.history_url("history/at"), {"timestamp": boundary.isoformat()}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["serialized_data"]["name"], original_name)


class ProjectRevisionBackfillTest(test.APITestCase):
    """Exercises migration 0088, which gives pre-existing projects a baseline."""

    def get_migration(self):
        # Not importable as an attribute path: the module name starts with a
        # digit, so mock.patch(...) on it fails too - patch the object instead.
        return import_module(
            "waldur_core.structure.migrations.0088_backfill_project_initial_revisions"
        )

    def run_backfill(self):
        with connection.schema_editor() as schema_editor:
            self.get_migration().backfill_project_initial_revisions(
                global_apps, schema_editor
            )

    def test_a_project_without_history_gets_a_baseline(self):
        project = factories.ProjectFactory(name="Castle Black")
        # Simulate a project created before projects were versioned.
        get_versions(project).delete()

        self.run_backfill()

        self.assertEqual(get_comments(project), ["Initial version (backfill)"])
        version = get_versions(project).first()
        self.assertEqual(version.field_dict["name"], "Castle Black")
        self.assertEqual(version.object_repr, str(project))

    def test_soft_deleted_projects_are_backfilled_too(self):
        project = factories.ProjectFactory()
        project.delete()
        get_versions(project).delete()

        self.run_backfill()

        self.assertEqual(get_versions(project).count(), 1)
        self.assertTrue(get_versions(project).first().field_dict["is_removed"])

    def test_projects_with_history_are_left_alone(self):
        project = factories.ProjectFactory()
        project.name = "The Nightfort"
        project.save()
        before = get_comments(project)

        self.run_backfill()

        self.assertEqual(get_comments(project), before)

    def test_backfill_is_idempotent(self):
        project = factories.ProjectFactory()
        get_versions(project).delete()

        self.run_backfill()
        self.run_backfill()

        self.assertEqual(get_versions(project).count(), 1)

    def test_more_projects_than_one_batch(self):
        projects = [factories.ProjectFactory() for _ in range(5)]
        for project in projects:
            get_versions(project).delete()

        with mock.patch.object(self.get_migration(), "BATCH_SIZE", 2):
            self.run_backfill()

        for project in projects:
            self.assertEqual(get_versions(project).count(), 1, project.name)
