from io import StringIO

from constance.test.unittest import override_config
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.support import models
from waldur_mastermind.support.enums import IssueStatusTypes
from waldur_mastermind.support.tests import fixtures

BASIC = override_config(
    WALDUR_SUPPORT_ENABLED=True,
    WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="basic",
    WALDUR_SUPPORT_AUTO_ASSIGN=False,
)


def seed():
    out = StringIO()
    call_command("init_service_desk_defaults", stdout=out)
    return out.getvalue()


@BASIC
class InitServiceDeskDefaultsTest(TestCase):
    def test_empty_database_gets_both_terminal_statuses(self):
        seed()

        self.assertEqual(
            set(models.IssueStatus.objects.values_list("name", "type")),
            {
                ("Resolved", IssueStatusTypes.RESOLVED),
                ("Canceled", IssueStatusTypes.CANCELED),
            },
        )

    def test_empty_database_gets_one_active_request_type(self):
        seed()

        request_type = models.RequestType.objects.get()
        self.assertEqual(request_type.name, "Service Request")
        self.assertTrue(request_type.is_active)
        # Null backend_id marks a locally created type, so a later sync from a
        # remote service desk can tell it apart from its own.
        self.assertIsNone(request_type.backend_id)
        self.assertEqual(request_type.backend_name, "basic")

    def test_seeded_statuses_make_a_ticket_resolvable(self):
        seed()

        self.assertTrue(models.IssueStatus.check_success_status("Resolved"))
        self.assertFalse(models.IssueStatus.check_success_status("Canceled"))

    def test_a_half_configured_table_is_repaired(self):
        # The state the command exists to avoid: one terminal type present, so
        # `check_success_status` answers None for every ticket and
        # `Issue.set_canceled` has no row to move to.
        models.IssueStatus.objects.create(name="done", type=IssueStatusTypes.RESOLVED)

        seed()

        self.assertEqual(
            set(models.IssueStatus.objects.values_list("name", "type")),
            {
                ("done", IssueStatusTypes.RESOLVED),
                ("Canceled", IssueStatusTypes.CANCELED),
            },
        )
        self.assertTrue(models.IssueStatus.check_success_status("done"))

    def test_a_status_named_after_a_missing_type_is_retyped(self):
        """The shape an upgraded deployment arrives in.

        `name` is unique and `type` defaults to RESOLVED, so statuses imported
        from a previous service desk can include a row called "Canceled" that is
        not of the CANCELED type. get_or_create matched that name, created
        nothing, and the type stayed missing on every later run — leaving
        Canceled off the dropdown permanently.
        """
        models.IssueStatus.objects.create(
            name="Canceled", type=IssueStatusTypes.RESOLVED
        )

        output = seed()

        self.assertEqual(
            set(models.IssueStatus.objects.values_list("name", "type")),
            {
                ("Canceled", IssueStatusTypes.CANCELED),
                ("Resolved", IssueStatusTypes.RESOLVED),
            },
        )
        self.assertIn("Corrected the type", output)
        self.assertFalse(models.IssueStatus.check_success_status("Canceled"))

    def test_retyping_is_not_repeated_on_the_next_run(self):
        models.IssueStatus.objects.create(
            name="Canceled", type=IssueStatusTypes.RESOLVED
        )
        seed()

        self.assertIn("already configured", seed())
        self.assertEqual(models.IssueStatus.objects.count(), 2)

    def test_a_swapped_table_is_corrected(self):
        """Complete by count, wrong by row.

        Both terminal types are present, so every completeness check passes and
        nothing is logged — but each label carries the other's type, so
        `check_success_status` answers the opposite of the truth for every
        ticket. Retyping is therefore not conditional on the type being absent.
        """
        models.IssueStatus.objects.create(
            name="Resolved", type=IssueStatusTypes.CANCELED
        )
        models.IssueStatus.objects.create(
            name="Canceled", type=IssueStatusTypes.RESOLVED
        )

        output = seed()

        self.assertEqual(
            set(models.IssueStatus.objects.values_list("name", "type")),
            {
                ("Resolved", IssueStatusTypes.RESOLVED),
                ("Canceled", IssueStatusTypes.CANCELED),
            },
        )
        self.assertIn("Corrected the type", output)
        self.assertTrue(models.IssueStatus.check_success_status("Resolved"))
        self.assertFalse(models.IssueStatus.check_success_status("Canceled"))

    def test_a_mistyped_label_is_corrected_even_when_its_type_exists(self):
        # The operator's own cancellation status already holds CANCELED, so the
        # old guard skipped the pass entirely and left "Canceled" meaning
        # resolved.
        models.IssueStatus.objects.create(name="done", type=IssueStatusTypes.RESOLVED)
        models.IssueStatus.objects.create(
            name="rejected", type=IssueStatusTypes.CANCELED
        )
        models.IssueStatus.objects.create(
            name="Canceled", type=IssueStatusTypes.RESOLVED
        )

        seed()

        self.assertEqual(
            models.IssueStatus.objects.get(name="Canceled").type,
            IssueStatusTypes.CANCELED,
        )
        # Nothing else moved, and no row was added to fill a type that was
        # never missing.
        self.assertEqual(models.IssueStatus.objects.count(), 3)
        self.assertEqual(
            models.IssueStatus.objects.get(name="done").type,
            IssueStatusTypes.RESOLVED,
        )

    def test_a_matched_row_is_not_reported_as_created(self):
        # get_or_create matching an existing name is the no-op this command
        # grew out of; it must not be announced as a creation.
        models.IssueStatus.objects.create(
            name="Resolved", type=IssueStatusTypes.RESOLVED
        )

        output = seed()

        self.assertNotIn("Created issue statuses: Resolved", output)
        self.assertIn("Created issue statuses: Canceled", output)

    def test_a_status_of_each_type_is_left_alone(self):
        models.IssueStatus.objects.create(name="done", type=IssueStatusTypes.RESOLVED)
        models.IssueStatus.objects.create(
            name="rejected", type=IssueStatusTypes.CANCELED
        )

        seed()

        self.assertEqual(
            sorted(models.IssueStatus.objects.values_list("name", flat=True)),
            ["done", "rejected"],
        )

    def test_existing_request_types_are_left_alone(self):
        models.RequestType.objects.create(
            name="Incident", issue_type_name="Incident", is_active=True
        )

        seed()

        self.assertEqual(
            list(models.RequestType.objects.values_list("name", flat=True)),
            ["Incident"],
        )

    def test_running_twice_creates_nothing_further(self):
        seed()
        seed()

        self.assertEqual(models.IssueStatus.objects.count(), 2)
        self.assertEqual(models.RequestType.objects.count(), 1)

    @override_config(WALDUR_SUPPORT_ACTIVE_BACKEND_TYPE="atlassian")
    def test_a_remote_backend_gets_statuses_but_no_request_type(self):
        # Seeding a type here would suppress the Atlassian backend's fallback
        # pull from Jira and hand a null backend id to ticket creation.
        output = seed()

        self.assertEqual(models.RequestType.objects.count(), 0)
        self.assertEqual(models.IssueStatus.objects.count(), 2)
        self.assertIn("not seeding one", output)


@BASIC
class SeededDeskAcceptsATicketTest(test.APITestCase):
    """The acceptance criterion: a seeded deployment works with no admin work."""

    def setUp(self):
        seed()
        self.fixture = fixtures.SupportFixture()
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.staff)

    def test_a_ticket_can_be_created_and_then_canceled(self):
        response = self.client.post(
            "/api/support-issues/",
            {
                "summary": "Need more disk",
                "type": "Service Request",
                "caller": structure_factories.UserFactory.get_url(self.staff),
            },
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        uuid = response.data["uuid"]

        detail = self.client.get(f"/api/support-issues/{uuid}/")
        self.assertEqual(
            sorted(detail.data["available_statuses"]), ["Canceled", "Resolved"]
        )

        moved = self.client.post(
            f"/api/support-issues/{uuid}/set_status/", {"status": "Canceled"}
        )
        self.assertEqual(moved.status_code, status.HTTP_200_OK, moved.data)
        self.assertFalse(models.Issue.objects.get(uuid=uuid).resolved)
