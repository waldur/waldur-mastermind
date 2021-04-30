import mock
from ddt import data, ddt
from django.urls import reverse
from django.utils import timezone
from freezegun import freeze_time
from mock_django import mock_signal_receiver
from rest_framework import status, test

from waldur_core.core.tests.helpers import override_waldur_core_settings
from waldur_core.quotas.tests import factories as quota_factories
from waldur_core.structure import signals
from waldur_core.structure.models import (
    Customer,
    CustomerPermission,
    CustomerRole,
    ProjectRole,
)
from waldur_core.structure.tests import factories, fixtures


class CustomerBaseTest(test.APITransactionTestCase):
    def _get_customer_url(self, customer):
        return 'http://testserver' + reverse(
            'customer-detail', kwargs={'uuid': customer.uuid.hex}
        )

    def _get_project_url(self, project):
        return 'http://testserver' + reverse(
            'project-detail', kwargs={'uuid': project.uuid.hex}
        )

    def _get_user_url(self, user):
        return 'http://testserver' + reverse(
            'user-detail', kwargs={'uuid': user.uuid.hex}
        )


@freeze_time('2017-11-01')
class CustomerUserTest(CustomerBaseTest):
    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.user = factories.UserFactory()
        self.created_by = factories.UserFactory()

    def test_add_user_returns_created_if_grant_didnt_exist_before(self):
        _, created = self.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertTrue(
            created, 'Customer permission should have been reported as created'
        )

    def test_add_user_returns_not_created_if_grant_existed_before(self):
        self.customer.add_user(self.user, CustomerRole.OWNER)
        _, created = self.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertFalse(
            created, 'Customer permission should have been reported as not created'
        )

    def test_add_user_returns_membership(self):
        membership, _ = self.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertEqual(membership.user, self.user)
        self.assertEqual(membership.customer, self.customer)

    def test_add_user_returns_same_membership_for_consequent_calls_with_same_arguments(
        self,
    ):
        membership1, _ = self.customer.add_user(self.user, CustomerRole.OWNER)
        membership2, _ = self.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertEqual(membership1, membership2)

    def test_add_user_emits_structure_role_granted_if_grant_didnt_exist_before(self):
        with mock_signal_receiver(signals.structure_role_granted) as receiver:
            self.customer.add_user(self.user, CustomerRole.OWNER, self.created_by)

        receiver.assert_called_once_with(
            structure=self.customer,
            user=self.user,
            role=CustomerRole.OWNER,
            sender=Customer,
            signal=signals.structure_role_granted,
            created_by=self.created_by,
        )

    def test_add_user_doesnt_emit_structure_role_granted_if_grant_existed_before(self):
        self.customer.add_user(self.user, CustomerRole.OWNER)

        with mock_signal_receiver(signals.structure_role_granted) as receiver:
            self.customer.add_user(self.user, CustomerRole.OWNER)

        self.assertFalse(
            receiver.called, 'structure_role_granted should not be emitted'
        )

    def test_remove_user_emits_structure_role_revoked_for_each_role_user_had_in_customer(
        self,
    ):
        self.customer.add_user(self.user, CustomerRole.OWNER)

        with mock_signal_receiver(signals.structure_role_revoked) as receiver:
            self.customer.remove_user(self.user, removed_by=self.created_by)

        receiver.assert_called_once_with(
            structure=self.customer,
            user=self.user,
            role=CustomerRole.OWNER,
            removed_by=self.created_by,
            sender=Customer,
            signal=signals.structure_role_revoked,
        )

    def test_remove_user_emits_structure_role_revoked_if_grant_existed_before(self):
        self.customer.add_user(self.user, CustomerRole.OWNER)

        with mock_signal_receiver(signals.structure_role_revoked) as receiver:
            self.customer.remove_user(self.user, CustomerRole.OWNER, self.created_by)

        receiver.assert_called_once_with(
            structure=self.customer,
            user=self.user,
            role=CustomerRole.OWNER,
            removed_by=self.created_by,
            sender=Customer,
            signal=signals.structure_role_revoked,
        )

    def test_remove_user_doesnt_emit_structure_role_revoked_if_grant_didnt_exist_before(
        self,
    ):
        with mock_signal_receiver(signals.structure_role_revoked) as receiver:
            self.customer.remove_user(self.user, CustomerRole.OWNER)

        self.assertFalse(receiver.called, 'structure_role_remove should not be emitted')

    def test_get_owners_returns_empty_list(self):
        self.assertEqual(0, self.customer.get_owners().count())


@ddt
class CustomerListTest(CustomerBaseTest):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    # List filtration tests
    @data(
        'staff',
        'global_support',
        'owner',
        'customer_support',
        'admin',
        'manager',
        'member',
    )
    def test_user_can_list_customers(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        self._check_user_list_access_customers(self.fixture.customer, 'assertIn')

    @data('user', 'admin', 'manager', 'member')
    def test_user_cannot_list_other_customer(self, user):
        customer = factories.CustomerFactory()
        self.client.force_authenticate(user=getattr(self.fixture, user))
        self._check_customer_in_list(customer, False)

    # Nested objects filtration tests
    @data('admin', 'manager', 'member')
    def test_user_can_see_project_he_has_a_role_in_within_customer(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.get(self._get_customer_url(self.fixture.customer))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        project_urls = set([project['url'] for project in response.data['projects']])
        self.assertIn(
            self._get_project_url(self.fixture.project),
            project_urls,
            'User should see project',
        )

    @data('admin', 'manager', 'member')
    def test_user_cannot_see_project_he_has_no_role_in_within_customer(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        non_seen_project = factories.ProjectFactory(customer=self.fixture.customer)

        response = self.client.get(self._get_customer_url(self.fixture.customer))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        project_urls = set([project['url'] for project in response.data['projects']])
        self.assertNotIn(
            self._get_project_url(non_seen_project),
            project_urls,
            'User should not see project',
        )

    # Direct instance access tests
    @data(('owner', 'owners'), ('customer_support', 'support_users'))
    def test_user_can_see_its_owner_membership_in_a_service_he_is_owner_of(
        self, user_data
    ):
        user, response_field = user_data
        self.client.force_authenticate(user=getattr(self.fixture, user))
        response = self.client.get(self._get_customer_url(self.fixture.customer))
        users = set(c['url'] for c in response.data[response_field])

        user_url = self._get_user_url(getattr(self.fixture, user))
        self.assertEqual([user_url], list(users))

    @data('staff', 'global_support')
    def test_user_can_access_all_customers_if_he_is_staff(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        self._check_user_direct_access_customer(
            self.fixture.customer, status.HTTP_200_OK
        )

        customer = factories.CustomerFactory()
        self._check_user_direct_access_customer(customer, status.HTTP_200_OK)

    # Helper methods
    def _check_user_list_access_customers(self, customer, test_function):
        response = self.client.get(reverse('customer-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        urls = set([instance['url'] for instance in response.data])
        url = self._get_customer_url(customer)
        getattr(self, test_function)(url, urls)

    def _check_customer_in_list(self, customer, positive=True):
        response = self.client.get(reverse('customer-list'))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        urls = set([instance['url'] for instance in response.data])
        customer_url = self._get_customer_url(customer)
        if positive:
            self.assertIn(customer_url, urls)
        else:
            self.assertNotIn(customer_url, urls)

    def _check_user_direct_access_customer(self, customer, status_code):
        response = self.client.get(self._get_customer_url(customer))
        self.assertEqual(response.status_code, status_code)


@ddt
class CustomerDeleteTest(CustomerBaseTest):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    # Deletion tests
    @data(
        'owner', 'admin', 'manager', 'global_support', 'customer_support', 'member',
    )
    def test_user_cannot_delete_customer(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.delete(self._get_customer_url(self.fixture.customer))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_cannot_delete_customer_with_associated_projects_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)

        factories.ProjectFactory(customer=self.fixture.customer)
        response = self.client.delete(self._get_customer_url(self.fixture.customer))

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertDictContainsSubset(
            {'detail': 'Cannot delete organization with existing projects'},
            response.data,
        )


class BaseCustomerMutationTest(CustomerBaseTest):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    # Helper methods
    def _get_valid_payload(self, resource=None):
        resource = resource or factories.CustomerFactory()

        return {
            'name': resource.name,
            'abbreviation': resource.abbreviation,
            'contact_details': resource.contact_details,
        }

    def _check_single_customer_field_change_permission(self, customer, status_code):
        payload = self._get_valid_payload(customer)

        for field, value in payload.items():
            data = {field: value}

            response = self.client.patch(self._get_customer_url(customer), data)
            self.assertEqual(response.status_code, status_code)


@ddt
class CustomerCreateTest(BaseCustomerMutationTest):
    @data('user', 'global_support')
    def test_user_can_not_create_customer_if_he_is_not_staff(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_waldur_core_settings(CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION=True)
    def test_default_project_is_created_if_configured(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        customer = Customer.objects.get(uuid=response.data['uuid'])
        self.assertEqual(customer.projects.count(), 1)
        self.assertEqual(customer.projects.first().name, 'First project')
        self.assertEqual(
            customer.projects.first().description,
            'First project we have created for you',
        )

    @override_waldur_core_settings(
        CREATE_DEFAULT_PROJECT_ON_ORGANIZATION_CREATION=False
    )
    def test_default_project_is_not_created_if_configured(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        customer = Customer.objects.get(uuid=response.data['uuid'])
        self.assertEqual(customer.projects.count(), 0)

    @override_waldur_core_settings(OWNER_CAN_MANAGE_CUSTOMER=True)
    def test_user_can_create_customer_if_he_is_not_staff_if_settings_are_tweaked(self):
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        # User became owner of created customer
        self.assertEqual(response.data['owners'][0]['uuid'], self.fixture.user.uuid.hex)

    def test_user_can_create_customer_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.post(
            factories.CustomerFactory.get_list_url(), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    @override_waldur_core_settings(OWNER_CAN_MANAGE_CUSTOMER=True)
    def test_domain_name_is_filled_from_user_organization(self):
        self.fixture.user.organization = 'ut.ee'
        self.fixture.user.save()

        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.post(
            factories.CustomerFactory.get_list_url(), {'name': 'Computer Science Lab'}
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['domain'], 'ut.ee')

    def test_domain_name_is_filled_from_input_for_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            factories.CustomerFactory.get_list_url(),
            {'name': 'Computer Science Lab', 'domain': 'ut.ee'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['domain'], 'ut.ee')

    @override_waldur_core_settings(OWNER_CAN_MANAGE_CUSTOMER=True)
    def test_domain_name_is_not_filled_from_input_for_owner(self):
        self.fixture.user.organization = ''
        self.fixture.user.save()
        self.client.force_authenticate(user=self.fixture.user)
        response = self.client.post(
            factories.CustomerFactory.get_list_url(),
            {'name': 'Computer Science Lab', 'domain': 'ut.ee'},
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data['domain'], '')


@ddt
class CustomerUpdateTest(BaseCustomerMutationTest):
    @data('manager', 'admin', 'customer_support', 'member', 'global_support')
    def test_user_cannot_change_customer_as_whole(self, user):
        self.client.force_authenticate(user=getattr(self.fixture, user))

        response = self.client.put(
            self._get_customer_url(self.fixture.customer), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_cannot_change_customer_he_is_owner_of(self):
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.put(
            self._get_customer_url(self.fixture.customer), self._get_valid_payload()
        )

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_user_can_change_customer_as_whole_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.put(
            self._get_customer_url(self.fixture.customer), self._get_valid_payload()
        )
        self.assertEqual(
            response.status_code,
            status.HTTP_200_OK,
            'Error message: %s' % response.data,
        )

    def test_user_cannot_change_single_customer_field_he_is_not_owner_of(self):
        self.client.force_authenticate(user=self.fixture.user)

        self._check_single_customer_field_change_permission(
            self.fixture.customer, status.HTTP_404_NOT_FOUND
        )

    def test_user_cannot_change_customer_field_he_is_owner_of(self):
        self.client.force_authenticate(user=self.fixture.owner)

        self._check_single_customer_field_change_permission(
            self.fixture.customer, status.HTTP_403_FORBIDDEN
        )

    def test_user_can_change_single_customer_field_if_he_is_staff(self):
        self.client.force_authenticate(user=self.fixture.staff)
        self._check_single_customer_field_change_permission(
            self.fixture.customer, status.HTTP_200_OK
        )

    def test_staff_can_change_organization_domain(self):
        self.client.force_authenticate(user=self.fixture.staff)

        response = self.client.patch(
            self._get_customer_url(self.fixture.customer), {'domain': 'ut.ee'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.domain, 'ut.ee')

    @override_waldur_core_settings(OWNER_CAN_MANAGE_CUSTOMER=True)
    def test_owner_can_not_change_organization_domain(self):
        self.client.force_authenticate(user=self.fixture.owner)

        response = self.client.patch(
            self._get_customer_url(self.fixture.customer), {'domain': 'ut.ee'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.domain, '')

    @mock.patch('waldur_core.structure.serializers.pyvat')
    def test_update_vat_code(self, mock_pyvat):
        self.client.force_authenticate(user=self.fixture.staff)

        class CheckResult:
            def __init__(self):
                self.business_name = ''
                self.business_address = ''
                self.is_valid = True
                self.log_lines = []

        check_result = CheckResult()
        mock_pyvat.check_vat_number.return_value = check_result

        response = self.client.patch(
            self._get_customer_url(self.fixture.customer), {'vat_code': 'ATU99999999'}
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.fixture.customer.refresh_from_db()
        self.assertEqual(self.fixture.customer.vat_code, 'ATU99999999')
        mock_pyvat.is_vat_number_format_valid.assert_called_once_with(
            'ATU99999999', None
        )
        mock_pyvat.check_vat_number.assert_called_once_with('ATU99999999', None)


class CustomerQuotasTest(test.APITransactionTestCase):
    def setUp(self):
        self.customer = factories.CustomerFactory()
        self.staff = factories.UserFactory(is_staff=True)

    def test_staff_can_edit_customer_quotas(self):
        self.client.force_login(self.staff)
        quota = self.customer.quotas.get(name=Customer.Quotas.nc_project_count)

        url = quota_factories.QuotaFactory.get_url(quota)
        response = self.client.put(url, {'limit': 100})
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        quota.refresh_from_db()
        self.assertEqual(quota.limit, 100)

    def test_owner_can_not_edit_customer_quotas(self):
        owner = factories.UserFactory()
        self.customer.add_user(owner, CustomerRole.OWNER)

        self.client.force_login(owner)
        quota = self.customer.quotas.get(name=Customer.Quotas.nc_project_count)

        url = quota_factories.QuotaFactory.get_url(quota)
        response = self.client.put(url, {'limit': 100})
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

        quota.refresh_from_db()
        self.assertNotEqual(quota.limit, 100)

    def test_customer_projects_quota_increases_on_project_creation(self):
        factories.ProjectFactory(customer=self.customer)
        self.assert_quota_usage('nc_project_count', 1)

    def test_customer_projects_quota_decreases_on_project_deletion(self):
        project = factories.ProjectFactory(customer=self.customer)
        project.delete()
        self.assert_quota_usage('nc_project_count', 0)

    def test_customer_users_quota_increases_on_adding_owner(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)
        self.assert_quota_usage('nc_user_count', 1)

    def test_customer_users_quota_decreases_on_removing_owner(self):
        user = factories.UserFactory()
        self.customer.add_user(user, CustomerRole.OWNER)
        self.customer.remove_user(user)
        self.assert_quota_usage('nc_user_count', 0)

    def test_customer_users_quota_increases_on_adding_administrator(self):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()
        project.add_user(user, ProjectRole.ADMINISTRATOR)
        self.assert_quota_usage('nc_user_count', 1)

    def test_customer_users_quota_decreases_on_removing_administrator(self):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()
        project.add_user(user, ProjectRole.ADMINISTRATOR)
        project.remove_user(user)
        self.assert_quota_usage('nc_user_count', 0)

    def test_customer_quota_is_not_increased_on_adding_owner_as_administrator(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory(customer=self.customer)
        self.customer.add_user(user, CustomerRole.OWNER)
        project.add_user(user, ProjectRole.ADMINISTRATOR)

        self.assert_quota_usage('nc_user_count', 1)

    def test_customer_quota_is_not_increased_on_adding_owner_as_manager(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory(customer=self.customer)
        self.customer.add_user(user, CustomerRole.OWNER)
        project.add_user(user, ProjectRole.ADMINISTRATOR)

        self.assert_quota_usage('nc_user_count', 1)

    def test_customer_users_quota_decreases_when_one_project_is_deleted(self):
        project = factories.ProjectFactory(customer=self.customer)
        user = factories.UserFactory()

        project.add_user(user, ProjectRole.ADMINISTRATOR)
        self.assert_quota_usage('nc_user_count', 1)

        project.delete()
        self.assert_quota_usage('nc_user_count', 0)

    def test_customer_users_quota_decreases_when_projects_are_deleted_in_bulk(self):
        count = 2
        for _ in range(count):
            project = factories.ProjectFactory(customer=self.customer)
            user = factories.UserFactory()
            project.add_user(user, ProjectRole.ADMINISTRATOR)

        self.assert_quota_usage('nc_user_count', count)

        for p in self.customer.projects.all():
            p.delete()

        self.assert_quota_usage('nc_user_count', 0)

    def assert_quota_usage(self, name, value):
        self.assertEqual(value, self.customer.quotas.get(name=name).usage)


@ddt
class CustomerUsersListTest(test.APITransactionTestCase):
    all_users = (
        'staff',
        'owner',
        'manager',
        'admin',
        'customer_support',
        'member',
        'global_support',
    )

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.url = factories.CustomerFactory.get_url(
            self.fixture.customer, action='users'
        )

    @data(*all_users)
    def test_user_can_list_customer_users(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        # call fixture to initiate all users:
        for user in self.all_users:
            getattr(self.fixture, user)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 5)

        self.assertSetEqual(
            {user['role'] for user in response.data}, {'owner', 'support', None, None}
        )
        self.assertSetEqual(
            {user['uuid'] for user in response.data},
            {
                self.fixture.owner.uuid.hex,
                self.fixture.admin.uuid.hex,
                self.fixture.manager.uuid.hex,
                self.fixture.customer_support.uuid.hex,
                self.fixture.member.uuid.hex,
            },
        )
        self.assertSetEqual(
            {
                user['projects'] and user['projects'][0]['role'] or None
                for user in response.data
            },
            {None, 'admin', 'manager', 'member'},
        )

    def test_user_can_not_list_project_users(self):
        self.client.force_authenticate(self.fixture.user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_users_ordering_by_concatenated_name(self):
        walter = factories.UserFactory(full_name='', username='walter')
        admin = factories.UserFactory(full_name='admin', username='zzz')
        alice = factories.UserFactory(full_name='', username='alice')
        dave = factories.UserFactory(full_name='dave', username='dave')
        expected_order = [admin, alice, dave, walter]
        for user in expected_order:
            self.fixture.customer.add_user(user, CustomerRole.OWNER)

        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url + '?o=concatenated_name')
        for serialized_user, expected_user in zip(response.data, expected_order):
            self.assertEqual(serialized_user['uuid'], expected_user.uuid.hex)

        # reversed order
        response = self.client.get(self.url + '?o=-concatenated_name')
        for serialized_user, expected_user in zip(response.data, expected_order[::-1]):
            self.assertEqual(serialized_user['uuid'], expected_user.uuid.hex)

    def test_filter_by_email(self):
        walter = factories.UserFactory(
            full_name='', username='walter', email='walter@gmail.com'
        )
        admin = factories.UserFactory(
            full_name='admin', username='zzz', email='admin@waldur.com'
        )
        alice = factories.UserFactory(
            full_name='', username='alice', email='alice@gmail.com'
        )

        for user in [admin, alice, walter]:
            self.fixture.customer.add_user(user, CustomerRole.OWNER)
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url, {'email': 'gmail.com'})
        self.assertEqual(len(response.data), 2)

    def test_filter_by_roles(self):
        walter = factories.UserFactory(
            full_name='', username='walter', email='walter@gmail.com'
        )
        admin = factories.UserFactory(
            full_name='admin', username='zzz', email='admin@waldur.com'
        )
        alice = factories.UserFactory(
            full_name='', username='alice', email='alice@gmail.com'
        )

        self.fixture.customer.add_user(walter, CustomerRole.SUPPORT)
        self.fixture.project.add_user(walter, ProjectRole.MANAGER)

        self.fixture.customer.add_user(admin, CustomerRole.OWNER)
        self.fixture.project.add_user(admin, ProjectRole.ADMINISTRATOR)

        self.fixture.customer.add_user(alice, CustomerRole.SUPPORT)
        self.fixture.project.add_user(alice, ProjectRole.MEMBER)

        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url)
        self.assertEqual(len(response.data), 3)

        response = self.client.get(
            self.url, {'project_role': [ProjectRole.ADMINISTRATOR, ProjectRole.MANAGER]}
        )
        usernames = [item['username'] for item in response.data]
        self.assertEqual(len(usernames), 2)
        self.assertTrue(admin.username in usernames)
        self.assertTrue(walter.username in usernames)

        response = self.client.get(
            self.url, {'organization_role': [CustomerRole.SUPPORT]}
        )
        usernames = [item['username'] for item in response.data]
        self.assertEqual(len(usernames), 2)
        self.assertTrue(walter.username in usernames)
        self.assertTrue(alice.username in usernames)

        response = self.client.get(
            self.url, {'organization_role': [CustomerRole.OWNER]}
        )
        usernames = [item['username'] for item in response.data]
        self.assertEqual(len(usernames), 1)
        self.assertTrue(admin.username in usernames)

        response = self.client.get(
            self.url,
            {
                'organization_role': [CustomerRole.OWNER],
                'project_role': [ProjectRole.MEMBER],
            },
        )
        usernames = [item['username'] for item in response.data]
        self.assertEqual(len(usernames), 2)
        self.assertTrue(admin.username in usernames)
        self.assertTrue(alice.username in usernames)

    def test_is_service_manager_property_in_users_serializer(self):
        user = factories.UserFactory()
        self.fixture.customer.add_user(user, role=CustomerRole.SERVICE_MANAGER)
        self.client.force_authenticate(self.fixture.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['is_service_manager'], True)

    def test_filter_by_role_if_permission_is_not_active(self):
        user = factories.UserFactory()
        self.client.force_authenticate(self.fixture.staff)

        response = self.client.get(self.url, {'organization_role': 'service_manager'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)

        self.fixture.customer.add_user(user, role=CustomerRole.SERVICE_MANAGER)
        response = self.client.get(self.url, {'organization_role': 'service_manager'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]['is_service_manager'], True)

        permission = CustomerPermission.objects.get(
            customer=self.fixture.customer, user=user, role=CustomerRole.SERVICE_MANAGER
        )
        permission.is_active = False
        permission.save()
        response = self.client.get(self.url, {'organization_role': 'service_manager'})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 0)


@ddt
class CustomerCountersListTest(test.APITransactionTestCase):
    def setUp(self):
        self.fixture = fixtures.ServiceFixture()
        self.owner = self.fixture.owner
        self.customer_support = self.fixture.customer_support
        self.admin = self.fixture.admin
        self.manager = self.fixture.manager
        self.member = self.fixture.member
        self.customer = self.fixture.customer
        self.url = factories.CustomerFactory.get_url(self.customer, action='counters')

    @data('owner', 'customer_support')
    def test_user_can_get_customer_counters(self, user):
        self.client.force_authenticate(getattr(self.fixture, user))
        response = self.client.get(self.url, {'fields': ['users', 'projects']})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data, {'users': 5, 'projects': 1})


class UserCustomersFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.staff = factories.UserFactory(is_staff=True)
        self.user1 = factories.UserFactory()
        self.user2 = factories.UserFactory()

        self.customer1 = factories.CustomerFactory()
        self.customer2 = factories.CustomerFactory()

        self.customer1.add_user(self.user1, CustomerRole.OWNER)
        self.customer2.add_user(self.user1, CustomerRole.OWNER)
        self.customer2.add_user(self.user2, CustomerRole.OWNER)

    def test_staff_can_filter_customer_by_user(self):
        self.assert_staff_can_filter_customer_by_user(
            self.user1, {self.customer1, self.customer2}
        )
        self.assert_staff_can_filter_customer_by_user(self.user2, {self.customer2})

    def assert_staff_can_filter_customer_by_user(self, user, customers):
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            factories.CustomerFactory.get_list_url(),
            {'user_uuid': user.uuid.hex, 'fields': ['uuid']},
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(
            {customer['uuid'] for customer in response.data},
            {customer.uuid.hex for customer in customers},
        )


@ddt
class AccountingIsRunningFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.enabled_customers = factories.CustomerFactory.create_batch(2)
        future_date = timezone.now() + timezone.timedelta(days=1)
        self.disabled_customers = factories.CustomerFactory.create_batch(
            3, accounting_start_date=future_date
        )
        self.all_customers = self.enabled_customers + self.disabled_customers

    def count_customers(self, accounting_is_running=None):
        self.client.force_authenticate(factories.UserFactory(is_staff=True))
        url = factories.CustomerFactory.get_list_url()
        params = {}
        if accounting_is_running in (True, False):
            params['accounting_is_running'] = accounting_is_running
        response = self.client.get(url, params)
        return len(response.data)

    @data(
        (True, 'enabled_customers'),
        (False, 'disabled_customers'),
        (None, 'all_customers'),
    )
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=True)
    def test_feature_is_enabled(self, params):
        actual = self.count_customers(params[0])
        expected = len(getattr(self, params[1]))
        self.assertEqual(expected, actual)

    @data(True, False, None)
    @override_waldur_core_settings(ENABLE_ACCOUNTING_START_DATE=False)
    def test_feature_is_disabled(self, param):
        actual = self.count_customers({'accounting_is_running': param})
        expected = len(self.all_customers)
        self.assertEqual(expected, actual)


@override_waldur_core_settings(
    OWNER_CAN_MANAGE_CUSTOMER=True, OWNERS_CAN_MANAGE_OWNERS=True
)
class CustomerBlockedTest(CustomerBaseTest):
    def setUp(self):
        self.user = factories.UserFactory()
        self.customer = factories.CustomerFactory(blocked=True)
        self.customer.add_user(self.user, CustomerRole.OWNER)

    def test_blocked_organization_is_not_available_for_updating(self):
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_url(customer=self.customer)
        response = self.client.put(url, {'name': 'new_name'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_blocked_organization_is_not_available_for_deleting(self):
        self.client.force_authenticate(user=self.user)
        url = factories.CustomerFactory.get_url(customer=self.customer)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_creating_is_not_available_for_blocked_organization(self):
        self.client.force_authenticate(user=self.user)
        url = factories.ProjectFactory.get_list_url()
        data = {
            'name': 'New project name',
            'customer': factories.CustomerFactory.get_url(self.customer),
        }
        response = self.client.post(url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_deleting_is_not_available_for_blocked_organization(self):
        self.client.force_authenticate(user=self.user)
        project = factories.ProjectFactory(customer=self.customer)
        url = factories.ProjectFactory.get_url(project=project)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_updating_is_not_available_for_blocked_organization(self):
        self.client.force_authenticate(user=self.user)
        project = factories.ProjectFactory(customer=self.customer)
        url = factories.ProjectFactory.get_url(project=project)
        response = self.client.patch(url, {'name': 'New project name'})
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_permission_adding_is_not_available_for_blocked_organization(self):
        user = factories.UserFactory()
        data = {
            'customer': factories.CustomerFactory.get_url(self.customer),
            'user': factories.UserFactory.get_url(user),
            'role': CustomerRole.OWNER,
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post(reverse('customer_permission-list'), data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_permission_updating_is_not_available_for_blocked_organization(
        self,
    ):
        permission = factories.CustomerPermissionFactory(customer=self.customer)
        url = factories.CustomerPermissionFactory.get_url(permission)
        data = {
            'is_active': False,
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_customer_permission_deleting_is_not_available_for_blocked_organization(
        self,
    ):
        permission = factories.CustomerPermissionFactory(customer=self.customer)
        url = factories.CustomerPermissionFactory.get_url(permission)
        self.client.force_authenticate(user=self.user)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_permission_adding_is_not_available_for_blocked_organization(self):
        user = factories.UserFactory()
        project = factories.ProjectFactory(customer=self.customer)
        data = {
            'project': factories.ProjectFactory.get_url(project),
            'user': factories.UserFactory.get_url(user),
            'role': CustomerRole.OWNER,
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.post(reverse('project_permission-list'), data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_permission_updating_is_not_available_for_blocked_organization(
        self,
    ):
        project = factories.ProjectFactory(customer=self.customer)
        permission = factories.ProjectPermissionFactory(project=project)
        url = factories.ProjectPermissionFactory.get_url(permission)
        data = {
            'is_active': False,
        }
        self.client.force_authenticate(user=self.user)
        response = self.client.put(url, data)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_project_permission_deleting_is_not_available_for_blocked_organization(
        self,
    ):
        project = factories.ProjectFactory(customer=self.customer)
        permission = factories.ProjectPermissionFactory(project=project)
        url = factories.ProjectPermissionFactory.get_url(permission)
        self.client.force_authenticate(user=self.user)
        response = self.client.delete(url)
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


class CustomerDivisionFilterTest(test.APITransactionTestCase):
    def setUp(self):
        self.division = factories.DivisionFactory()
        self.customer1 = factories.CustomerFactory()
        self.customer2 = factories.CustomerFactory(division=self.division)
        self.user = fixtures.UserFixture().staff
        self.url = factories.CustomerFactory.get_list_url()

    def test_filters(self):
        """Test of customers' list filter by division name and division UUID."""
        rows = [
            {
                'name': 'division_name',
                'valid': self.division.name[2:],
                'invalid': 'invalid',
            },
            {
                'name': 'division_uuid',
                'valid': self.division.uuid.hex,
                'invalid': 'invalid',
            },
        ]

        self.client.force_authenticate(self.user)

        for row in rows:
            response = self.client.get(self.url, data={row['name']: row['valid']})
            self.assertEqual(status.HTTP_200_OK, response.status_code)
            self.assertEqual(len(response.data), 1)

            response = self.client.get(self.url, data={row['name']: row['invalid']})
            if row['name'] == 'division_uuid':
                self.assertEqual(status.HTTP_400_BAD_REQUEST, response.status_code)
            else:
                self.assertEqual(status.HTTP_200_OK, response.status_code)
                self.assertEqual(len(response.data), 0)
