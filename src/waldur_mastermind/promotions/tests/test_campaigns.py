import datetime

from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole, ServiceProviderRole
from waldur_core.permissions.tests import factories as permission_factories
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.marketplace.tests import factories as marketplace_factories
from waldur_mastermind.marketplace.tests import fixtures as marketplace_fixtures
from waldur_mastermind.promotions import models
from waldur_mastermind.promotions.tests import factories, fixtures


@ddt
class CreateCampaignTest(test.APITestCase):
    def setUp(self):
        self.fixture = marketplace_fixtures.MarketplaceFixture()
        self.offering = self.fixture.offering
        self.url = factories.CampaignFactory.get_list_url()
        self.other_sp = marketplace_factories.ServiceProviderFactory()
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_CAMPAIGN)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.MANAGE_CAMPAIGN)

    def _get_payload(self, **kwargs):
        payload = {
            "name": "test",
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

    @data("staff", "offering_owner", "service_manager")
    def test_user_can_create_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, data=self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data(
        "offering_support",
        "offering_admin",
        "offering_manager",
        "admin",
        "manager",
        "owner",
        "member",
        "user",
    )
    def test_user_can_not_create_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, data=self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_service_provider_manager_can_create_campaign(self):
        manager = structure_factories.UserFactory()
        self.fixture.service_provider.add_user(manager, ServiceProviderRole.MANAGER)
        self.client.force_authenticate(manager)
        response = self.client.post(self.url, data=self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)

    def test_owner_without_manage_campaign_can_not_create_campaign(self):
        CustomerRole.OWNER.delete_permission(PermissionEnum.MANAGE_CAMPAIGN)
        self.client.force_authenticate(self.fixture.offering_owner)
        response = self.client.post(self.url, data=self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("service_provider", response.data)

    @data("service_provider", "offering_customer")
    def test_custom_role_without_manage_campaign_can_not_create_campaign(
        self, scope_name
    ):
        scope = getattr(self.fixture, scope_name)
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(scope)
        )
        role.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS)
        user = structure_factories.UserFactory()
        scope.add_user(user, role)
        self.client.force_authenticate(user)
        response = self.client.post(self.url, data=self._get_payload())
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("service_provider", response.data)

    @data("staff", "offering_owner", "service_manager")
    # can access campaign but not create for a service provider
    def test_service_provider_can_not_create_campaign_in_offering(self, user):
        self.other_offering = marketplace_factories.OfferingFactory(
            customer=self.other_sp.customer
        )
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(
            self.url, data=self._get_payload(offerings=[self.other_offering.uuid.hex])
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["offering"][0],
            "You do not have permissions to create campaign in selected offering.",
        )

    def test_offering_exists_in_campaign(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload(
            offerings=[],
        )
        response = self.client.post(self.url, data=payload)
        self.assertEqual(
            response.data["offerings"]["offering"], "An offering must be specified."
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_validate_start_date(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload(
            start_date=datetime.date.today() - datetime.timedelta(days=30),
            end_date=datetime.date.today() + datetime.timedelta(days=30),
        )
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["start_date"][0],
            "Campaign start cannot be before the current date.",
        )

    def test_validate_end_date(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload(
            start_date=datetime.date.today() + datetime.timedelta(days=30),
            end_date=datetime.date.today() + datetime.timedelta(days=10),
        )
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["end_date"][0],
            "Campaign end cannot be before the start date.",
        )

    def test_validate_stock(self):
        self.client.force_authenticate(self.fixture.staff)
        payload = self._get_payload(
            stock=10,
        )
        response = self.client.post(self.url, data=payload)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertEqual(
            response.data["stock"][0],
            "Stock cannot be defined if auto_apply is true.",
        )


@ddt
class GetCampaignTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.url = factories.CampaignFactory.get_list_url()

    @data("staff", "offering_owner", "service_manager", "offering_support")
    def test_user_can_get_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)

    @data(
        "offering_admin",
        "offering_manager",
        "admin",
        "manager",
        "owner",
        "member",
        "user",
    )
    def test_user_can_not_get_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

    def test_orders(self):
        url = factories.CampaignFactory.get_url(self.fixture.campaign, "orders")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.order.uuid.hex)

    def test_resources(self):
        url = factories.CampaignFactory.get_url(self.fixture.campaign, "resources")
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["uuid"], self.fixture.resource.uuid.hex)


@ddt
class OfferingPublicEndpointTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.campaign = self.fixture.campaign
        self.campaign.activate()
        self.campaign.save()
        self.url = marketplace_factories.OfferingFactory.get_public_url(
            offering=self.fixture.offering
        )

    @data("admin", "owner", "user")
    def test_offering_promotion_campaigns(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["promotion_campaigns"]), 1)
        self.assertFalse("coupon" in response.data["promotion_campaigns"][0].keys())

    @data("admin", "owner", "user")
    def test_unstarted_campaigns_are_not_displayed(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        self.campaign.start_date = datetime.date.today() + datetime.timedelta(days=7)
        self.campaign.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["promotion_campaigns"]), 0)

    @data("admin", "owner", "user")
    def test_old_campaigns_are_not_displayed(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        self.campaign.start_date = datetime.date.today() - datetime.timedelta(days=30)
        self.campaign.end_date = datetime.date.today() - datetime.timedelta(days=10)
        self.campaign.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["promotion_campaigns"]), 0)

    @data("admin", "owner", "user")
    def test_unactive_campaigns_are_not_displayed(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        self.campaign.state = models.Campaign.States.DRAFT
        self.campaign.save()

        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["promotion_campaigns"]), 0)


@ddt
class UpdateCampaignTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.campaign = self.fixture.campaign
        self.url = factories.CampaignFactory.get_url(self.fixture.campaign)
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_CAMPAIGN)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.MANAGE_CAMPAIGN)

    def _get_payload(self, **kwargs):
        payload = {
            "name": self.campaign.name,
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

    @freeze_time("2023-11-01")
    @data("staff", "offering_owner", "service_manager")
    def test_user_can_update_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.put(self.url, self._get_payload(months=5))
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
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
        "offering_support",
    )
    def test_user_can_not_update_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.put(self.url, self._get_payload(months=5))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class DeleteCampaignTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        self.url = factories.CampaignFactory.get_url(self.fixture.campaign)
        CustomerRole.OWNER.add_permission(PermissionEnum.MANAGE_CAMPAIGN)
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.MANAGE_CAMPAIGN)

    @data("staff", "offering_owner", "service_manager")
    def test_user_can_delete_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("staff", "offering_owner", "service_manager")
    def test_user_can_not_delete_not_draft_campaign(self, user):
        self.fixture.campaign.activate()
        self.fixture.campaign.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @data("offering_support")
    def test_user_can_not_delete_campaign(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))

        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ServiceProviderManagerCampaignLifecycleTest(test.APITestCase):
    """A manager holding their role on the ServiceProvider itself, rather than
    on its organization, manages that provider's campaigns and no others."""

    def setUp(self):
        self.fixture = fixtures.PromotionsFixture()
        ServiceProviderRole.MANAGER.add_permission(PermissionEnum.MANAGE_CAMPAIGN)
        self.campaign = factories.CampaignFactory(
            service_provider=self.fixture.service_provider
        )
        self.campaign.offerings.add(self.fixture.offering)
        self.manager = structure_factories.UserFactory()
        self.fixture.service_provider.add_user(
            self.manager, ServiceProviderRole.MANAGER
        )
        self.other_manager = structure_factories.UserFactory()
        marketplace_factories.ServiceProviderFactory().add_user(
            self.other_manager, ServiceProviderRole.MANAGER
        )

    def _update(self, user):
        self.client.force_authenticate(user)
        payload = {
            "name": "Renamed",
            "start_date": self.campaign.start_date,
            "end_date": self.campaign.end_date,
            "discount_type": self.campaign.discount_type,
            "discount": self.campaign.discount,
            "service_provider": marketplace_factories.ServiceProviderFactory.get_url(
                self.fixture.service_provider
            ),
            "offerings": [self.fixture.offering.uuid.hex],
        }
        return self.client.put(
            factories.CampaignFactory.get_url(self.campaign), payload
        )

    def _post(self, user, action):
        self.client.force_authenticate(user)
        return self.client.post(
            factories.CampaignFactory.get_url(self.campaign, action)
        )

    def test_manager_can_update_campaign(self):
        response = self._update(self.manager)
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.name, "Renamed")

    def test_manager_can_activate_and_terminate_campaign(self):
        response = self._post(self.manager, "activate")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, models.Campaign.States.ACTIVE)

        response = self._post(self.manager, "terminate")
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, models.Campaign.States.TERMINATED)

    def test_manager_can_delete_campaign(self):
        self.client.force_authenticate(self.manager)
        response = self.client.delete(factories.CampaignFactory.get_url(self.campaign))
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(models.Campaign.objects.filter(id=self.campaign.id).exists())

    def test_manager_of_another_provider_can_not_manage_campaign(self):
        self.assertEqual(
            self._update(self.other_manager).status_code, status.HTTP_404_NOT_FOUND
        )
        for action in ("activate", "terminate"):
            self.assertEqual(
                self._post(self.other_manager, action).status_code,
                status.HTTP_404_NOT_FOUND,
                action,
            )
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, models.Campaign.States.DRAFT)

    @data("activate", "terminate")
    def test_provider_role_without_manage_campaign_can_not_run_action(self, action):
        role = permission_factories.RoleFactory(
            content_type=ContentType.objects.get_for_model(
                self.fixture.service_provider
            )
        )
        role.add_permission(PermissionEnum.LIST_SERVICE_PROVIDER_CUSTOMERS)
        user = structure_factories.UserFactory()
        self.fixture.service_provider.add_user(user, role)
        response = self._post(user, action)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)
        self.campaign.refresh_from_db()
        self.assertEqual(self.campaign.state, models.Campaign.States.DRAFT)
