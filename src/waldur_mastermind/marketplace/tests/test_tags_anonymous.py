from constance.test import override_config
from rest_framework import status, test

from waldur_mastermind.marketplace.enums import OfferingStates

from . import factories, fixtures


class TagAnonymousTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.MarketplaceFixture()
        self.tag = factories.TagFactory()
        self.list_url = factories.TagFactory.get_list_url()

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_tag_list_should_be_visible_to_anonymous_users_if_enabled(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=False)
    def test_tag_list_should_not_be_visible_to_anonymous_users_if_disabled(self):
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_tag_offering_count_for_anonymous_user_only_includes_active_offerings(self):
        # ACTIVE offering with tag
        offering_active = factories.OfferingFactory(state=OfferingStates.ACTIVE)
        offering_active.tags.add(self.tag)

        # PAUSED offering with tag
        offering_paused = factories.OfferingFactory(state=OfferingStates.PAUSED)
        offering_paused.tags.add(self.tag)

        # ARCHIVED offering with tag
        offering_archived = factories.OfferingFactory(state=OfferingStates.ARCHIVED)
        offering_archived.tags.add(self.tag)

        # DRAFT offering with tag
        offering_draft = factories.OfferingFactory(state=OfferingStates.DRAFT)
        offering_draft.tags.add(self.tag)

        response = self.client.get(factories.TagFactory.get_url(self.tag))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        # Should only count offering_active
        self.assertEqual(response.data["offering_count"], 1)

    @override_config(ANONYMOUS_USER_CAN_VIEW_OFFERINGS=True)
    def test_tag_details_should_be_visible_to_anonymous_users_if_enabled(self):
        response = self.client.get(factories.TagFactory.get_url(self.tag))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["name"], self.tag.name)
