"""Media access rules for onboarding justification documentation.

See :mod:`waldur_core.media.access`. These files are user-scoped rather than
customer-scoped: the rule mirrors ``StaffOrUserFilter``, which
``OnboardingJustificationViewSet`` applies.
"""

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.media import models as media_models
from waldur_core.onboarding import models
from waldur_core.onboarding.tests import factories
from waldur_core.structure.tests import factories as structure_factories

PDF = b"%PDF-1.4 justification"


class OnboardingDocumentationMediaAccessTest(test.APITestCase):
    def setUp(self):
        self.justification = factories.OnboardingJustificationFactory()
        self.documentation = models.OnboardingJustificationDocumentation.objects.create(
            justification=self.justification,
            file=SimpleUploadedFile(
                "justification.pdf", PDF, content_type="application/pdf"
            ),
        )
        media_file = media_models.File.objects.get(name=self.documentation.file.name)
        self.url = reverse("media", kwargs={"uuid": media_file.uuid})

    def get_as(self, user):
        if user is None:
            self.client.logout()
        else:
            self.client.force_authenticate(user)
        return self.client.get(self.url).status_code

    def test_anonymous_user_cannot_download(self):
        self.assertEqual(self.get_as(None), status.HTTP_404_NOT_FOUND)

    def test_unrelated_user_cannot_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory()), status.HTTP_404_NOT_FOUND
        )

    def test_owner_of_the_justification_can_download(self):
        self.assertEqual(self.get_as(self.justification.user), status.HTTP_200_OK)

    def test_staff_can_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(is_staff=True)),
            status.HTTP_200_OK,
        )

    def test_support_can_download(self):
        self.assertEqual(
            self.get_as(structure_factories.UserFactory(is_support=True)),
            status.HTTP_200_OK,
        )
