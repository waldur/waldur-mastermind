from importlib import import_module

from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from rest_framework import status, test

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories, fixtures

migration = import_module(
    "waldur_mastermind.proposal.migrations.0092_delete_empty_proposal_documents"
)


def _pdf(name="proposal.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4\n%%EOF\n")


class ProposalDocumentUploadTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.url = factories.ProposalFactory.get_url(
            self.proposal, action="attach_document"
        )
        self.client.force_authenticate(self.fixture.staff)

    def test_upload_without_file_is_rejected(self):
        response = self.client.post(self.url, {}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", response.data)
        self.assertFalse(self.proposal.proposaldocumentation_set.exists())

    def test_upload_with_empty_file_value_is_rejected(self):
        response = self.client.post(self.url, {"file": ""}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("file", response.data)
        self.assertFalse(self.proposal.proposaldocumentation_set.exists())

    def test_upload_with_file_is_accepted(self):
        response = self.client.post(self.url, {"file": _pdf()}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.proposaldocumentation_set.count(), 1)

    def test_the_same_file_is_refused_the_second_time(self):
        self.client.post(self.url, {"file": _pdf()}, format="multipart")

        response = self.client.post(
            self.url, {"file": _pdf("renamed.pdf")}, format="multipart"
        )

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("already attached", str(response.data["file"]))
        self.assertEqual(self.proposal.proposaldocumentation_set.count(), 1)

    def test_a_different_file_with_the_same_name_is_accepted(self):
        self.client.post(self.url, {"file": _pdf()}, format="multipart")
        other = SimpleUploadedFile("proposal.pdf", b"%PDF-1.4\n% v2\n%%EOF\n")

        response = self.client.post(self.url, {"file": other}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.proposaldocumentation_set.count(), 2)

    def test_a_removed_file_may_be_attached_again(self):
        self.client.post(self.url, {"file": _pdf()}, format="multipart")
        self.proposal.proposaldocumentation_set.all().delete()

        response = self.client.post(self.url, {"file": _pdf()}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(self.proposal.proposaldocumentation_set.count(), 1)

    def test_the_same_file_may_be_attached_to_another_proposal(self):
        self.client.post(self.url, {"file": _pdf()}, format="multipart")
        other_proposal = factories.ProposalFactory(round=self.proposal.round)
        other_url = factories.ProposalFactory.get_url(
            other_proposal, action="attach_document"
        )

        response = self.client.post(other_url, {"file": _pdf()}, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(other_proposal.proposaldocumentation_set.count(), 1)


class ProposalWithEmptyDocumentReadTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.empty = models.ProposalDocumentation.objects.create(
            proposal=self.proposal, file=""
        )
        self.url = factories.ProposalFactory.get_url(self.proposal)

    def test_proposal_with_an_empty_document_is_readable(self):
        self.client.force_authenticate(self.fixture.proposal_creator)

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [document] = response.data["supporting_documentation"]
        self.assertEqual(document["uuid"], self.empty.uuid.hex)
        self.assertIsNone(document["file_size"])


class DeleteEmptyProposalDocumentsMigrationTest(TestCase):
    def setUp(self):
        self.proposal = fixtures.ProposalFixture().proposal

    def test_deletes_rows_without_a_file_and_keeps_the_others(self):
        empty = models.ProposalDocumentation.objects.create(
            proposal=self.proposal, file=""
        )
        null = models.ProposalDocumentation.objects.create(
            proposal=self.proposal, file=None
        )
        kept = models.ProposalDocumentation.objects.create(
            proposal=self.proposal, file=_pdf()
        )

        with connection.schema_editor() as schema_editor:
            migration.delete_empty_documents(django_apps, schema_editor)

        remaining = set(
            models.ProposalDocumentation.objects.values_list("pk", flat=True)
        )
        self.assertEqual(remaining, {kept.pk})
        self.assertNotIn(empty.pk, remaining)
        self.assertNotIn(null.pk, remaining)
