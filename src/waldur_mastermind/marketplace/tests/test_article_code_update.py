from rest_framework import status, test

from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.marketplace.enums import OfferingStates
from waldur_mastermind.marketplace.tests import factories


class BaseArticleCodeUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.category = factories.CategoryFactory()
        self.offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            category=self.category,
            state=OfferingStates.ACTIVE,
            name="HPC Cluster",
        )
        self.comp1 = factories.OfferingComponentFactory(
            offering=self.offering,
            type="cpu",
            name="CPU",
            article_code="ABC-100",
        )
        self.comp2 = factories.OfferingComponentFactory(
            offering=self.offering,
            type="ram",
            name="RAM",
            article_code="ABC-200",
        )
        self.comp3 = factories.OfferingComponentFactory(
            offering=self.offering,
            type="gpu",
            name="GPU",
            article_code="XYZ-300",
        )
        self.preview_url = (
            "http://testserver/api/marketplace-article-code-update/preview/"
        )
        self.apply_url = "http://testserver/api/marketplace-article-code-update/apply/"


class ArticleCodeUpdatePreviewTest(BaseArticleCodeUpdateTest):
    def test_staff_can_preview_matching_components(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.preview_url,
            {"search": "ABC", "replace": "ABD"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)
        codes = {item["old_article_code"] for item in response.data}
        self.assertEqual(codes, {"ABC-100", "ABC-200"})
        new_codes = {item["new_article_code"] for item in response.data}
        self.assertEqual(new_codes, {"ABD-100", "ABD-200"})

    def test_preview_returns_empty_when_no_matches(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.preview_url,
            {"search": "NONEXISTENT", "replace": "FOO"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_preview_filters_by_category(self):
        other_category = factories.CategoryFactory()
        other_offering = factories.OfferingFactory(
            category=other_category,
            state=OfferingStates.ACTIVE,
        )
        factories.OfferingComponentFactory(
            offering=other_offering,
            type="cpu",
            name="CPU",
            article_code="ABC-999",
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.preview_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "offering_category_uuid": self.category.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_preview_filters_by_customer(self):
        other_offering = factories.OfferingFactory(
            state=OfferingStates.ACTIVE,
        )
        factories.OfferingComponentFactory(
            offering=other_offering,
            type="cpu",
            name="CPU",
            article_code="ABC-999",
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.preview_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "offering_customer_uuid": self.fixture.customer.uuid.hex,
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_preview_filters_by_offering_state(self):
        draft_offering = factories.OfferingFactory(
            customer=self.fixture.customer,
            category=self.category,
            state=OfferingStates.DRAFT,
        )
        factories.OfferingComponentFactory(
            offering=draft_offering,
            type="cpu",
            name="CPU",
            article_code="ABC-999",
        )
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.preview_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "offering_state": "Active",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_preview_filters_by_offering_name(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.preview_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "offering_name": "HPC",
            },
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 2)

    def test_preview_skips_components_where_replacement_exceeds_max_length(self):
        self.comp1.article_code = "A" * 30
        self.comp1.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.preview_url,
            {"search": "A", "replace": "BB"},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        uuids = {item["component_uuid"] for item in response.data}
        self.assertNotIn(str(self.comp1.uuid), uuids)

    def test_non_staff_gets_403(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            self.preview_url,
            {"search": "ABC", "replace": "ABD"},
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_anonymous_gets_401(self):
        response = self.client.post(
            self.preview_url,
            {"search": "ABC", "replace": "ABD"},
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class ArticleCodeUpdateApplyTest(BaseArticleCodeUpdateTest):
    def test_staff_can_apply_changes(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.apply_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "component_uuids": [
                    self.comp1.uuid.hex,
                    self.comp2.uuid.hex,
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated_count"], 2)
        self.comp1.refresh_from_db()
        self.comp2.refresh_from_db()
        self.assertEqual(self.comp1.article_code, "ABD-100")
        self.assertEqual(self.comp2.article_code, "ABD-200")

    def test_apply_only_updates_specified_components(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.apply_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "component_uuids": [self.comp1.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["updated_count"], 1)
        self.comp1.refresh_from_db()
        self.comp2.refresh_from_db()
        self.assertEqual(self.comp1.article_code, "ABD-100")
        self.assertEqual(self.comp2.article_code, "ABC-200")  # unchanged

    def test_apply_rejects_stale_data(self):
        self.client.force_authenticate(self.fixture.staff)
        # Change article code after preview
        self.comp1.article_code = "CHANGED-100"
        self.comp1.save()
        response = self.client.post(
            self.apply_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "component_uuids": [
                    self.comp1.uuid.hex,
                    self.comp2.uuid.hex,
                ],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_apply_rejects_empty_component_list(self):
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.apply_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "component_uuids": [],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_non_staff_gets_403(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.post(
            self.apply_url,
            {
                "search": "ABC",
                "replace": "ABD",
                "component_uuids": [self.comp1.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_apply_with_replacement_clearing_article_code(self):
        """Replacing the entire article code with empty string should work."""
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.post(
            self.apply_url,
            {
                "search": "ABC-100",
                "replace": "",
                "component_uuids": [self.comp1.uuid.hex],
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.comp1.refresh_from_db()
        self.assertEqual(self.comp1.article_code, "")
