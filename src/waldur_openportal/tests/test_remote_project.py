import datetime
from unittest import mock

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework import test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_openportal import exceptions, models, remote_project_service
from waldur_openportal.board import OpenPortalBoard

DESTINATION = "someportal.somebridge.someoffering"


class RemoteProjectDefaultsTest(TestCase):
    """
    The values a brand new award starts with.

    These are the permissive end of the range — anyone can join, from any
    domain — so they are pinned here to make a change to them deliberate
    rather than incidental. Narrowing an award is an explicit act by an
    organisation owner through the RemoteProject actions.
    """

    def setUp(self):
        self.allocation = mock.Mock(
            created=timezone.now() - datetime.timedelta(hours=2),
        )
        self.allocation.project = mock.Mock()

        links_patcher = mock.patch(
            "waldur_openportal.utils.get_proposal_links_for_project",
            return_value=(None, None),
        )
        links_patcher.start()
        self.addCleanup(links_patcher.stop)

        objects_patcher = mock.patch(
            "waldur_openportal.remote_project_service.models.RemoteProject.objects"
        )
        self.remote_projects = objects_patcher.start()
        self.addCleanup(objects_patcher.stop)
        self.remote_projects.get_or_create.return_value = (mock.Mock(), True)

    def created_defaults(self):
        remote_project_service.get_or_create_remote_project(
            self.allocation, DESTINATION, remote_identifier=None
        )
        return self.remote_projects.get_or_create.call_args.kwargs["defaults"]

    def test_membership_starts_open(self):
        self.assertEqual(
            self.created_defaults()["membership_control"],
            models.MembershipControlChoices.OPEN,
        )

    def test_no_domain_restriction_is_applied(self):
        # None, not [] — an empty list would mean "nobody may join".
        self.assertIsNone(self.created_defaults()["allowed_domains"])

    def test_no_renewal_link_is_set(self):
        self.assertIsNone(self.created_defaults()["link_renewal"])

    def test_award_is_held_briefly_before_it_can_be_approved(self):
        defaults = self.created_defaults()
        self.assertEqual(
            defaults["earliest_approve"],
            self.allocation.created + datetime.timedelta(hours=1),
        )


class AllowedDomainsTest(TestCase):
    """
    allowed_domains distinguishes three states, and all three have to survive
    the trip through get_extras()/award_details():

        None  — no restriction
        []    — nothing allowed
        [...] — restricted to the listed patterns
    """

    def build(self, allowed_domains, last_sent=None):
        return models.RemoteProject(
            destination=DESTINATION,
            identifier="someproject.someportal",
            allowed_domains=allowed_domains,
            last_sent_details=last_sent,
        )

    def test_extras_omit_allowed_domains_when_unrestricted(self):
        self.assertNotIn("allowed_domains", self.build(None).get_extras())

    def test_extras_keep_an_empty_allowed_domains_list(self):
        self.assertEqual(self.build([]).get_extras()["allowed_domains"], [])

    def test_extras_keep_a_populated_allowed_domains_list(self):
        self.assertEqual(
            self.build(["*.ac.uk"]).get_extras()["allowed_domains"], ["*.ac.uk"]
        )

    def test_empty_list_overrides_a_previously_sent_restriction(self):
        """
        Locking everybody out has to actually reach the remote portal, rather
        than being read as "unset" and leaving the old patterns in place.
        """
        remote_project = self.build([], last_sent={"allowed_domains": ["*.ac.uk"]})

        self.assertEqual(remote_project.award_details().allowed_domains, [])

    def test_populated_list_overrides_a_previously_sent_restriction(self):
        remote_project = self.build(
            ["*.example.org"], last_sent={"allowed_domains": ["*.ac.uk"]}
        )

        self.assertEqual(
            [str(d) for d in remote_project.award_details().allowed_domains],
            ["*.example.org"],
        )


class MembersOutsideAllowedDomainsTest(TestCase):
    """A member whose email falls outside allowed_domains must not break reads.

    The openportal library validates member emails in exactly one place -- the
    ``members`` attribute setter. ``from_json()`` and ``merge()`` both accept
    them without complaint, so such members are stored perfectly legitimately:
    a member added before the domain list was tightened, for instance.

    Assigning them back therefore raised

        OSError: Parse("Email '...' is not in the allowed domains
                        for this project")

    from a pure read path, which took down the whole remote-projects list
    rather than one row.
    """

    def build(self, allowed_domains=None, last_sent=None, last_confirmed=None):
        return models.RemoteProject(
            destination=DESTINATION,
            identifier="someproject.someportal",
            allowed_domains=allowed_domains,
            last_sent_details=last_sent,
            last_confirmed_details=last_confirmed,
        )

    def test_a_member_outside_the_snapshot_domains_does_not_raise(self):
        remote_project = self.build(
            last_sent={"members": {"person@elsewhere.com": "user"}},
            last_confirmed={"allowed_domains": ["*.ac.uk"]},
        )

        details = remote_project.award_details()

        self.assertEqual(details.members, {"person@elsewhere.com": "user"})

    def test_the_member_is_kept_rather_than_quietly_dropped(self):
        """Read paths report what is stored; they do not edit the team."""
        remote_project = self.build(
            last_sent={
                "members": {
                    "inside@ok.ac.uk": "user",
                    "outside@elsewhere.com": "admin",
                }
            },
            last_confirmed={"allowed_domains": ["*.ac.uk"]},
        )

        self.assertEqual(
            remote_project.award_details().members,
            {"inside@ok.ac.uk": "user", "outside@elsewhere.com": "admin"},
        )

    def test_last_sent_membership_still_wins_over_last_confirmed(self):
        """The authority rule the removed assignment existed to enforce."""
        remote_project = self.build(
            last_sent={"members": {"current@ok.ac.uk": "user"}},
            last_confirmed={"members": {"stale@ok.ac.uk": "admin"}},
        )

        self.assertEqual(
            remote_project.award_details().members, {"current@ok.ac.uk": "user"}
        )

    def test_no_sent_members_clears_a_confirmed_membership(self):
        """last_confirmed must never supply members of its own."""
        remote_project = self.build(
            last_sent={"name": "an award"},
            last_confirmed={"members": {"stale@ok.ac.uk": "admin"}},
        )

        self.assertIsNone(remote_project.award_details().members)

    def test_an_empty_sent_membership_is_not_read_as_unset(self):
        remote_project = self.build(
            last_sent={"members": {}},
            last_confirmed={"members": {"stale@ok.ac.uk": "admin"}},
        )

        self.assertEqual(remote_project.award_details().members, {})


class RemoteProjectListWithMemberOutsideAllowedDomainsTest(test.APITestCase):
    """One remote project whose details will not derive must not 500 the list."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.outside = models.RemoteProject.objects.create(
            destination=DESTINATION,
            identifier="outside.someportal",
            current_project=self.fixture.project,
            last_sent_details={
                "allocation": "100 GPUh",
                "members": {
                    "pi@ok.ac.uk": "admin",
                    "person@elsewhere.com": "user",
                },
            },
            last_confirmed_details={"allowed_domains": ["*.ac.uk"]},
        )
        self.other = models.RemoteProject.objects.create(
            destination=DESTINATION,
            identifier="other.someportal",
            current_project=self.fixture.project,
            last_sent_details={"allocation": "50 GPUh"},
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_list_and_organisation_filter_succeed(self):
        url = reverse("openportal-remote-project-list")
        for params in ({}, {"customer_uuid": self.fixture.customer.uuid.hex}):
            response = self.client.get(url, params)
            self.assertEqual(response.status_code, 200, params)
            rows = {row["uuid"]: row for row in response.data}
            self.assertEqual(
                rows[self.outside.uuid.hex]["award_details"]["members"],
                {"pi@ok.ac.uk": "admin", "person@elsewhere.com": "user"},
            )
            self.assertEqual(rows[self.other.uuid.hex]["allocation_string"], "50 gpuh")

    def test_detail_succeeds(self):
        url = reverse(
            "openportal-remote-project-detail", kwargs={"uuid": self.outside.uuid.hex}
        )
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["allocation_string"], "100 gpuh")

    def test_details_are_derived_once_per_row_and_logged_without_the_reason(self):
        """
        The library's message names the member's email, so it stays out of
        the log; the remote project uuid is enough to find the row.
        """
        with (
            mock.patch.object(
                models.RemoteProject,
                "award_details",
                autospec=True,
                side_effect=OSError("Email 'person@elsewhere.com' is not allowed"),
            ) as award_details,
            self.assertLogs("waldur_openportal.serializers", level="WARNING") as logs,
        ):
            response = self.client.get(reverse("openportal-remote-project-list"))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(award_details.call_count, 2)
        output = "\n".join(logs.output)
        self.assertNotIn("person@elsewhere.com", output)
        self.assertIn(self.outside.uuid.hex, output)
        self.assertTrue(all(record.startswith("WARNING") for record in logs.output))

    def test_a_row_that_cannot_derive_its_details_renders_them_as_null(self):
        with mock.patch.object(
            models.RemoteProject,
            "award_details",
            side_effect=OSError("cannot parse"),
        ):
            response = self.client.get(reverse("openportal-remote-project-list"))

        self.assertEqual(response.status_code, 200)
        for row in response.data:
            self.assertIsNone(row["award_details"])
            self.assertIsNone(row["allocation_string"])


class UnsupportedCommandTest(TestCase):
    """
    Older remote portals reject commands they do not know about.  The fallback
    depends on a real exception class existing, otherwise the except clause
    raises AttributeError while handling the original error.
    """

    def setUp(self):
        config_patcher = mock.patch(
            "waldur_openportal.config.ensure_config_loaded", return_value=True
        )
        config_patcher.start()
        self.addCleanup(config_patcher.stop)

        self.board = OpenPortalBoard(DESTINATION)

        client_patcher = mock.patch(
            "waldur_openportal.remoteclient.RemoteOpenPortalClient"
        )
        self.client_class = client_patcher.start()
        self.addCleanup(client_patcher.stop)
        self.client = self.client_class.return_value

    def test_exception_is_a_openportal_error(self):
        self.assertTrue(
            issubclass(
                exceptions.OpenPortalUnsupportedCommandError,
                exceptions.OpenPortalError,
            )
        )

    def test_unknown_command_is_translated(self):
        self.client.get_award.side_effect = exceptions.OpenPortalOtherError(
            "Unknown command get_award"
        )

        with self.assertRaises(exceptions.OpenPortalUnsupportedCommandError):
            self.board.refetch_award("someproject.someportal")

    def test_other_errors_are_not_translated(self):
        self.client.get_award.side_effect = exceptions.OpenPortalOtherError(
            "The bridge is on fire"
        )

        with self.assertRaises(exceptions.OpenPortalOtherError) as cm:
            self.board.refetch_award("someproject.someportal")

        self.assertNotIsInstance(
            cm.exception, exceptions.OpenPortalUnsupportedCommandError
        )
