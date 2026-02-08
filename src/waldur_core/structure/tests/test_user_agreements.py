from rest_framework import status, test

from waldur_core.structure.models import UserAgreement
from waldur_core.structure.tests import factories, fixtures


class UserAgreementListTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.url = factories.UserAgreementFactory.get_list_url()

    def test_user_can_list_agreements(self):
        factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.TOS, language=""
        )
        factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.PP, language=""
        )
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_user_can_filter_by_agreement_type(self):
        factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.TOS, language=""
        )
        factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.PP, language=""
        )
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url, {"agreement_type": "TOS"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["agreement_type"], "TOS")


class UserAgreementLanguageTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.url = factories.UserAgreementFactory.get_list_url()
        # Create default (no language) TOS
        self.tos_default = factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.TOS,
            language="",
            content="<p>Default TOS</p>",
        )
        # Create German TOS
        self.tos_german = factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.TOS,
            language="de",
            content="<p>German TOS</p>",
        )
        # Create default PP (no localized version)
        self.pp_default = factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.PP,
            language="",
            content="<p>Default PP</p>",
        )

    def test_exact_language_match_returns_localized_version(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(
            self.url, {"agreement_type": "TOS", "language": "de"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["language"], "de")
        self.assertIn("German TOS", response.data[0]["content"])

    def test_missing_language_falls_back_to_default(self):
        self.client.force_authenticate(self.fixture.user)
        # Request French, which doesn't exist
        response = self.client.get(
            self.url, {"agreement_type": "TOS", "language": "fr"}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["language"], "")
        self.assertIn("Default TOS", response.data[0]["content"])

    def test_language_filter_returns_all_types_with_fallback(self):
        self.client.force_authenticate(self.fixture.user)
        # Request German - TOS has German version, PP doesn't
        response = self.client.get(self.url, {"language": "de"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        # TOS should be German
        tos = next(a for a in response.data if a["agreement_type"] == "TOS")
        self.assertEqual(tos["language"], "de")
        # PP should fallback to default
        pp = next(a for a in response.data if a["agreement_type"] == "PP")
        self.assertEqual(pp["language"], "")

    def test_no_language_filter_returns_all_versions(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should return all 3 agreements
        self.assertEqual(len(response.data), 3)


class UserAgreementCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.url = factories.UserAgreementFactory.get_list_url()
        self.payload = {
            "content": "<p>New agreement content</p>",
            "agreement_type": "TOS",
            "language": "et",
        }

    def test_staff_can_create_localized_agreement(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["language"], "et")
        self.assertEqual(response.data["agreement_type"], "TOS")

    def test_user_cannot_create_agreement(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_duplicate_language_and_type_rejected(self):
        factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.TOS, language="et"
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(self.url, self.payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class UserAgreementUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.agreement = factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.TOS,
            language="de",
            content="<p>Original content</p>",
        )
        self.url = factories.UserAgreementFactory.get_url(self.agreement)

    def test_staff_can_update_agreement(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.patch(self.url, {"content": "<p>Updated content</p>"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIn("Updated content", response.data["content"])

    def test_user_cannot_update_agreement(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.patch(self.url, {"content": "<p>Updated content</p>"})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class UserAgreementModelTest(test.APITestCase):
    def test_str_with_language(self):
        agreement = factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.TOS, language="de"
        )
        self.assertEqual(str(agreement), "TOS (de)")

    def test_str_without_language(self):
        agreement = factories.UserAgreementFactory(
            agreement_type=UserAgreement.UserAgreements.PP, language=""
        )
        self.assertEqual(str(agreement), "PP (default)")
