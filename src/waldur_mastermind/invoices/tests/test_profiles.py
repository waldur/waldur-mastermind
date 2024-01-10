from unittest import mock

from ddt import data, ddt
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures
from waldur_mastermind.invoices import models, tasks, utils
from waldur_mastermind.invoices.tests import factories


@ddt
class ProfileRetrieveTest(test.APITransactionTestCase):
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


@ddt
class ProfileCreateTest(test.APITransactionTestCase):
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
class ProfileUpdateTest(test.APITransactionTestCase):
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
class ProfileDeleteTest(test.APITransactionTestCase):
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


class ProfileModelTest(test.APITransactionTestCase):
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


class ProfileProcessingTest(test.APITransactionTestCase):
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


class ProfileNotificationTest(test.APITransactionTestCase):
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
