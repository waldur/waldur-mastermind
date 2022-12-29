import datetime

from ddt import data, ddt
from rest_framework import status, test

from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.promotions import models
from waldur_mastermind.promotions.tests import factories, fixtures


@ddt
class CreateCampaignTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.CampaignFactory.get_list_url()

    def _get_payload(self, **kwargs):
        payload = {
            "start_date": datetime.date.today(),
            "end_date": datetime.date.today() + datetime.timedelta(days=30),
            "discount_type": models.DiscountType.DISCOUNT,
            "discount": "10",
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
            "offerings": [
                self.offering.uuid.hex,
            ],
        }
        payload.update(kwargs)
        return payload

    @data('staff', 'offering_owner', 'service_manager')
    def test_user_can_create_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, data=self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data(
        'offering_support',
        'offering_admin',
        'offering_manager',
        'admin',
        'manager',
        'owner',
        'member',
        'user',
    )
    def test_user_can_not_create_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, data=self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_start_date(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload(
            start_date=datetime.date.today() - datetime.timedelta(days=30),
            end_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_end_date(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload(
            start_date=datetime.date.today() + datetime.timedelta(days=30),
            end_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_stock(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload(
            stock=10,
        )
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@ddt
class GetCampaignTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.url = factories.CampaignFactory.get_list_url()

    @data('staff', 'offering_owner', 'service_manager', 'offering_support')
    def test_user_can_get_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data(
        'offering_admin',
        'offering_manager',
        'admin',
        'manager',
        'owner',
        'member',
        'user',
    )
    def test_user_can_not_get_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class OfferingPublicEndpointTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.campaign = self.fixture.campaign
        self.campaign.activate()
        self.campaign.save()
        self.url = marketplace_factories.OfferingFactory.get_public_url(
            offering=self.fixture.offering
        )

    @data('admin', 'owner', 'user')
    def test_offering_promotion_campaigns(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['promotion_campaigns']), 1)
        self.assertFalse('coupon' in response.data['promotion_campaigns'][0].keys())

    @data('admin', 'owner', 'user')
    def test_unstarted_campaigns_are_not_displayed(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        self.campaign.start_date = datetime.date.today() + datetime.timedelta(days=7)
        self.campaign.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['promotion_campaigns']), 0)

    @data('admin', 'owner', 'user')
    def test_old_campaigns_are_not_displayed(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        self.campaign.start_date = datetime.date.today() - datetime.timedelta(days=30)
        self.campaign.end_date = datetime.date.today() - datetime.timedelta(days=10)
        self.campaign.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['promotion_campaigns']), 0)

    @data('admin', 'owner', 'user')
    def test_unactive_campaigns_are_not_displayed(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        self.campaign.state = models.Campaign.States.DRAFT
        self.campaign.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['promotion_campaigns']), 0)


@ddt
class UpdateCampaignTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.campaign = self.fixture.campaign
        self.url = factories.CampaignFactory.get_url(self.fixture.campaign)

    def _get_payload(self, **kwargs):
        payload = {
            "start_date": self.campaign.start_date,
            "end_date": self.campaign.end_date,
            "discount_type": self.campaign.discount_type,
            "discount": self.campaign.discount,
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
            "offerings": [
                self.fixture.offering.uuid.hex,
            ],
        }
        payload.update(kwargs)
        return payload

    @data('staff', 'offering_owner', 'service_manager')
    def test_user_can_update_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.put(self.url, self._get_payload(months=5))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.campaign.refresh_from_db()
        self.assertEqual(self.fixture.campaign.months, 5)

    def test_user_can_not_update_protected_fields_of_started_campaign(self):
        self.fixture.campaign.activate()
        self.fixture.campaign.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.put(self.url, self._get_payload(months=5))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.campaign.refresh_from_db()
        self.assertEqual(self.fixture.campaign.months, 1)

    def test_user_can_not_update_protected_fields_of_terminated_campaign(self):
        self.fixture.campaign.terminate()
        self.fixture.campaign.save()
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.put(self.url, self._get_payload(months=5))
        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)

    @data(
        'offering_support',
    )
    def test_user_can_not_update_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.put(self.url, self._get_payload(months=5))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class DeleteCampaignTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.url = factories.CampaignFactory.get_url(self.fixture.campaign)

    @data('staff', 'offering_owner', 'service_manager')
    def test_user_can_delete_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('staff', 'offering_owner', 'service_manager')
    def test_user_can_not_delete_not_draft_campaign(self, user):
        self.fixture.campaign.activate()
        self.fixture.campaign.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data('offering_support')
    def test_user_can_not_delete_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
