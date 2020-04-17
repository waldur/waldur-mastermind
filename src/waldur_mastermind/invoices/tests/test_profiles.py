from ddt import data, ddt
from django.db import IntegrityError
from rest_framework import status, test

from waldur_core.structure.tests import factories as structure_factories
from waldur_core.structure.tests import fixtures as structure_fixtures

from .. import models
from . import factories


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

    @data('owner', 'staff', 'global_support')
    def test_user_with_access_can_retrieve_customer_profile(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('manager', 'admin', 'user')
    def test_user_cannot_retrieve_customer_profile(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data('staff', 'global_support')
    def test_user_with_access_can_retrieve_unactive_customer_profile(self, user):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('owner', 'manager', 'admin', 'user')
    def test_user_cannot_retrieve_unactive_customer_profile(self, user):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    @data('owner', 'staff', 'global_support')
    def test_user_with_access_can_retrieve_customer_profile_in_organization_endpoint(
        self, user
    ):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['payment_profiles']), 1)

    @data('manager', 'admin')
    def test_user_cannot_retrieve_customer_profile_in_organization_endpoint(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['payment_profiles'], None)

    @data('staff', 'global_support')
    def test_user_with_access_can_retrieve_unactive_customer_profile_in_organization_endpoint(
        self, user
    ):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data['payment_profiles']), 1)

    @data('owner',)
    def test_user_cannot_retrieve_unactive_customer_profile_in_organization_endpoint(
        self, user
    ):
        self.profile.is_active = False
        self.profile.save()
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.customer_url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data['payment_profiles'], [])


@ddt
class ProfileCreateTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.url = factories.PaymentProfileFactory.get_list_url()

    def get_data(self):
        return {
            'organization': structure_factories.CustomerFactory.get_url(
                customer=self.fixture.customer
            ),
            'payment_type': models.PaymentType.MONTHLY_INVOICES,
            'name': 'default',
        }

    @data('staff',)
    def test_user_with_access_can_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.post(self.url, self.get_data())
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @data('owner', 'manager', 'admin', 'user', 'global_support')
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

    @data('staff',)
    def test_user_with_access_can_update_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @data('owner', 'manager', 'admin', 'user', 'global_support')
    def test_user_cannot_create_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.patch(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@ddt
class ProfileDeleteTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = structure_fixtures.ProjectFixture()
        self.profile = factories.PaymentProfileFactory(
            organization=self.fixture.customer
        )
        self.url = factories.PaymentProfileFactory.get_url(profile=self.profile)

    @data('staff',)
    def test_user_with_access_can_delete_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    @data('owner', 'manager', 'admin', 'user', 'global_support')
    def test_user_cannot_delete_customer_profiles(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.delete(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


class ProfileModelTest(test.APITransactionTestCase):
    def test_there_should_be_multiple_non_active_but_only_a_single_active(self):
        customer = structure_factories.CustomerFactory()
        factories.PaymentProfileFactory(organization=customer, is_active=True)
        self.assertRaises(
            IntegrityError,
            factories.PaymentProfileFactory,
            organization=customer,
            is_active=True,
        )

        factories.PaymentProfileFactory(organization=customer, is_active=False)
        profile = factories.PaymentProfileFactory(
            organization=customer, is_active=False
        )
        profile.is_active = True
        self.assertRaises(IntegrityError, profile.save)
