from unittest import mock

from ddt import data, ddt
from django.contrib.contenttypes.models import ContentType
from django.core import mail
from freezegun import freeze_time
from rest_framework import status, test
from rest_framework.test import APIRequestFactory

from waldur_core.permissions import utils as permissions_utils
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.permissions.models import Role
from waldur_core.structure.models import Customer
from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models, serializers, tasks, utils
from waldur_mastermind.invoices.tests import factories


@ddt
class ProfileRetrieveTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentProfileFactory.get_url(profile=self.profile)
        self.customer_url = structure_factories.CustomerFactory.get_url(
            customer=self.fixture.customer
        )

    @data("owner", "staff", "global_support")
    def test_user_with_access_can_retrieve_customer_profile(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("manager", "admin", "user")
    def test_user_cannot_retrieve_customer_profile(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("staff", "global_support")
    def test_user_with_access_can_retrieve_unactive_customer_profile(self, user):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "manager", "admin", "user")
    def test_user_cannot_retrieve_unactive_customer_profile(self, user):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data("owner", "staff", "global_support")
    def test_user_with_access_can_retrieve_customer_profile_in_organization_endpoint(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["payment_profiles"]), 1)

    @data("manager", "admin")
    def test_user_cannot_retrieve_customer_profile_in_organization_endpoint(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment_profiles"], None)

    @data("staff", "global_support")
    def test_user_with_access_can_retrieve_unactive_customer_profile_in_organization_endpoint(
        self, user
    ):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data["payment_profiles"]), 1)

    @data(
        "owner",
    )
    def test_user_cannot_retrieve_unactive_customer_profile_in_organization_endpoint(
        self, user
    ):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["payment_profiles"], [])


class ProfileVisibilityForInvoiceViewersTest(test.APITestCase):
    """The organization endpoint hands the active payment profiles to anyone
    who may list the organization's invoices, so the invoice pages hide or show
    prices for them exactly as they do for the owner."""

    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer,
            payment_type=models.PaymentType.FIXED_PRICE,
            is_active=True,
        )
        self.user = structure_factories.UserFactory()
        self.role = Role.objects.create(
            name="CUSTOMER.REPRESENTATIVE",
            content_type=ContentType.objects.get_for_model(Customer),
        )

    def get_payment_profiles(self, customer=None):
        self.client.force_authenticate(self.user)
        response = self.client.get(
            structure_factories.CustomerFactory.get_url(
                customer=customer or self.fixture.customer
            )
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["payment_profiles"]

    def test_role_with_invoice_permission_gets_active_profiles(self):
        factories.PaymentProfileFactory(
            organization=self.fixture.customer, is_active=False
        )
        self.role.add_permission(PermissionEnum.LIST_INVOICES)
        self.fixture.customer.add_user(self.user, self.role)

        profiles = self.get_payment_profiles()

        self.assertEqual([p["uuid"] for p in profiles], [self.profile.uuid.hex])

    def test_role_with_invoice_permission_gets_no_inactive_profiles(self):
        self.profile.is_active = False
        self.profile.save()
        self.role.add_permission(PermissionEnum.LIST_INVOICES)
        self.fixture.customer.add_user(self.user, self.role)

        self.assertEqual(self.get_payment_profiles(), [])

    def test_role_without_invoice_permission_gets_none(self):
        self.fixture.customer.add_user(self.user, self.role)

        self.assertIsNone(self.get_payment_profiles())

    def test_invoice_permission_on_another_organization_does_not_leak(self):
        other_customer = structure_factories.CustomerFactory()
        self.role.add_permission(PermissionEnum.LIST_INVOICES)
        other_customer.add_user(self.user, self.role)
        # A permission-less role, so the organization itself is visible.
        bare_role = Role.objects.create(
            name="CUSTOMER.BARE",
            content_type=ContentType.objects.get_for_model(Customer),
        )
        self.fixture.customer.add_user(self.user, bare_role)

        self.assertIsNone(self.get_payment_profiles())

    def test_customer_list_checks_the_whole_page_at_once(self):
        self.role.add_permission(PermissionEnum.LIST_INVOICES)
        customers = [self.fixture.customer] + [
            structure_factories.CustomerFactory() for _ in range(2)
        ]
        for customer in customers:
            customer.add_user(self.user, self.role)

        self.client.force_authenticate(self.user)
        with mock.patch(
            "waldur_mastermind.invoices.serializers.build_role_index",
            wraps=permissions_utils.build_role_index,
        ) as build_index:
            response = self.client.get(
                structure_factories.CustomerFactory.get_list_url()
            )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        build_index.assert_called_once()
        _, pairs = build_index.call_args.args
        self.assertEqual({scope.id for _, scope in pairs}, {c.id for c in customers})
        profiles = {
            row["uuid"]: [p["uuid"] for p in row["payment_profiles"]]
            for row in response.data
        }
        self.assertEqual(
            profiles[self.fixture.customer.uuid.hex], [self.profile.uuid.hex]
        )

    def test_page_index_answers_every_row_without_a_query(self):
        self.role.add_permission(PermissionEnum.LIST_INVOICES)
        customers = [structure_factories.CustomerFactory() for _ in range(3)]
        customers[0].add_user(self.user, self.role)
        customers[1].add_user(self.user, CustomerRole.OWNER)
        request = APIRequestFactory().get("/")
        request.user = self.user

        access = serializers._PaymentProfileAccess(request, customers)
        with self.assertNumQueries(0):
            allowed = [access.allows(customer) for customer in customers]

        self.assertEqual(allowed, [True, True, False])

    def test_owner_without_invoice_permission_still_gets_profiles(self):
        # Covers a custom-roles.yaml that redefines Owner without INVOICE.LIST.
        CustomerRole.OWNER.permissions.filter(
            permission=PermissionEnum.LIST_INVOICES
        ).delete()
        self.fixture.customer.add_user(self.user, CustomerRole.OWNER)

        profiles = self.get_payment_profiles()

        self.assertEqual([p["uuid"] for p in profiles], [self.profile.uuid.hex])


@ddt
class ProfileCreateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.url = factories.PaymentProfileFactory.get_list_url()

    def get_data(self):
        return {
            "organization": structure_factories.CustomerFactory.get_url(
                customer=self.fixture.customer
            ),
            "payment_type": models.PaymentType.MONTHLY_INVOICES,
            "name": "default",
        }

    @data(
        "staff",
    )
    def test_user_with_access_can_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.get_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data("owner", "manager", "admin", "user", "global_support")
    def test_user_cannot_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.get_data())
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProfileUpdateTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentProfileFactory.get_url(profile=self.profile)

    @data(
        "staff",
    )
    def test_user_with_access_can_update_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data("owner", "manager", "admin", "user", "global_support")
    def test_user_cannot_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_enable_action(self):
        self.client.force_authenticate(self.fixture.staff)
        new_profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer,
            is_active=False,
        )
        url = factories.PaymentProfileFactory.get_url(
            profile=new_profile, action="enable"
        )
        response = self.client.post(url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        new_profile.refresh_from_db()
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.is_active)
        self.assertTrue(new_profile.is_active)


@ddt
class ProfileDeleteTest(test.APITestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentProfileFactory.get_url(profile=self.profile)

    @data(
        "staff",
    )
    def test_user_with_access_can_delete_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data("owner", "manager", "admin", "user", "global_support")
    def test_user_cannot_delete_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProfileModelTest(test.APITestCase):
    def setUp(self):
        self.customer = structure_factories.CustomerFactory()
        self.profile = factories.PaymentProfileFactory(
            organization=self.customer, is_active=True
        )

    def test_if_enabling_payment_profile_disables_other_existing_profiles(self):
        # creation
        new_profile = factories.PaymentProfileFactory(
            organization=self.customer, is_active=True
        )
        self.profile.refresh_from_db()
        self.assertIsNone(self.profile.is_active)

        # update
        self.profile.is_active = True
        self.profile.save()
        new_profile.refresh_from_db()
        self.assertIsNone(new_profile.is_active)
        self.assertTrue(self.profile.is_active)


class ProfileProcessingTest(test.APITestCase):
    def setUp(self):
        self.profile = factories.PaymentProfileFactory(
            payment_type=models.PaymentType.FIXED_PRICE
        )

    def create_invoice(self):
        tasks.create_monthly_invoices()
        invoice = models.Invoice.objects.get(
            year="2020", month="01", customer=self.profile.organization
        )
        self.assertEqual(invoice.state, models.Invoice.States.PENDING)
        return invoice

    def test_if_customer_has_a_fixed_price_payment_profile_then_invoice_are_created_as_paid(
        self,
    ):
        with freeze_time("2020-01-01"):
            invoice = self.create_invoice()

        with freeze_time("2020-02-01"):
            tasks.create_monthly_invoices()
            invoice.refresh_from_db()
            self.assertEqual(invoice.state, models.Invoice.States.PAID)

    @mock.patch("waldur_mastermind.invoices.tasks.send_invoice_notification")
    def test_that_invoice_notifications_are_not_sent_if_customer_has_a_fixed_price_payment_profile(
        self, mock_send_invoice_notification
    ):
        with freeze_time("2020-01-01"):
            self.create_invoice()

            # if fixed-price payment profile exists, so invoice notifications are not sent
            tasks.send_new_invoices_notification()
            mock_send_invoice_notification.delay.assert_not_called()

            # if fixed-price payment profile does not exists, so invoice notifications are sent
            self.profile.delete()
            tasks.send_new_invoices_notification()
            mock_send_invoice_notification.delay.assert_called_once()


class ProfileNotificationTest(test.APITestCase):
    def setUp(self):
        self.profile = factories.PaymentProfileFactory(
            payment_type=models.PaymentType.FIXED_PRICE,
            attributes={"end_date": "2020-01-31"},
        )

        factories.PaymentProfileFactory(
            payment_type=models.PaymentType.FIXED_PRICE,
            attributes={"end_date": "2020-02-15"},
        )

        factories.PaymentProfileFactory(
            payment_type=models.PaymentType.FIXED_PRICE,
        )

    def test_notification_only_if_end_exists_and_contact_will_end_in_30_days(self):
        with freeze_time("2020-01-01"):
            result = utils.get_upcoming_ends_of_fixed_payment_profiles()
            self.assertEqual(len(result), 1)

    def test_upcoming_end_goes_to_invoice_emails(self):
        structure_factories.NotificationFactory(
            key="invoices.upcoming_ends_notification"
        )
        organization = self.profile.organization
        organization.invoice_emails = "billing@example.com"
        organization.save()

        with freeze_time("2020-01-01"):
            tasks.send_notifications_about_upcoming_ends()

        self.assertEqual(
            [address for m in mail.outbox for address in m.to],
            ["billing@example.com"],
        )
