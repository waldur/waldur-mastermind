"""Attachments of a ticket routed to a provider helpdesk.

The provider's agents work on the child issue routing creates, so the files
the customer attached to the parent have to reach it, and the provider's own
files have to come back, the way public comments already travel.
"""

from datetime import timedelta
from unittest import mock

from celery.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.core.files.base import ContentFile
from django.db import DatabaseError, OperationalError, connection
from django.db.models import QuerySet
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.core.enums import CoreStates
from waldur_core.core.tests.helpers import load_json_resource
from waldur_core.media import models as media_models
from waldur_core.media.utils import dummy_image
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.tests.pat_utils import STAFF, use_pat
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.support import backend, models, tasks
from waldur_mastermind.support.backend.atlassian import (
    AttachmentSynchronizer,
    ServiceDeskBackend,
)
from waldur_mastermind.support.backend.basic import BasicBackend
from waldur_mastermind.support.backend.smax import SmaxServiceBackend
from waldur_mastermind.support.backend.zammad import ZammadServiceBackend
from waldur_mastermind.support.extension import SupportExtension
from waldur_mastermind.support.tests import factories
from waldur_mastermind.support.tests.test_provider_api import ProviderHelpdeskBaseTest

PROVIDER_BACKEND = "waldur_mastermind.support.backend.get_backend_for_provider"

CUSTOMER_FILES = [b"kernel panic at boot", b"vm.swappiness = 10"]


def read(attachment):
    with attachment.file.open("rb") as file:
        return file.read()


def contents(issue):
    return contents_of(issue.attachments.all())


def contents_of(attachments):
    return sorted(read(attachment) for attachment in attachments)


def events(issue):
    issue.refresh_from_db()
    return [entry["event"] for entry in issue.processing_log]


# Forwarding runs in Celery tasks, which the tests run in place.
@override_settings(task_always_eager=True)
class RoutedAttachmentsBaseTest(ProviderHelpdeskBaseTest):
    def setUp(self):
        super().setUp()
        offering = marketplace_factories.OfferingFactory(customer=self.customer)
        resource = marketplace_factories.ResourceFactory(offering=offering)
        self.issue = factories.IssueFactory(
            resource_content_type=ContentType.objects.get_for_model(resource),
            resource_object_id=resource.id,
            backend_id="WLD-800",
            customer=self.fixture.customer,
            project=self.fixture.project,
        )
        author = factories.SupportUserFactory(user=self.issue.caller)
        self.logs = factories.AttachmentFactory(
            issue=self.issue,
            author=author,
            file__filename="logs.txt",
            file__data=b"kernel panic at boot",
        )
        self.config = factories.AttachmentFactory(
            issue=self.issue,
            author=author,
            file__filename="config.txt",
            file__data=b"vm.swappiness = 10",
        )
        self.agent = factories.ProviderSupportUserFactory(
            provider_helpdesk=self.helpdesk
        ).user

        # Every desk gives a file it takes an id of its own.
        self.mock_get_active_backend().create_attachment.side_effect = (
            BasicBackend().create_attachment
        )
        # The provider's own backend, recording what reaches it.
        self.provider_backend = mock.Mock(wraps=BasicBackend())
        patcher = mock.patch(PROVIDER_BACKEND, return_value=self.provider_backend)
        patcher.start()

    def route(self):
        tasks.route_issue_to_provider(self.issue.id)
        return self.issue.child_issues.get()

    def reroute(self, helpdesk):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.IssueFactory.get_url(self.issue, action="reroute"),
            {"provider_helpdesk": helpdesk.uuid.hex},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return self.issue.child_issues.get()

    def pushed(self):
        return [
            call.args[0] for call in self.provider_backend.create_attachment.mock_calls
        ]

    def attach(self, user, issue, data=None):
        self.client.force_authenticate(user)
        response = self.client.post(
            factories.AttachmentFactory.get_list_url(),
            {
                "issue": factories.IssueFactory.get_url(issue),
                "file": data or dummy_image(),
            },
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        return models.Attachment.objects.get(uuid=response.data["uuid"])


class AttachmentsOnRoutingTest(RoutedAttachmentsBaseTest):
    def test_routing_copies_the_parents_attachments_to_the_child(self):
        child = self.route()

        self.assertEqual(contents(child), contents(self.issue))

    def test_copies_are_pushed_to_the_providers_backend(self):
        child = self.route()

        copies = list(child.attachments.all())
        self.assertEqual(len(copies), 2)
        self.assertCountEqual(self.pushed(), copies)
        for copy in copies:
            self.assertTrue(copy.backend_id)

    def test_manual_routing_copies_the_parents_attachments(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            factories.IssueFactory.get_url(self.issue, action="route_to_provider"),
            {"provider_helpdesk": self.helpdesk.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        child = self.issue.child_issues.get()
        self.assertEqual(contents(child), contents(self.issue))
        self.assertCountEqual(self.pushed(), list(child.attachments.all()))

    def test_manual_routing_leaves_the_copying_to_a_task(self):
        # The request only creates the ticket; the files follow once it is
        # committed, in a task that can retry a desk that is down.
        with mock.patch.object(tasks.forward_parent_attachments, "delay") as delay:
            self.client.force_authenticate(self.fixture.staff)
            response = self.client.post(
                factories.IssueFactory.get_url(self.issue, action="route_to_provider"),
                {"provider_helpdesk": self.helpdesk.uuid.hex},
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        child = self.issue.child_issues.get()
        delay.assert_called_once_with(child.id)
        self.assertEqual(child.attachments.count(), 0)

    def test_reroute_copies_the_parents_attachments_to_the_new_child(self):
        self.route()
        other_helpdesk = factories.ProviderHelpdeskFactory()

        child = self.reroute(other_helpdesk)

        self.assertEqual(child.provider_helpdesk, other_helpdesk)
        self.assertEqual(contents(child), CUSTOMER_FILES)

    def test_reroute_deletes_the_files_of_the_withdrawn_copies(self):
        old_child = self.route()
        withdrawn = [attachment.file.name for attachment in old_child.attachments.all()]

        self.reroute(factories.ProviderHelpdeskFactory())

        self.assertFalse(media_models.File.objects.filter(name__in=withdrawn).exists())
        self.assertEqual(contents(self.issue), CUSTOMER_FILES)

    def test_each_copy_has_a_file_of_its_own(self):
        child = self.route()

        original_names = {self.logs.file.name, self.config.file.name}
        for copy in child.attachments.all():
            self.assertNotIn(copy.file.name, original_names)

        # Erasing the customer's file from their ticket leaves the provider's
        # copy readable, and the other way round.
        self.logs.file.delete(save=False)
        self.logs.delete()
        self.assertEqual(contents(child), CUSTOMER_FILES)

    def test_a_desk_that_fails_for_a_while_gets_the_file_on_a_retry(self):
        failures = []

        def create_attachment(attachment):
            if read(attachment) == b"vm.swappiness = 10" and len(failures) < 2:
                failures.append(attachment)
                raise ConnectionError("The provider's desk is not answering.")
            BasicBackend().create_attachment(attachment)

        self.provider_backend.create_attachment.side_effect = create_attachment

        child = self.route()

        self.assertEqual(len(failures), 2)
        self.assertEqual(contents(child), CUSTOMER_FILES)
        self.assertNotIn("attachment_not_forwarded", events(self.issue))

    def test_a_file_the_provider_refuses_does_not_hold_the_routing_back(self):
        def create_attachment(attachment):
            if read(attachment) == b"vm.swappiness = 10":
                raise ValueError("Attachment type is not allowed.")
            BasicBackend().create_attachment(attachment)

        self.provider_backend.create_attachment.side_effect = create_attachment

        child = self.route()

        refused = child.attachments.get(forwarded_from=self.config)
        self.assertEqual(refused.state, CoreStates.CREATION_SCHEDULED)
        self.assertEqual(
            contents_of(child.attachments.filter(state=CoreStates.OK)),
            [b"kernel panic at boot"],
        )
        self.assertIn("attachment_not_forwarded", events(self.issue))
        self.assertIn("routed_to_provider", events(self.issue))


class OneCopyPerTicketTest(RoutedAttachmentsBaseTest):
    """Routing and forwarding race each other; each file reaches a ticket once."""

    def test_upload_before_the_ticket_is_routed_is_forwarded_once(self):
        with mock.patch.object(tasks.forward_attachment_to_child, "delay") as delay:
            added = self.attach(self.fixture.owner, self.issue)
        # Scheduled although the ticket has no child yet: by the time the
        # task runs, routing may have created one.
        delay.assert_called_once_with(added.id)

        child = self.route()
        tasks.forward_attachment_to_child.delay(added.id)

        self.assertEqual(contents(child).count(read(added)), 1)

    def test_upload_while_routing_is_copied_once(self):
        def create_issue(child):
            BasicBackend().create_issue(child)
            # The customer's upload commits while the provider's desk is
            # still answering: the child exists, its files are not copied yet.
            self.attach(
                self.fixture.owner, self.issue, ContentFile(b"late", "late.txt")
            )

        self.provider_backend.create_issue.side_effect = create_issue

        child = self.route()

        self.assertEqual(contents(child).count(b"late"), 1)

    def test_forwarding_a_file_again_makes_no_second_copy(self):
        child = self.route()
        added = self.attach(self.fixture.owner, self.issue)

        tasks.forward_attachment_to_child.delay(added.id)
        tasks.forward_parent_attachments.delay(child.id)

        self.assertEqual(contents(child).count(read(added)), 1)
        self.assertEqual(child.attachments.count(), 3)

    def test_forwarding_that_runs_after_a_reroute_makes_no_second_copy(self):
        self.route()
        with mock.patch.object(tasks.forward_attachment_to_child, "delay") as delay:
            added = self.attach(self.fixture.owner, self.issue)

        child = self.reroute(factories.ProviderHelpdeskFactory())
        tasks.forward_attachment_to_child.delay(delay.call_args.args[0])

        self.assertEqual(contents(child).count(read(added)), 1)

    def test_a_copy_records_its_original(self):
        child = self.route()

        self.assertCountEqual(
            child.attachments.values_list("forwarded_from", flat=True),
            [self.logs.id, self.config.id],
        )


class InternalFilesTest(RoutedAttachmentsBaseTest):
    """A file from an internal note stays with the operator, as the note does."""

    def sync_from_zammad(self, is_public, filename, data):
        zammad = ZammadServiceBackend()
        zammad.manager = mock.Mock()
        zammad.manager.get_comment.return_value = mock.Mock(
            attachments=[mock.Mock(id=filename, filename=filename, size=len(data))]
        )
        zammad.manager.attachment_download.return_value = data
        note = factories.CommentFactory(issue=self.issue, is_public=is_public)
        zammad.add_waldur_attachments_from_zammad(note)
        return self.issue.attachments.get(backend_id=filename)

    def test_zammad_marks_the_files_of_internal_articles(self):
        internal = self.sync_from_zammad(False, "internal.txt", b"operator only")
        public = self.sync_from_zammad(True, "public.txt", b"for everyone")

        self.assertFalse(internal.is_public)
        self.assertTrue(public.is_public)

    def test_routing_does_not_copy_internal_files(self):
        self.sync_from_zammad(False, "internal.txt", b"operator only")

        child = self.route()

        self.assertEqual(contents(child), CUSTOMER_FILES)

    def test_internal_file_synced_after_routing_is_not_forwarded(self):
        child = self.route()

        self.sync_from_zammad(False, "internal.txt", b"operator only")

        self.assertEqual(contents(child), CUSTOMER_FILES)

    def test_public_file_synced_after_routing_is_forwarded(self):
        # The syncs save the row first and the file afterwards; forwarding
        # waits for the file.
        child = self.route()

        self.sync_from_zammad(True, "public.txt", b"for everyone")

        self.assertEqual(contents(child), sorted([*CUSTOMER_FILES, b"for everyone"]))

    def test_internal_file_on_a_child_is_not_propagated(self):
        child = self.route()

        factories.AttachmentFactory(issue=child, is_public=False)

        self.assertEqual(contents(self.issue), CUSTOMER_FILES)


class ProviderAgentAccessTest(RoutedAttachmentsBaseTest):
    def test_provider_agent_lists_and_downloads_the_copies(self):
        child = self.route()

        self.client.force_authenticate(self.agent)
        response = self.client.get(
            factories.AttachmentFactory.get_list_url(),
            {"issue_uuid": child.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        downloaded = []
        for item in response.data:
            download = self.client.get(item["file"])
            self.assertEqual(download.status_code, status.HTTP_200_OK)
            downloaded.append(download.content)
        self.assertEqual(sorted(downloaded), CUSTOMER_FILES)

    def test_provider_agent_sees_files_on_a_ticket_routed_to_them(self):
        # Whatever put the file on the routed copy: the agents work that
        # ticket, and see its files as they see its comments.
        child = factories.IssueFactory(
            parent_issue=self.issue,
            provider_helpdesk=self.helpdesk,
            customer=self.issue.customer,
            project=self.issue.project,
        )
        attachment = factories.AttachmentFactory(
            issue=child, file__filename="trace.txt", file__data=b"stack trace"
        )

        self.client.force_authenticate(self.agent)
        response = self.client.get(factories.AttachmentFactory.get_url(attachment))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        download = self.client.get(response.data["file"])
        self.assertEqual(download.status_code, status.HTTP_200_OK)
        self.assertEqual(download.content, b"stack trace")

    def test_provider_agent_does_not_see_the_parents_own_attachments(self):
        self.route()

        self.client.force_authenticate(self.agent)
        response = self.client.get(
            factories.AttachmentFactory.get_list_url(),
            {"issue_uuid": self.issue.uuid.hex},
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, [])
        self.assertEqual(
            self.client.get(factories.AttachmentFactory.get_url(self.logs)).status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_inactive_agent_does_not_see_the_copies(self):
        child = self.route()
        models.ProviderSupportUser.objects.filter(user=self.agent).update(
            is_active=False
        )

        self.client.force_authenticate(self.agent)
        response = self.client.get(
            factories.AttachmentFactory.get_list_url(),
            {"issue_uuid": child.uuid.hex},
        )

        self.assertEqual(response.data, [])


class BackendOfTheTicketTest(RoutedAttachmentsBaseTest):
    """What is on a routed copy is held by the provider's desk, so changing or
    removing it goes there, never to the operator's."""

    def setUp(self):
        super().setUp()
        self.child = self.route()
        self.copy = self.child.attachments.first()
        self.operator = self.mock_get_active_backend()
        self.client.force_authenticate(self.fixture.staff)

    def test_deleting_a_copy_goes_to_the_providers_backend(self):
        response = self.client.delete(factories.AttachmentFactory.get_url(self.copy))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        deleted = [
            c.args[0].uuid for c in self.provider_backend.delete_attachment.mock_calls
        ]
        self.assertEqual(deleted, [self.copy.uuid])
        self.operator.delete_attachment.assert_not_called()

    def test_a_copy_asks_the_providers_backend_whether_it_may_be_deleted(self):
        self.provider_backend.attachment_destroy_is_available.return_value = False
        self.operator.attachment_destroy_is_available.return_value = True
        url = factories.AttachmentFactory.get_url(self.copy)

        self.assertFalse(self.client.get(url).data["destroy_is_available"])
        self.assertEqual(
            self.client.delete(url).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.assertTrue(models.Attachment.objects.filter(id=self.copy.id).exists())

    def test_comment_on_a_routed_copy_changes_through_the_providers_backend(self):
        comment = factories.CommentFactory(issue=self.child)
        url = factories.CommentFactory.get_url(comment)

        response = self.client.patch(url, {"description": "corrected"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

        self.provider_backend.update_comment.assert_called_once()
        self.provider_backend.delete_comment.assert_called_once()
        self.operator.update_comment.assert_not_called()
        self.operator.delete_comment.assert_not_called()

    def test_comment_on_a_routed_copy_asks_the_providers_backend(self):
        comment = factories.CommentFactory(issue=self.child)
        self.provider_backend.comment_update_is_available.return_value = False
        self.provider_backend.comment_destroy_is_available.return_value = False
        url = factories.CommentFactory.get_url(comment)

        data = self.client.get(url).data
        self.assertFalse(data["update_is_available"])
        self.assertFalse(data["destroy_is_available"])
        self.assertEqual(
            self.client.patch(url, {"description": "corrected"}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.delete(url).status_code, status.HTTP_400_BAD_REQUEST
        )


class HelpdeskBackendNameTest(RoutedAttachmentsBaseTest):
    """Every desk numbers its attachments from its own sequence."""

    def number_like_jira(self):
        sequences = {}

        def create_attachment(attachment):
            helpdesk_id = attachment.issue.provider_helpdesk_id
            sequences[helpdesk_id] = sequences.get(helpdesk_id, 10041) + 1
            attachment.backend_id = str(sequences[helpdesk_id])
            attachment.save(update_fields=["backend_id"])

        self.provider_backend.create_attachment.side_effect = create_attachment

    def test_copies_on_two_desks_may_carry_the_same_remote_id(self):
        self.number_like_jira()
        first = self.route()
        other_parent = factories.IssueFactory(customer=self.fixture.customer)
        factories.AttachmentFactory(issue=other_parent, file__data=b"other ticket")
        other_helpdesk = factories.ProviderHelpdeskFactory()

        self.client.force_authenticate(self.fixture.staff)
        self.client.post(
            factories.IssueFactory.get_url(other_parent, action="route_to_provider"),
            {"provider_helpdesk": other_helpdesk.uuid.hex},
        )

        second = other_parent.child_issues.get()
        self.assertEqual(contents(second), [b"other ticket"])
        self.assertIn("10042", first.attachments.values_list("backend_id", flat=True))
        self.assertEqual(second.attachments.get().backend_id, "10042")
        self.assertNotIn("attachment_not_forwarded", events(other_parent))

    def test_attachments_of_a_routed_copy_carry_the_helpdesks_name(self):
        child = self.route()
        uploaded = self.attach(self.fixture.staff, child)

        expected = f"basic:{self.helpdesk.uuid.hex}"
        for attachment in child.attachments.all():
            self.assertEqual(attachment.backend_name, expected)
        self.assertEqual(uploaded.backend_name, expected)


class AttachmentsAfterRoutingTest(RoutedAttachmentsBaseTest):
    def test_file_the_customer_adds_later_reaches_the_child(self):
        child = self.route()
        routed = list(child.attachments.values_list("id", flat=True))
        self.provider_backend.create_attachment.reset_mock()

        added = self.attach(self.fixture.owner, self.issue)

        copy = child.attachments.exclude(id__in=routed).get()
        self.assertEqual(read(copy), read(added))
        self.assertEqual(self.pushed(), [copy])
        # The copy does not travel back to the parent.
        self.assertEqual(self.issue.attachments.count(), 3)

    def test_file_staff_add_to_the_child_reaches_the_parent(self):
        # Staff, as only staff may attach to a routed copy on develop; the
        # provider's agents may once attaching follows comment rights.
        child = self.route()
        self.provider_backend.create_attachment.reset_mock()

        added = self.attach(self.fixture.staff, child)

        # Pushed to the provider's backend, which owns the child.
        self.assertEqual(self.pushed(), [added])
        copy = self.issue.attachments.exclude(
            id__in=[self.logs.id, self.config.id]
        ).get()
        self.assertEqual(read(copy), read(added))
        self.assertNotEqual(copy.file.name, added.file.name)
        # The copy does not travel back to the child.
        self.assertEqual(child.attachments.count(), 3)

    def test_reroute_leaves_the_old_providers_files_behind(self):
        old_child = self.route()
        self.attach(self.fixture.staff, old_child)
        self.assertEqual(self.issue.attachments.count(), 3)

        # The new provider gets what the customer attached, not what the
        # provider it replaces sent back, as with comments.
        child = self.reroute(factories.ProviderHelpdeskFactory())

        self.assertEqual(contents(child), CUSTOMER_FILES)


class AttachmentHandlersTest(RoutedAttachmentsBaseTest):
    def setUp(self):
        super().setUp()
        self.child = self.route()

    @mock.patch("waldur_mastermind.support.tasks.forward_attachment_to_child.delay")
    def test_new_file_on_the_parent_is_dispatched(self, forward):
        attachment = factories.AttachmentFactory(issue=self.issue)

        forward.assert_called_once_with(attachment.id)

    @mock.patch("waldur_mastermind.support.tasks.propagate_attachment_to_parent.delay")
    def test_new_file_on_the_child_is_dispatched(self, propagate):
        attachment = factories.AttachmentFactory(issue=self.child)

        propagate.assert_called_once_with(attachment.id)

    @mock.patch("waldur_mastermind.support.tasks.propagate_attachment_to_parent.delay")
    @mock.patch("waldur_mastermind.support.tasks.forward_attachment_to_child.delay")
    def test_forwarded_file_is_not_dispatched_again(self, forward, propagate):
        factories.AttachmentFactory(issue=self.issue, is_forwarded=True)
        factories.AttachmentFactory(issue=self.child, is_forwarded=True)

        forward.assert_not_called()
        propagate.assert_not_called()

    @mock.patch("waldur_mastermind.support.tasks.forward_attachment_to_child.delay")
    def test_file_saved_after_its_row_is_dispatched_with_the_file(self, forward):
        # How the Zammad, SMAX and Atlassian syncs store what they pull in.
        attachment = models.Attachment.objects.create(
            issue=self.issue, backend_id="remote-1"
        )
        forward.assert_not_called()

        attachment.file.save("pulled.txt", ContentFile(b"from the desk"))

        forward.assert_called_once_with(attachment.id)


class ProviderDeskTest(TestCase):
    """The remote desks a provider runs."""

    def setUp(self):
        self.helpdesk = factories.ProviderHelpdeskFactory(
            backend_type=models.ProviderHelpdesk.BackendTypes.ZAMMAD
        )
        self.child = factories.IssueFactory(
            parent_issue=factories.IssueFactory(),
            provider_helpdesk=self.helpdesk,
            backend_id="900",
        )

    def test_zammad_desk_of_a_provider_files_attachments_under_its_own_name(self):
        zammad = backend.get_backend_for_provider(self.helpdesk)
        zammad.manager = mock.Mock()
        zammad.manager.add_attachment.return_value = mock.Mock(id="55")
        attachment = factories.AttachmentFactory(issue=self.child, backend_id="")

        zammad.create_attachment(attachment)
        self.assertEqual(attachment.backend_name, f"zammad:{self.helpdesk.uuid.hex}")
        self.assertEqual(attachment.backend_id, "55")

        zammad.manager.get_ticket_attachments.return_value = [
            mock.Mock(id="55", article_id="9")
        ]
        zammad.delete_attachment(attachment)
        zammad.manager.del_comment.assert_called_once_with("9")

    def test_smax_desk_of_a_provider_files_attachments_under_its_own_name(self):
        helpdesk = factories.ProviderHelpdeskFactory(
            backend_type=models.ProviderHelpdesk.BackendTypes.SMAX
        )
        child = factories.IssueFactory(
            parent_issue=factories.IssueFactory(),
            provider_helpdesk=helpdesk,
            backend_id="901",
            backend_name="smax",
        )
        smax = backend.get_backend_for_provider(helpdesk)
        smax.manager = mock.Mock()
        smax.manager.create_attachment.return_value = mock.Mock(id="88")
        smax.get_smax_user_id_for_support_user = mock.Mock(return_value="7")
        attachment = factories.AttachmentFactory(
            issue=child,
            author=factories.SupportUserFactory(),
            backend_id="",
            file__data=b"report",
        )

        smax.create_attachment(attachment)
        self.assertEqual(attachment.backend_name, f"smax:{helpdesk.uuid.hex}")

        smax.delete_attachment(attachment)
        smax.manager.delete_attachment.assert_called_once_with("901", "88")

    def test_smax_uploads_a_file_without_an_author_as_the_tickets_reporter(self):
        helpdesk = factories.ProviderHelpdeskFactory(
            backend_type=models.ProviderHelpdesk.BackendTypes.SMAX
        )
        reporter = factories.SupportUserFactory()
        child = factories.IssueFactory(
            parent_issue=factories.IssueFactory(),
            provider_helpdesk=helpdesk,
            backend_id="902",
            backend_name="smax",
            reporter=reporter,
        )
        smax = backend.get_backend_for_provider(helpdesk)
        smax.manager = mock.Mock()
        smax.manager.create_attachment.return_value = mock.Mock(id="89")
        smax.get_smax_user_id_for_support_user = mock.Mock(return_value="7")
        # The purchase order a marketplace order attaches has no author.
        attachment = factories.AttachmentFactory(
            issue=child, author=None, backend_id="", file__data=b"order"
        )

        smax.create_attachment(attachment)

        smax.get_smax_user_id_for_support_user.assert_called_once_with(reporter)
        self.assertEqual(attachment.backend_id, "89")

    def test_smax_refuses_a_file_it_has_nobody_to_upload_as(self):
        helpdesk = factories.ProviderHelpdeskFactory(
            backend_type=models.ProviderHelpdesk.BackendTypes.SMAX
        )
        child = factories.IssueFactory(
            parent_issue=factories.IssueFactory(),
            provider_helpdesk=helpdesk,
            backend_id="903",
            backend_name="smax",
            reporter=None,
        )
        smax = backend.get_backend_for_provider(helpdesk)
        smax.manager = mock.Mock()
        attachment = factories.AttachmentFactory(
            issue=child, author=None, backend_id="", file__data=b"order"
        )

        with self.assertRaises(backend.SupportBackendError):
            smax.create_attachment(attachment)
        smax.manager.create_attachment.assert_not_called()

    def test_jira_sync_deletes_only_the_synced_tickets_stale_attachments(self):
        # Ids are only unique within one desk: the same number on another
        # ticket is another desk's attachment.
        parent = factories.IssueFactory(key="SD-1")
        stale = factories.AttachmentFactory(issue=parent, backend_id="10042")
        elsewhere = factories.AttachmentFactory(
            issue=self.child,
            backend_id="10042",
            backend_name=f"atlassian:{self.helpdesk.uuid.hex}",
        )
        jira = mock.Mock(api_version=2)
        jira.get.return_value = {"fields": {"attachment": []}}

        waiting = factories.AttachmentFactory(
            issue=parent,
            is_forwarded=True,
            backend_id="WLD-AF-0002",
            state=CoreStates.CREATING,
        )

        AttachmentSynchronizer(jira, parent, {}).perform_update()

        self.assertFalse(models.Attachment.objects.filter(id=stale.id).exists())
        self.assertTrue(models.Attachment.objects.filter(id=elsewhere.id).exists())
        # Not on the desk yet, as it is being pushed there.
        self.assertTrue(models.Attachment.objects.filter(id=waiting.id).exists())


class UploadedOnceTest(RoutedAttachmentsBaseTest):
    """A copy is committed before it is pushed, so a retry never sends a desk a
    file it has taken already."""

    def test_a_file_the_desk_took_is_not_sent_again_when_recording_it_fails(self):
        uploads = []

        def create_attachment(attachment):
            uploads.append(read(attachment))
            attachment.backend_id = f"remote-{len(uploads)}"
            # The desk has the file; saving its id is what fails.
            raise DatabaseError("connection lost")

        self.provider_backend.create_attachment.side_effect = create_attachment

        child = self.route()

        self.assertEqual(sorted(uploads), CUSTOMER_FILES)
        self.assertCountEqual(
            child.attachments.values_list("backend_id", "state"),
            [("remote-1", CoreStates.OK), ("remote-2", CoreStates.OK)],
        )
        self.assertNotIn("attachment_not_forwarded", events(self.issue))

    def test_a_copy_the_desk_refuses_stays_for_the_next_attempt(self):
        self.provider_backend.create_attachment.side_effect = ConnectionError("down")

        child = self.route()

        copies = child.attachments.all()
        self.assertEqual(len(copies), 2)
        for copy in copies:
            self.assertEqual(copy.state, CoreStates.CREATION_SCHEDULED)
        self.issue.refresh_from_db()
        logged = [
            entry["details"]["copy_uuid"]
            for entry in self.issue.processing_log
            if entry["event"] == "attachment_not_forwarded"
        ]
        self.assertCountEqual(logged, [copy.uuid.hex for copy in copies])

        # The desk is back: the next attempt pushes what is still waiting.
        self.provider_backend.create_attachment.side_effect = None
        tasks.forward_parent_attachments.delay(child.id)
        self.assertEqual(
            set(child.attachments.values_list("state", flat=True)), {CoreStates.OK}
        )
        self.assertEqual(len(self.pushed()), 4 * 2 + 2)

    def test_a_desk_that_takes_nothing_is_a_failure(self):
        # As SMAX does for a file it has no user to upload as.
        self.provider_backend.create_attachment.side_effect = lambda attachment: None

        child = self.route()

        self.assertEqual(
            set(child.attachments.values_list("state", flat=True)),
            {CoreStates.CREATION_SCHEDULED},
        )
        self.assertIn("attachment_not_forwarded", events(self.issue))

    def test_a_copy_waiting_for_the_desk_is_not_shown_to_the_provider(self):
        self.provider_backend.create_attachment.side_effect = ConnectionError("down")
        child = self.route()

        self.client.force_authenticate(self.agent)
        response = self.client.get(
            factories.AttachmentFactory.get_list_url(), {"issue_uuid": child.uuid.hex}
        )
        self.assertEqual(response.data, [])

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(
            factories.AttachmentFactory.get_list_url(), {"issue_uuid": child.uuid.hex}
        )
        self.assertEqual(len(response.data), 2)


class InternalFilesVisibilityTest(RoutedAttachmentsBaseTest):
    """A file from an internal note is hidden as the note is: from everyone
    but staff and whoever attached it."""

    def setUp(self):
        super().setUp()
        self.internal = factories.AttachmentFactory(
            issue=self.issue, is_public=False, file__data=b"operator only"
        )

    def visible_to(self, user=None):
        if user is not None:
            self.client.force_authenticate(user)
        response = self.client.get(factories.AttachmentFactory.get_list_url())
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return {item["uuid"] for item in response.data}

    def download(self, attachment):
        return self.client.get(attachment.file.url).status_code

    def test_customer_does_not_see_or_download_an_internal_file(self):
        visible = self.visible_to(self.fixture.owner)
        self.assertIn(self.logs.uuid.hex, visible)
        self.assertNotIn(self.internal.uuid.hex, visible)
        self.assertEqual(self.download(self.internal), status.HTTP_404_NOT_FOUND)
        self.assertNotIn(self.internal.uuid.hex, self.visible_to(self.issue.caller))

    def test_the_author_and_staff_see_it(self):
        author = factories.SupportUserFactory(user=self.fixture.owner)
        self.internal.author = author
        self.internal.save(update_fields=["author"])

        self.assertIn(self.internal.uuid.hex, self.visible_to(self.fixture.owner))
        self.assertIn(self.internal.uuid.hex, self.visible_to(self.fixture.staff))
        self.assertEqual(self.download(self.internal), status.HTTP_200_OK)

    @override_config(PAT_ENABLED=True)
    def test_a_staff_token_without_the_staff_scope_does_not_see_it(self):
        # Without the scope the token is held to the user's own roles, here
        # those of an organization owner.
        self.fixture.customer.add_user(self.fixture.staff, CustomerRole.OWNER)

        use_pat(self.client, self.fixture.staff)
        self.assertIn(self.logs.uuid.hex, self.visible_to())
        self.assertNotIn(self.internal.uuid.hex, self.visible_to())

        use_pat(self.client, self.fixture.staff, STAFF)
        self.assertIn(self.internal.uuid.hex, self.visible_to())

    def test_customer_does_not_see_the_providers_internal_file_on_the_child(self):
        child = self.route()
        provider_note = factories.AttachmentFactory(
            issue=child, is_public=False, file__data=b"provider only"
        )

        self.assertNotIn(provider_note.uuid.hex, self.visible_to(self.fixture.owner))
        self.assertNotIn(provider_note.uuid.hex, self.visible_to(self.issue.caller))


class HandlerCostTest(TestCase):
    """Forwarding costs nothing where nothing can be routed."""

    def test_a_save_that_adds_no_file_asks_nothing_and_schedules_nothing(self):
        factories.ProviderHelpdeskFactory()
        attachment = factories.AttachmentFactory(file__data=b"x")
        attachment = models.Attachment.objects.select_related("issue").get(
            id=attachment.id
        )

        with mock.patch.object(tasks.forward_attachment_to_child, "delay") as delay:
            with CaptureQueriesContext(connection) as queries:
                attachment.save()

        delay.assert_not_called()
        self.assertFalse(
            [
                q
                for q in queries.captured_queries
                if "support_providerhelpdesk" in q["sql"]
            ]
        )

    def test_with_only_an_inactive_helpdesk_an_upload_schedules_nothing(self):
        factories.ProviderHelpdeskFactory(is_active=False)

        with mock.patch.object(tasks.forward_attachment_to_child, "delay") as delay:
            factories.AttachmentFactory(file__data=b"x")

        delay.assert_not_called()

    def test_a_ticket_routed_to_a_helpdesk_since_deactivated_still_forwards(self):
        helpdesk = factories.ProviderHelpdeskFactory(is_active=False)
        parent = factories.IssueFactory()
        factories.IssueFactory(parent_issue=parent, provider_helpdesk=helpdesk)

        with mock.patch.object(tasks.forward_attachment_to_child, "delay") as delay:
            attachment = factories.AttachmentFactory(issue=parent, file__data=b"x")

        delay.assert_called_once_with(attachment.id)


@override_settings(task_always_eager=True)
class SharedDeskSyncTest(TestCase):
    """A provider helpdesk with no settings of its own uses the operator's
    desk, whose sync then also syncs the provider's tickets."""

    def setUp(self):
        self.reporter = factories.SupportUserFactory(
            backend_name="smax", backend_id="u1"
        )
        self.parent = factories.IssueFactory(
            backend_id="900", backend_name="smax", reporter=self.reporter
        )
        self.original = factories.AttachmentFactory(
            issue=self.parent,
            file__data=b"report",
            backend_id="77",
            backend_name="smax",
        )

    def route_to(self, backend_type, backend_id):
        self.helpdesk = factories.ProviderHelpdeskFactory(backend_type=backend_type)
        self.child = factories.IssueFactory(
            parent_issue=self.parent,
            provider_helpdesk=self.helpdesk,
            backend_id="901",
            backend_name=backend_type,
            reporter=self.reporter,
        )
        self.name = backend.provider_attachment_backend_name(self.helpdesk)
        # What forwarding leaves behind once the desk has taken the copy.
        return factories.AttachmentFactory(
            issue=self.child,
            forwarded_from=self.original,
            is_forwarded=True,
            backend_id=backend_id,
            backend_name=self.name,
            state=CoreStates.OK,
            file__data=b"report",
        )

    def sync_smax(self, remote_ids):
        smax = SmaxServiceBackend()
        smax.manager = mock.Mock()
        smax.manager.get_issue.return_value = mock.Mock(
            description="d",
            summary="s",
            status="open",
            comments=[],
            attachments=[
                mock.Mock(id=remote_id, backend_user_id="u1", filename="f.txt")
                for remote_id in remote_ids
            ],
        )
        smax.manager.get_user.return_value = mock.Mock(id="u1", name="Agent")
        smax.manager.attachment_download.return_value = b"pulled"
        with mock.patch(
            "waldur_mastermind.support.backend.get_active_backend",
            return_value=BasicBackend(),
        ):
            smax.update_waldur_issue_from_smax(self.child)

    def test_smax_sync_does_not_pull_a_copy_in_again(self):
        self.route_to("smax", "88")

        self.sync_smax(["88"])

        self.assertEqual(self.child.attachments.count(), 1)
        self.assertEqual(self.parent.attachments.count(), 1)

    def test_smax_sync_removes_a_copy_the_desk_no_longer_holds(self):
        copy = self.route_to("smax", "88")

        self.sync_smax([])

        self.assertFalse(models.Attachment.objects.filter(id=copy.id).exists())

    def test_smax_sync_leaves_a_copy_waiting_for_the_desk(self):
        copy = self.route_to("smax", "WLD-AF-0000")
        models.Attachment.objects.filter(id=copy.id).update(state=CoreStates.CREATING)

        self.sync_smax([])

        self.assertTrue(models.Attachment.objects.filter(id=copy.id).exists())

    def test_smax_sync_files_what_it_pulls_into_a_child_under_the_helpdesk(self):
        self.route_to("smax", "88")

        self.sync_smax(["88", "89"])

        pulled = self.child.attachments.get(backend_id="89")
        self.assertEqual(pulled.backend_name, self.name)

    def test_zammad_sync_removes_a_copy_the_desk_no_longer_holds(self):
        copy = self.route_to("zammad", "55")
        waiting = factories.AttachmentFactory(
            issue=self.child,
            is_forwarded=True,
            backend_id="WLD-AF-0001",
            backend_name=self.name,
            state=CoreStates.CREATING,
        )
        zammad = ZammadServiceBackend()
        zammad.manager = mock.Mock()
        zammad.manager.get_ticket_attachments.return_value = []

        zammad.del_waldur_attachments_from_zammad(self.child)

        self.assertFalse(models.Attachment.objects.filter(id=copy.id).exists())
        self.assertTrue(models.Attachment.objects.filter(id=waiting.id).exists())

    def test_zammad_sync_files_what_it_pulls_into_a_child_under_the_helpdesk(self):
        self.route_to("zammad", "55")
        zammad = ZammadServiceBackend()
        zammad.manager = mock.Mock()
        zammad.manager.get_comment.return_value = mock.Mock(
            attachments=[mock.Mock(id="56", filename="f.txt", size=4)]
        )
        zammad.manager.attachment_download.return_value = b"note"
        note = factories.CommentFactory(issue=self.child, is_public=True)

        with mock.patch(
            "waldur_mastermind.support.backend.get_active_backend",
            return_value=BasicBackend(),
        ):
            zammad.add_waldur_attachments_from_zammad(note)

        self.assertEqual(
            self.child.attachments.get(backend_id="56").backend_name, self.name
        )

    def synced_under_the_plain_name(self, backend_type, remote_id):
        self.route_to(backend_type, "1")
        synced = factories.AttachmentFactory(
            issue=self.child, backend_id=remote_id, backend_name=backend_type
        )
        desk = backend.get_backend_for_provider(self.helpdesk)
        desk.manager = mock.Mock()
        desk.manager.get_ticket_attachments.return_value = [
            mock.Mock(id=remote_id, article_id="9")
        ]
        desk.delete_attachment(synced)
        return desk

    def test_zammad_provider_desk_deletes_a_file_synced_under_the_plain_name(self):
        desk = self.synced_under_the_plain_name("zammad", "55")

        desk.manager.del_comment.assert_called_once_with("9")

    def test_smax_provider_desk_deletes_a_file_synced_under_the_plain_name(self):
        desk = self.synced_under_the_plain_name("smax", "88")

        desk.manager.delete_attachment.assert_called_once_with("901", "88")


class PushOutsideTransactionTest(RoutedAttachmentsBaseTest):
    """The desk is called with no transaction open: a slow desk holds no
    connection idle in a transaction, and a reroute's delete does not wait."""

    def test_the_desk_is_called_with_no_transaction_open(self):
        baseline = len(connection.atomic_blocks)
        seen = []

        def create_attachment(attachment):
            seen.append(len(connection.atomic_blocks))
            BasicBackend().create_attachment(attachment)

        self.provider_backend.create_attachment.side_effect = create_attachment

        self.route()

        self.assertEqual(seen, [baseline, baseline])

    def test_a_copy_is_claimed_before_the_desk_is_called(self):
        states = []

        def create_attachment(attachment):
            row = models.Attachment.objects.get(pk=attachment.pk)
            states.append((row.state, row.push_attempts))
            BasicBackend().create_attachment(attachment)

        self.provider_backend.create_attachment.side_effect = create_attachment

        self.route()

        self.assertEqual(states, [(CoreStates.CREATING, 1)] * 2)

    def test_a_lost_connection_while_recording_does_not_send_the_file_again(self):
        uploads = []

        def create_attachment(attachment):
            # The desk takes the file, and the backend saves its id.
            uploads.append(read(attachment))
            attachment.backend_id = f"remote-{len(uploads)}"
            attachment.save(update_fields=["backend_id"])

        self.provider_backend.create_attachment.side_effect = create_attachment
        real_update = QuerySet.update
        failures = []

        def update(queryset, **kwargs):
            # Recording the push, and marking it erred, both fail.
            if kwargs.get("state") in (CoreStates.OK, CoreStates.ERRED) and (
                queryset.model is models.Attachment and len(failures) < 4
            ):
                failures.append(kwargs["state"])
                raise OperationalError("server closed the connection")
            return real_update(queryset, **kwargs)

        with mock.patch.object(QuerySet, "update", update):
            child = self.route()

        self.assertEqual(sorted(uploads), CUSTOMER_FILES)
        self.assertEqual(
            set(child.attachments.values_list("state", flat=True)),
            {CoreStates.CREATING},
        )

        # Once the claim is stale, the periodic task finds the desk's id on
        # the row and records the push without sending the file again.
        with freeze_time(timezone.now() + timedelta(hours=3)):
            tasks.retry_attachment_forwarding()

        self.assertEqual(sorted(uploads), CUSTOMER_FILES)
        self.assertCountEqual(
            child.attachments.values_list("backend_id", "state"),
            [("remote-1", CoreStates.OK), ("remote-2", CoreStates.OK)],
        )


class StuckCopiesTest(RoutedAttachmentsBaseTest):
    """Copies a forwarding task leaves waiting, or a dead worker leaves
    claimed, are pushed again by a periodic task, a bounded number of times."""

    def route_while_the_desk_is_down(self):
        self.provider_backend.create_attachment.side_effect = ConnectionError("down")
        child = self.route()
        self.provider_backend.create_attachment.side_effect = None
        self.provider_backend.create_attachment.reset_mock()
        return child

    def test_the_periodic_task_is_scheduled(self):
        tasks_by_name = {
            entry["task"] for entry in SupportExtension.celery_tasks().values()
        }
        self.assertIn(
            "waldur_mastermind.support.retry_attachment_forwarding", tasks_by_name
        )

    def test_copies_left_waiting_are_pushed_once_the_desk_is_back(self):
        child = self.route_while_the_desk_is_down()

        # Not while a forwarding task may still be retrying them.
        tasks.retry_attachment_forwarding()
        self.assertEqual(self.pushed(), [])

        with freeze_time(timezone.now() + timedelta(hours=1)):
            tasks.retry_attachment_forwarding()

        self.assertEqual(
            set(child.attachments.values_list("state", flat=True)), {CoreStates.OK}
        )
        self.assertEqual(len(self.pushed()), 2)

    def test_a_copy_claimed_by_a_worker_that_died_is_pushed_again(self):
        child = self.route_while_the_desk_is_down()
        copy = child.attachments.first()
        models.Attachment.objects.filter(pk=copy.pk).update(state=CoreStates.CREATING)

        with freeze_time(timezone.now() + timedelta(hours=3)):
            tasks.retry_attachment_forwarding()

        copy.refresh_from_db()
        self.assertEqual(copy.state, CoreStates.OK)

    def test_a_copy_being_pushed_now_is_left_alone(self):
        child = self.route_while_the_desk_is_down()
        copy = child.attachments.first()
        with freeze_time(timezone.now() + timedelta(hours=1)):
            models.Attachment.objects.filter(pk=copy.pk).update(
                state=CoreStates.CREATING, modified=timezone.now()
            )
            tasks.retry_attachment_forwarding()

        copy.refresh_from_db()
        self.assertEqual(copy.state, CoreStates.CREATING)

    def test_a_copy_is_given_up_after_its_attempts(self):
        child = self.route_while_the_desk_is_down()
        self.provider_backend.create_attachment.side_effect = ConnectionError("down")
        models.Attachment.objects.filter(issue=child).update(
            push_attempts=tasks.MAX_PUSH_ATTEMPTS
        )

        with freeze_time(timezone.now() + timedelta(hours=1)):
            tasks.retry_attachment_forwarding()

        self.assertEqual(
            set(child.attachments.values_list("state", flat=True)),
            {CoreStates.ERRED},
        )
        self.assertEqual(self.pushed(), [])
        self.issue.refresh_from_db()
        reasons = [
            entry["details"]["reason"]
            for entry in self.issue.processing_log
            if entry["event"] == "attachment_not_forwarded"
        ]
        self.assertIn("given_up", reasons)


class HiddenFilesElsewhereTest(RoutedAttachmentsBaseTest):
    """Counts and names of the files a user may not see do not show either."""

    def setUp(self):
        super().setUp()
        self.author = factories.SupportUserFactory(user=self.fixture.owner)
        factories.AttachmentFactory(issue=self.issue, author=self.author)
        factories.AttachmentFactory(
            issue=self.issue, author=self.author, is_public=False
        )
        self.provider_backend.create_attachment.side_effect = ConnectionError("down")
        child = self.route()
        child.attachments.update(author=self.author)
        self.support = structure_factories.UserFactory(is_support=True)

    def test_global_support_sees_only_the_public_files_of_a_support_user(self):
        self.client.force_authenticate(self.support)
        url = factories.SupportUserFactory.get_url(self.author)

        self.assertEqual(self.client.get(url).data["attachments_count"], 1)
        listed = self.client.get(url + "connections/").data["attachments"]
        self.assertEqual(len(listed), 1)

    def test_staff_sees_them_all(self):
        self.client.force_authenticate(self.fixture.staff)
        url = factories.SupportUserFactory.get_url(self.author)

        # Their two files on the ticket, and the three copies still waiting
        # for the provider's desk.
        self.assertEqual(self.client.get(url).data["attachments_count"], 5)
        listed = self.client.get(url + "connections/").data["attachments"]
        self.assertEqual(len(listed), 5)


@override_settings(task_always_eager=True)
@override_config(
    WALDUR_SUPPORT_ENABLED=True,
    SMAX_API_URL="https://smax.operator.example",
    ZAMMAD_API_URL="https://zammad.operator.example",
    ZAMMAD_TOKEN="operator",
    ZAMMAD_WEBHOOK_SHARED_SECRET="",
    SMAX_WEBHOOK_SHARED_SECRET="",
    JIRA_WEBHOOK_SHARED_SECRET="",
    ATLASSIAN_API_URL="https://jira.operator.example",
)
class OperatorDeskTest(test.APITestCase):
    """The operator's sync and webhooks ask the operator's own server. A
    ticket routed to a provider helpdesk with a connection of its own is on
    the provider's server, where its id names another ticket, so they leave
    it alone. A helpdesk without one uses the operator's server, and its
    tickets are synced with the operator's."""

    OWN = {
        "smax": {"SMAX_API_URL": "https://smax.provider.example"},
        "zammad": {"ZAMMAD_API_URL": "https://zammad.provider.example"},
        "atlassian": {"ATLASSIAN_API_URL": "https://jira.provider.example"},
    }

    def setUp(self):
        self.reporter = factories.SupportUserFactory(
            backend_name="smax", backend_id="u1"
        )
        self.sequence = 0

    def routed(self, backend_type, settings):
        self.sequence += 1
        parent = factories.IssueFactory(
            backend_name=backend_type,
            backend_id=f"P-{self.sequence}",
            key=f"P-{self.sequence}",
            reporter=self.reporter,
        )
        helpdesk = factories.ProviderHelpdeskFactory(
            backend_type=backend_type, settings=settings
        )
        child = factories.IssueFactory(
            parent_issue=parent,
            provider_helpdesk=helpdesk,
            backend_name=backend_type,
            backend_id=f"C-{self.sequence}",
            key=f"C-{self.sequence}",
            reporter=self.reporter,
        )
        return parent, child

    def smax_answering(self, foreign_ids):
        """The operator's SMAX, on which ``foreign_ids`` name someone else's
        tickets, with a comment and a file each."""

        def get_issue(backend_id):
            if backend_id not in foreign_ids:
                return mock.Mock(
                    description="d",
                    summary="s",
                    status="open",
                    comments=[],
                    attachments=[],
                )
            return mock.Mock(
                description="d",
                summary="s",
                status="open",
                comments=[
                    mock.Mock(
                        id=f"c-{backend_id}",
                        backend_user_id="u1",
                        description="another customer's note",
                        is_public=True,
                    )
                ],
                attachments=[
                    mock.Mock(
                        id=f"a-{backend_id}", backend_user_id="u1", filename="o.txt"
                    )
                ],
            )

        smax = SmaxServiceBackend()
        smax.manager = mock.Mock()
        smax.manager.get_issue.side_effect = get_issue
        smax.manager.get_user.return_value = mock.Mock(id="u1", name="Agent")
        smax.manager.attachment_download.return_value = b"another customer's file"
        return smax

    def test_smax_sync_leaves_the_providers_own_desk_alone(self):
        own_parent, own_child = self.routed("smax", self.OWN["smax"])
        _, shared_child = self.routed("smax", {})
        smax = self.smax_answering({own_child.backend_id, shared_child.backend_id})

        with mock.patch(
            "waldur_mastermind.support.backend.get_active_backend",
            return_value=BasicBackend(),
        ):
            smax.sync_issues()

        # Neither its comments nor its files reach the ticket or its parent.
        for issue in (own_child, own_parent):
            self.assertFalse(issue.comments.exists())
            self.assertFalse(issue.attachments.exists())
        # The helpdesk on the operator's server is synced as before.
        self.assertTrue(shared_child.comments.exists())
        self.assertTrue(shared_child.attachments.exists())

    def test_smax_webhook_leaves_the_providers_own_desk_alone(self):
        _, own_child = self.routed("smax", self.OWN["smax"])
        _, shared_child = self.routed("smax", {})

        with mock.patch.object(SmaxServiceBackend, "sync_single_issue") as sync:
            own = self.client.post(
                "/api/support-smax-webhook/", {"id": own_child.backend_id}
            )
            shared = self.client.post(
                "/api/support-smax-webhook/", {"id": shared_child.backend_id}
            )

        self.assertEqual(own.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(shared.status_code, status.HTTP_200_OK)
        self.assertEqual([call.args[0] for call in sync.mock_calls], [shared_child])

    def test_jira_sync_leaves_the_providers_own_desk_alone(self):
        own_parent, own_child = self.routed("atlassian", self.OWN["atlassian"])
        _, shared_child = self.routed("atlassian", {})
        jira = ServiceDeskBackend()

        # Syncs the ticket, its comments and its files.
        with mock.patch.object(ServiceDeskBackend, "sync_single_issue") as sync:
            jira.sync_issues()

        synced = {call.args[0] for call in sync.mock_calls}
        self.assertNotIn(own_child, synced)
        self.assertIn(own_parent, synced)
        self.assertIn(shared_child, synced)

    def test_jira_webhook_leaves_the_providers_own_desk_alone(self):
        _, own_child = self.routed("atlassian", self.OWN["atlassian"])
        payload = load_json_resource("jira_comment_create_query.json", __name__)
        payload["issue"]["key"] = own_child.backend_id

        with mock.patch.object(ServiceDeskBackend, "sync_single_issue") as sync:
            response = self.client.post(
                "/api/support-jira-webhook/", payload, format="json"
            )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        sync.assert_not_called()

    def test_zammad_webhook_leaves_the_providers_own_desk_alone(self):
        _, own_child = self.routed("zammad", self.OWN["zammad"])
        _, shared_child = self.routed("zammad", {})
        # The webhook only knows the ticket id, which a ticket on another
        # desk may share.
        factories.IssueFactory(backend_name="basic", backend_id="Z-1")

        # Syncs the ticket's comments and the files that came with them.
        with (
            mock.patch.object(ZammadServiceBackend, "update_waldur_issue_from_zammad"),
            mock.patch.object(
                ZammadServiceBackend, "update_waldur_comments_from_zammad"
            ) as sync,
        ):
            own = self.client.post(
                "/api/support-zammad-webhook/",
                {"ticket": {"id": own_child.backend_id}},
                format="json",
            )
            other_desk = self.client.post(
                "/api/support-zammad-webhook/", {"ticket": {"id": "Z-1"}}, format="json"
            )
            shared = self.client.post(
                "/api/support-zammad-webhook/",
                {"ticket": {"id": shared_child.backend_id}},
                format="json",
            )

        self.assertEqual(own.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(other_desk.status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(shared.status_code, status.HTTP_200_OK)
        self.assertEqual([call.args[0] for call in sync.mock_calls], [shared_child])

    def test_a_helpdesk_naming_the_operators_server_shares_its_desk(self):
        _, child = self.routed(
            "smax",
            {
                "SMAX_API_URL": "https://smax.operator.example/",
                "SMAX_LOGIN": "provider",
                "SMAX_PASSWORD": "provider",
            },
        )

        self.assertIn(child, backend.operator_desk_issues("smax"))

    def test_another_smax_tenant_is_another_desk(self):
        _, child = self.routed("smax", {"SMAX_TENANT_ID": "provider-tenant"})

        self.assertNotIn(child, backend.operator_desk_issues("smax"))


class SmaxSyncKnownIdTest(TestCase):
    def test_an_id_another_ticket_holds_is_skipped_not_fatal(self):
        reporter = factories.SupportUserFactory(backend_name="smax", backend_id="u1")
        issue = factories.IssueFactory(
            backend_name="smax", backend_id="910", reporter=reporter
        )
        factories.AttachmentFactory(
            issue=factories.IssueFactory(backend_name="smax", backend_id="911"),
            backend_name="smax",
            backend_id="88",
        )
        smax = SmaxServiceBackend()
        smax.manager = mock.Mock()
        smax.manager.get_issue.return_value = mock.Mock(
            description="d",
            summary="s",
            status="open",
            comments=[],
            attachments=[
                mock.Mock(id="88", backend_user_id="u1", filename="a.txt"),
                mock.Mock(id="89", backend_user_id="u1", filename="b.txt"),
            ],
        )
        smax.manager.get_user.return_value = mock.Mock(id="u1", name="Agent")
        smax.manager.attachment_download.return_value = b"pulled"

        smax.update_waldur_issue_from_smax(issue)

        self.assertEqual(
            list(issue.attachments.values_list("backend_id", flat=True)), ["89"]
        )


class DeletedHelpdeskTest(RoutedAttachmentsBaseTest):
    """Deleting a helpdesk leaves its tickets without a desk. Nothing of theirs
    is pushed to the operator's desk, where their ids name other tickets."""

    def orphan(self, child):
        child.backend_id = "42"  # the provider desk's ticket id
        child.save()
        self.helpdesk.delete()
        child.refresh_from_db()
        self.assertIsNone(child.provider_helpdesk_id)
        self.operator = self.mock_get_active_backend()
        self.operator.create_attachment.reset_mock()
        return child

    def pushed_to_operator(self, issue):
        return [
            call.args[0]
            for call in self.operator.create_attachment.mock_calls
            if call.args[0].issue_id == issue.id
        ]

    def test_a_waiting_copy_is_given_up_not_pushed_to_the_operators_desk(self):
        self.provider_backend.create_attachment.side_effect = ConnectionError("down")
        child = self.orphan(self.route())

        with freeze_time(timezone.now() + timedelta(hours=1)):
            tasks.retry_attachment_forwarding()

        self.assertEqual(self.pushed_to_operator(child), [])
        self.assertEqual(
            set(child.attachments.values_list("state", flat=True)), {CoreStates.ERRED}
        )
        self.issue.refresh_from_db()
        reasons = [
            entry["details"]["reason"]
            for entry in self.issue.processing_log
            if entry["event"] == "attachment_not_forwarded"
        ]
        self.assertEqual(reasons.count("helpdesk_deleted"), 2)

    def test_a_new_parent_file_does_not_reach_the_child(self):
        child = self.orphan(self.route())
        factories.ProviderHelpdeskFactory()  # so the handler schedules

        self.attach(self.fixture.staff, self.issue)

        self.assertEqual(self.pushed_to_operator(child), [])
        self.assertEqual(child.attachments.count(), 2)

    def test_files_and_comments_are_refused_on_the_child(self):
        child = self.orphan(self.route())
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.AttachmentFactory.get_list_url(),
            {"issue": factories.IssueFactory.get_url(child), "file": dummy_image()},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        response = self.client.post(
            factories.IssueFactory.get_url(child, action="comment"),
            {"description": "hello"},
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(self.pushed_to_operator(child), [])
        self.operator.create_comment.assert_not_called()

    def test_its_comments_are_not_changed_through_the_operators_desk(self):
        child = self.orphan(self.route())
        comment = factories.CommentFactory(issue=child)
        self.client.force_authenticate(self.fixture.staff)

        url = factories.CommentFactory.get_url(comment)
        data = self.client.get(url).data
        self.assertFalse(data["update_is_available"])
        self.assertFalse(data["destroy_is_available"])
        self.assertEqual(
            self.client.patch(url, {"description": "x"}).status_code,
            status.HTTP_400_BAD_REQUEST,
        )
        self.assertEqual(
            self.client.delete(url).status_code, status.HTTP_400_BAD_REQUEST
        )
        self.operator.update_comment.assert_not_called()
        self.operator.delete_comment.assert_not_called()

    def test_staff_delete_a_file_in_waldur_only(self):
        child = self.orphan(self.route())
        copy = child.attachments.first()
        stored = copy.file.name
        self.client.force_authenticate(self.fixture.staff)
        url = factories.AttachmentFactory.get_url(copy)

        data = self.client.get(url).data
        self.assertTrue(data["destroy_is_available"])
        self.assertTrue(data["delete_is_local_only"])
        self.assertEqual(
            self.client.delete(url).status_code, status.HTTP_204_NO_CONTENT
        )

        self.assertFalse(models.Attachment.objects.filter(pk=copy.pk).exists())
        self.assertFalse(media_models.File.objects.filter(name=stored).exists())
        self.operator.delete_attachment.assert_not_called()
        self.provider_backend.delete_attachment.assert_not_called()

    def test_nobody_but_staff_deletes_a_file_there(self):
        child = self.orphan(self.route())
        copy = child.attachments.first()
        # The uploader too: an owner of the organization, who may comment there.
        copy.author = factories.SupportUserFactory(user=self.fixture.owner)
        copy.save(update_fields=["author"])
        self.client.force_authenticate(self.fixture.owner)

        response = self.client.delete(factories.AttachmentFactory.get_url(copy))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Attachment.objects.filter(pk=copy.pk).exists())

    @override_config(PAT_ENABLED=True)
    def test_a_staff_token_without_the_staff_scope_deletes_nothing_there(self):
        child = self.orphan(self.route())
        copy = child.attachments.first()
        copy.author = factories.SupportUserFactory(user=self.fixture.staff)
        copy.save(update_fields=["author"])
        # So the token, held to the user's own roles, still sees the file.
        self.fixture.customer.add_user(self.fixture.staff, CustomerRole.OWNER)
        use_pat(self.client, self.fixture.staff)

        response = self.client.delete(factories.AttachmentFactory.get_url(copy))

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.assertTrue(models.Attachment.objects.filter(pk=copy.pk).exists())

    def test_delete_is_local_only_only_where_the_helpdesk_is_gone(self):
        live_child = self.route()
        self.client.force_authenticate(self.fixture.staff)

        def local_only(attachment):
            url = factories.AttachmentFactory.get_url(attachment)
            return self.client.get(url).data["delete_is_local_only"]

        self.assertFalse(local_only(self.logs))
        self.assertFalse(local_only(live_child.attachments.first()))
        orphan = self.orphan(live_child)
        self.assertTrue(local_only(orphan.attachments.first()))

    def test_files_there_still_cannot_be_added(self):
        child = self.orphan(self.route())
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.post(
            factories.AttachmentFactory.get_list_url(),
            {"issue": factories.IssueFactory.get_url(child), "file": dummy_image()},
            format="multipart",
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_get_backend_for_issue_refuses_the_child(self):
        child = self.orphan(self.route())

        with self.assertRaises(backend.SupportBackendError):
            backend.get_backend_for_issue(child)


class PushClaimTest(RoutedAttachmentsBaseTest):
    def test_a_claim_outlasts_the_task_time_limit(self):
        limit = timedelta(seconds=settings.CELERY_TASK_TIME_LIMIT)
        self.assertGreater(tasks.push_claim_timeout(), limit)
        with override_settings(CELERY_TASK_TIME_LIMIT=7200):
            self.assertGreater(tasks.push_claim_timeout(), timedelta(seconds=7200))

    def test_a_push_that_lost_its_claim_records_nothing(self):
        taken_over = timezone.now() + timedelta(seconds=5)

        def create_attachment(attachment):
            # Another worker takes the copy over while the desk is answering.
            models.Attachment.objects.filter(pk=attachment.pk).update(
                modified=taken_over
            )
            attachment.backend_id = "remote-1"
            attachment.save()  # a full save, as the Zammad and SMAX backends do

        self.provider_backend.create_attachment.side_effect = create_attachment
        self.config.delete()

        child = self.route()

        copy = child.attachments.get()
        self.assertEqual(copy.state, CoreStates.CREATING)
        self.assertEqual(copy.modified, taken_over)


class RetryTaskBoundsTest(RoutedAttachmentsBaseTest):
    def route_while_the_desk_is_down(self):
        self.provider_backend.create_attachment.side_effect = ConnectionError("down")
        child = self.route()
        self.provider_backend.create_attachment.side_effect = None
        self.provider_backend.create_attachment.reset_mock()
        return child

    def test_a_run_pushes_a_bounded_number_of_copies(self):
        child = self.route_while_the_desk_is_down()

        with (
            mock.patch.object(tasks, "PUSH_RETRY_BATCH", 1),
            freeze_time(timezone.now() + timedelta(hours=1)),
        ):
            tasks.retry_attachment_forwarding()

        self.assertEqual(len(self.pushed()), 1)
        self.assertEqual(child.attachments.filter(state=CoreStates.OK).count(), 1)

    def test_the_soft_time_limit_ends_the_run(self):
        self.route_while_the_desk_is_down()
        self.provider_backend.create_attachment.side_effect = SoftTimeLimitExceeded()

        with freeze_time(timezone.now() + timedelta(hours=1)):
            with self.assertRaises(SoftTimeLimitExceeded):
                tasks.retry_attachment_forwarding()

    def test_an_erred_copy_is_noted_once(self):
        child = self.route()
        copy = child.attachments.first()
        models.Attachment.objects.filter(pk=copy.pk).update(state=CoreStates.ERRED)

        tasks.forward_parent_attachments.delay(child.id)
        tasks.forward_parent_attachments.delay(child.id)

        self.assertNotIn("attachment_not_forwarded", events(self.issue))

    def test_a_copy_deleted_during_the_run_does_not_end_it(self):
        child = self.route_while_the_desk_is_down()
        first, second = list(child.attachments.order_by("modified"))
        push_copy = tasks.push_copy

        def push(forwarded, issue_backend):
            if forwarded.pk == first.pk:
                # Gone with its ticket while the run was under way.
                models.Attachment.objects.filter(pk=first.pk).delete()
            return push_copy(forwarded, issue_backend)

        with (
            mock.patch.object(tasks, "push_copy", side_effect=push),
            freeze_time(timezone.now() + timedelta(hours=1)),
        ):
            tasks.retry_attachment_forwarding()

        second.refresh_from_db()
        self.assertEqual(second.state, CoreStates.OK)

    def test_a_copy_on_a_ticket_whose_parent_was_deleted_is_still_pushed(self):
        child = self.route_while_the_desk_is_down()
        self.issue.delete()
        child.refresh_from_db()
        self.assertIsNone(child.parent_issue_id)

        with freeze_time(timezone.now() + timedelta(hours=1)):
            tasks.retry_attachment_forwarding()

        self.assertEqual(
            set(child.attachments.values_list("state", flat=True)), {CoreStates.OK}
        )


class RetryTaskCostTest(TestCase):
    def test_without_any_routed_ticket_the_run_reads_no_attachments(self):
        factories.AttachmentFactory(file__data=b"x")

        with CaptureQueriesContext(connection) as queries:
            tasks.retry_attachment_forwarding()

        self.assertFalse(
            [q for q in queries.captured_queries if "support_attachment" in q["sql"]]
        )


@override_config(
    WALDUR_SUPPORT_ENABLED=True,
    SMAX_API_URL="https://smax.operator.example",
    SMAX_TENANT_ID="100",
    ATLASSIAN_API_URL="https://jira.operator.example",
)
class OperatorDeskSettingsTest(TestCase):
    def routed(self, backend_type, settings):
        helpdesk = factories.ProviderHelpdeskFactory(
            backend_type=backend_type, settings=settings
        )
        return factories.IssueFactory(
            parent_issue=factories.IssueFactory(backend_name=backend_type),
            provider_helpdesk=helpdesk,
            backend_name=backend_type,
        )

    def test_a_null_setting_does_not_break_the_operators_sync(self):
        jira = self.routed(
            "atlassian", {"ATLASSIAN_TOKEN": "x", "ATLASSIAN_API_URL": None}
        )
        smax = self.routed("smax", {"SMAX_API_URL": None})
        operator_ticket = factories.IssueFactory(backend_name="atlassian")

        jira_desk = list(backend.operator_desk_issues("atlassian"))
        smax_desk = list(backend.operator_desk_issues("smax"))

        self.assertIn(operator_ticket, jira_desk)
        self.assertNotIn(jira, jira_desk)
        self.assertNotIn(smax, smax_desk)

    def test_the_same_server_written_differently_is_the_same_desk(self):
        upper = self.routed(
            "atlassian", {"ATLASSIAN_API_URL": "https://JIRA.operator.example/"}
        )
        port = self.routed(
            "atlassian", {"ATLASSIAN_API_URL": "https://jira.operator.example:443"}
        )
        tenant = self.routed("smax", {"SMAX_TENANT_ID": 100})

        self.assertIn(upper, backend.operator_desk_issues("atlassian"))
        self.assertIn(port, backend.operator_desk_issues("atlassian"))
        self.assertIn(tenant, backend.operator_desk_issues("smax"))

    def test_ipv6_servers_keep_their_brackets(self):
        def desk(url):
            return backend._desk_value("ATLASSIAN_API_URL", url)

        # A port, and the last group of another address, must not read alike.
        self.assertNotEqual(
            desk("https://[2001:db8::1]:8443"), desk("https://[2001:db8::1:8443]")
        )
        self.assertEqual(
            desk("https://[2001:DB8::1]:443/"), desk("https://[2001:db8::1]")
        )

    @override_config(ATLASSIAN_API_URL="https://[2001:db8::1]:8443")
    def test_an_ipv6_server_differing_in_the_last_group_is_another_desk(self):
        other = self.routed(
            "atlassian",
            {"ATLASSIAN_API_URL": "https://[2001:db8::1:8443]", "ATLASSIAN_TOKEN": "t"},
        )
        same = self.routed(
            "atlassian",
            {
                "ATLASSIAN_API_URL": "https://[2001:DB8::1]:8443/",
                "ATLASSIAN_TOKEN": "t",
            },
        )

        desk = set(backend.operator_desk_issues("atlassian"))
        self.assertNotIn(other, desk)
        self.assertIn(same, desk)

    def test_another_port_is_another_desk(self):
        other = self.routed(
            "atlassian", {"ATLASSIAN_API_URL": "https://jira.operator.example:8443"}
        )

        self.assertNotIn(other, backend.operator_desk_issues("atlassian"))

    def test_the_cost_does_not_grow_with_the_helpdesks(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                backend.routed_elsewhere_q("atlassian")
            return len(captured.captured_queries)

        self.routed("atlassian", {})
        one = queries()
        for _ in range(4):
            self.routed("atlassian", {})
            self.routed("smax", {})

        self.assertEqual(queries(), one)
