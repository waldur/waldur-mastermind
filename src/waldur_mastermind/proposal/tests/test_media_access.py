"""Media access rules for proposal documents.

See :mod:`waldur_core.media.access`.

- Supporting documentation follows ``ProposalViewSet.get_queryset``.
- Requested-resource attachments are purchase orders, legitimately visible to
  both sides, so the rule is the union of the consumer view (through the
  proposal) and the provider view (through the requested offering).
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.proposal import models
from waldur_mastermind.proposal.tests import fixtures

PDF = b"%PDF-1.4 proposal"


class BaseProposalMediaTest(test.APITestCase):
    def get_as(self, user, url):
        if user is None:
            self.client.logout()
        else:
            self.client.force_authenticate(user)
        return self.client.get(url).status_code

    def url_of(self, name):
        return reverse(
            "media", kwargs={"uuid": media_models.File.objects.get(name=name).uuid}
        )


class ProposalDocumentationMediaAccessTest(BaseProposalMediaTest):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.proposal = self.fixture.proposal
        documentation = models.ProposalDocumentation.objects.create(
            proposal=self.proposal,
            file=SimpleUploadedFile("doc.pdf", PDF, content_type="application/pdf"),
        )
        self.url = self.url_of(documentation.file.name)

    def test_anonymous_user_cannot_download(self):
        self.assertEqual(self.get_as(None, self.url), status.HTTP_404_NOT_FOUND)

    def test_unrelated_user_cannot_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(), self.url),
            status.HTTP_404_NOT_FOUND,
        )

    def test_proposal_creator_can_download(self):
        self.assertEqual(
            self.get_as(self.proposal.created_by, self.url), status.HTTP_200_OK
        )

    def test_call_manager_can_download(self):
        self.assertEqual(
            self.get_as(self.fixture.call_manager, self.url), status.HTTP_200_OK
        )

    def test_staff_can_download(self):
        self.assertEqual(self.get_as(self.fixture.staff, self.url), status.HTTP_200_OK)


class RequestedResourceAttachmentMediaAccessTest(BaseProposalMediaTest):
    def setUp(self):
        self.fixture = fixtures.ProposalFixture()
        self.requested_resource = self.fixture.requested_resource
        self.requested_resource.attachment = SimpleUploadedFile(
            "po.pdf", PDF, content_type="application/pdf"
        )
        self.requested_resource.save(update_fields=["attachment"])
        self.url = self.url_of(self.requested_resource.attachment.name)

    def test_anonymous_user_cannot_download(self):
        self.assertEqual(self.get_as(None, self.url), status.HTTP_404_NOT_FOUND)

    def test_unrelated_user_cannot_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(), self.url),
            status.HTTP_404_NOT_FOUND,
        )

    def test_consumer_side_proposal_creator_can_download(self):
        self.assertEqual(
            self.get_as(self.fixture.proposal.created_by, self.url), status.HTTP_200_OK
        )

    def test_provider_side_offering_owner_can_download(self):
        """The provider needs the purchase order for the offering they serve."""
        self.assertEqual(
            self.get_as(self.fixture.offering_owner, self.url), status.HTTP_200_OK
        )

    def test_staff_can_download(self):
        self.assertEqual(self.get_as(self.fixture.staff, self.url), status.HTTP_200_OK)
