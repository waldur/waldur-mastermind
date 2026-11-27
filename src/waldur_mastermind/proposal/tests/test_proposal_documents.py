import hashlib
from importlib import import_module

from django.apps import apps as django_apps
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from rest_framework import status, test

from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import factories, fixtures

migration = import_module(
    "waldur_mastermind.proposal.migrations.0092_delete_empty_proposal_documents"
)


def _pdf(name="proposal.pdf"):
    return SimpleUploadedFile(name, b"%PDF-1.4\n%%EOF\n")


def _pdf_with(content, name="other.pdf"):
    return SimpleUploadedFile(name, content)


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

    def test_proposal_lists_the_hash_of_each_document(self):
        self.client.post(self.url, {"file": _pdf()}, format="multipart")
        self.client.post(
            self.url,
            {"file": _pdf_with(b"%PDF-1.4\n% other\n%%EOF\n")},
            format="multipart",
        )

        response = self.client.get(factories.ProposalFactory.get_url(self.proposal))

        self.assertEqual(
            {
                document["file_hash"]
                for document in response.data["supporting_documentation"]
            },
            {
                hashlib.sha256(b"%PDF-1.4\n%%EOF\n").hexdigest(),
                hashlib.sha256(b"%PDF-1.4\n% other\n%%EOF\n").hexdigest(),
            },
        )


class ProposalDocumentQueryTest(test.APITestCase):
    """Reading a proposal never loads its documents' content."""

    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.client.force_authenticate(self.fixture.staff)
        url = factories.ProposalFactory.get_url(self.proposal, action="attach_document")
        for index in range(3):
            self.client.post(
                url,
                {"file": _pdf_with(b"%PDF-1.4\n% " + bytes([65 + index]) + b"\n")},
                format="multipart",
            )
        self.url = factories.ProposalFactory.get_url(self.proposal)

    def _media_queries(self, url):
        with CaptureQueriesContext(connection) as context:
            response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response, [
            query["sql"]
            for query in context.captured_queries
            if '"media_file"' in query["sql"]
        ]

    def test_reading_a_proposal_does_not_load_document_content(self):
        _, queries = self._media_queries(self.url)

        self.assertTrue(queries)
        for sql in queries:
            self.assertNotIn('"media_file"."content"', sql)

    def test_document_lookups_do_not_grow_with_the_number_of_documents(self):
        _, three = self._media_queries(self.url)
        self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="attach_document"),
            {"file": _pdf_with(b"%PDF-1.4\n% D\n")},
            format="multipart",
        )

        _, four = self._media_queries(self.url)

        self.assertEqual(len(four), len(three))

    def test_documents_keep_their_link_size_and_hash(self):
        response, _ = self._media_queries(self.url)

        documents = response.data["supporting_documentation"]
        self.assertEqual(len(documents), 3)
        for document in documents:
            stored = models.ProposalDocumentation.objects.get(uuid=document["uuid"])
            media = django_apps.get_model("media", "File").objects.get(
                name=stored.file.name
            )
            self.assertTrue(document["file"].endswith(f"/api/media/{media.uuid.hex}/"))
            self.assertEqual(document["file_size"], media.size)
            self.assertEqual(document["file_hash"], media.hash)
            self.assertEqual(document["file_name"], stored.file.name)


class ProposalDocumentStoredFileTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        self.client.force_authenticate(self.fixture.staff)
        self.client.post(
            factories.ProposalFactory.get_url(self.proposal, action="attach_document"),
            {"file": _pdf()},
            format="multipart",
        )
        self.document = self.proposal.proposaldocumentation_set.get()
        self.url = factories.ProposalFactory.get_url(self.proposal)

    def test_link_carries_no_query_parameters_of_the_request(self):
        response = self.client.get(self.url, {"format": "json"})

        [document] = response.data["supporting_documentation"]
        self.assertNotIn("?", document["file"])

    def test_document_whose_stored_file_is_gone_still_reads(self):
        django_apps.get_model("media", "File").objects.filter(
            name=self.document.file.name
        ).delete()

        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        [document] = response.data["supporting_documentation"]
        self.assertEqual(document["file_size"], 0)
        self.assertIsNone(document["file_hash"])


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
        self.assertIsNone(document["file_hash"])


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
