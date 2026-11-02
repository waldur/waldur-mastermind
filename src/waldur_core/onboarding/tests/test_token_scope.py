"""Other users' onboarding verifications, their raw upstream response and
their expiry are reserved to staff, and their documents to staff or support. A staff user's personal access token
without `STAFF.ACCESS` acts as the user without staff (#980).
"""

import datetime

from django.core.files.uploadedfile import SimpleUploadedFile
from django.urls import reverse
from rest_framework import status, test

from waldur_core.configuration.testing import override_config
from waldur_core.media import models as media_models
from waldur_core.onboarding import models
from waldur_core.onboarding.tests import factories
from waldur_core.permissions.tests.pat_utils import STAFF, SUPPORT, use_pat
from waldur_core.structure.tests import factories as structure_factories

RAW = {"signatories": [{"national_id": "38001010000"}]}


@override_config(PAT_ENABLED=True)
class OnboardingVerificationTokenScopeTest(test.APITestCase):
    def setUp(self):
        self.staff = structure_factories.UserFactory(is_staff=True)
        self.verification = factories.OnboardingVerificationFactory(raw_response=RAW)
        self.url = factories.OnboardingVerificationFactory.get_url(self.verification)
        self.own = factories.OnboardingVerificationFactory(
            user=self.staff, raw_response=RAW
        )
        self.own_url = factories.OnboardingVerificationFactory.get_url(self.own)

    def postpone(self, url):
        expires_at = datetime.datetime(2099, 1, 1, tzinfo=datetime.UTC)
        return self.client.patch(url, {"expires_at": expires_at.isoformat()})

    def test_staff_session_reads_and_changes_another_users_verification(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["raw_response"], RAW)
        self.assertEqual(self.postpone(self.url).status_code, status.HTTP_200_OK)

    def test_staff_token_with_staff_scope_reads_and_changes_it(self):
        use_pat(self.client, self.staff, STAFF)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["raw_response"], RAW)
        self.assertEqual(self.postpone(self.url).status_code, status.HTTP_200_OK)

    def test_staff_token_without_staff_scope_cannot_reach_it(self):
        use_pat(self.client, self.staff)
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(self.postpone(self.url).status_code, status.HTTP_404_NOT_FOUND)
        self.assertEqual(
            self.client.post(self.url + "create_customer/").status_code,
            status.HTTP_404_NOT_FOUND,
        )

    def test_staff_token_without_staff_scope_acts_as_user_on_own_verification(self):
        use_pat(self.client, self.staff)
        response = self.client.get(self.own_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("raw_response", response.data)
        response = self.postpone(self.own_url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("expires_at", response.data)

    def test_other_user_cannot_reach_it(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(
            self.client.get(self.url).status_code, status.HTTP_404_NOT_FOUND
        )
        self.assertEqual(self.postpone(self.url).status_code, status.HTTP_404_NOT_FOUND)


@override_config(PAT_ENABLED=True)
class JustificationDocumentationTokenScopeTest(test.APITestCase):
    """Another user's justification documents are downloaded through staff or
    support, as `StaffOrUserFilter` decides for the justification itself."""

    def setUp(self):
        justification = factories.OnboardingJustificationFactory()
        documentation = models.OnboardingJustificationDocumentation.objects.create(
            justification=justification,
            file=SimpleUploadedFile(
                "justification.pdf", b"%PDF-1.4 x", content_type="application/pdf"
            ),
        )
        media_file = media_models.File.objects.get(name=documentation.file.name)
        self.url = reverse("media", kwargs={"uuid": media_file.uuid})
        self.owner = justification.user

    def download(self):
        return self.client.get(self.url).status_code

    def test_support_session_can_download(self):
        self.client.force_authenticate(structure_factories.UserFactory(is_support=True))
        self.assertEqual(self.download(), status.HTTP_200_OK)

    def test_support_token_with_support_scope_can_download(self):
        use_pat(self.client, structure_factories.UserFactory(is_support=True), SUPPORT)
        self.assertEqual(self.download(), status.HTTP_200_OK)

    def test_staff_token_with_staff_scope_can_download(self):
        use_pat(self.client, structure_factories.UserFactory(is_staff=True), STAFF)
        self.assertEqual(self.download(), status.HTTP_200_OK)

    def test_staff_token_without_staff_scope_cannot_download(self):
        use_pat(self.client, structure_factories.UserFactory(is_staff=True))
        self.assertEqual(self.download(), status.HTTP_404_NOT_FOUND)

    def test_support_token_without_support_scope_cannot_download(self):
        use_pat(self.client, structure_factories.UserFactory(is_support=True))
        self.assertEqual(self.download(), status.HTTP_404_NOT_FOUND)

    def test_owner_token_can_download(self):
        use_pat(self.client, self.owner)
        self.assertEqual(self.download(), status.HTTP_200_OK)

    def test_other_user_cannot_download(self):
        self.client.force_authenticate(structure_factories.UserFactory())
        self.assertEqual(self.download(), status.HTTP_404_NOT_FOUND)
