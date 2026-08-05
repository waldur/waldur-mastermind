import datetime
from unittest import mock

from django.test import TestCase
from django.utils import timezone

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
