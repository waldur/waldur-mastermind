import copy
import json
from datetime import datetime

from django.contrib.admin.sites import AdminSite
from django.test import TestCase

from waldur_core.structure import admin as structure_admin
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import fixtures, factories
from waldur_core.structure.tests.serializers import ServiceSerializer
from waldur_core.structure.utils import get_all_services_field_info, FieldInfo


class MockRequest:
    pass


class MockSuperUser:
    def has_perm(self, perm):
        return True


request = MockRequest()
request.user = MockSuperUser()


class override_serializer(object):
    def __init__(self, field_info):
        self.field_info = field_info
        self.required = copy.copy(ServiceSerializer.Meta.required_fields)

        if ServiceSerializer.SERVICE_ACCOUNT_FIELDS is not NotImplemented:
            self.fields = copy.copy(ServiceSerializer.SERVICE_ACCOUNT_FIELDS)
        else:
            self.fields = NotImplemented

        if ServiceSerializer.SERVICE_ACCOUNT_EXTRA_FIELDS is not NotImplemented:
            self.extra_fields = copy.copy(ServiceSerializer.SERVICE_ACCOUNT_EXTRA_FIELDS)
        else:
            self.extra_fields = NotImplemented

    def __enter__(self):
        ServiceSerializer.Meta.required_fields = self.field_info.fields_required
        ServiceSerializer.SERVICE_ACCOUNT_FIELDS = {
            field: ''
            for field in self.field_info.fields
        }
        ServiceSerializer.SERVICE_ACCOUNT_EXTRA_FIELDS = {
            field: ''
            for field in self.field_info.extra_fields_required
        }
        return ServiceSerializer

    def __exit__(self, *args):
        ServiceSerializer.Meta.required_fields = self.required
        ServiceSerializer.SERVICE_ACCOUNT_FIELDS = self.fields
        ServiceSerializer.SERVICE_ACCOUNT_EXTRA_FIELDS = self.extra_fields


class ServiceSettingAdminTest(TestCase):
    def setUp(self):
        super(ServiceSettingAdminTest, self).setUp()
        get_all_services_field_info.cache_clear()

    def test_if_required_field_value_is_provided_form_is_valid(self):
        fields = FieldInfo(
            fields_required=['backend_url'],
            fields=['backend_url'],
            extra_fields_required=[]
        )

        data = self.get_valid_data(backend_url='http://test.net')
        self.assert_form_valid(fields, data)

    def test_if_required_field_value_is_not_provided_form_is_invalid(self):
        fields = FieldInfo(
            fields_required=['backend_url'],
            fields=['backend_url'],
            extra_fields_required=[]
        )

        data = self.get_valid_data()
        self.assert_form_invalid(fields, data)

    def test_if_required_extra_field_value_is_provided_form_is_valid(self):
        fields = FieldInfo(
            fields_required=['tenant'],
            fields=[],
            extra_fields_required=['tenant']
        )
        data = self.get_valid_data(options=json.dumps({'tenant': 1}))
        self.assert_form_valid(fields, data)

    def test_if_required_extra_field_value_is_not_provided_form_is_invalid(self):
        fields = FieldInfo(
            fields_required=['tenant'],
            fields=[],
            extra_fields_required=['tenant']
        )
        data = self.get_valid_data()
        self.assert_form_invalid(fields, data)

    def test_if_options_is_not_valid_json_form_is_invalid(self):
        fields = FieldInfo(
            fields_required=['tenant'],
            fields=[],
            extra_fields_required=['tenant']
        )
        data = self.get_valid_data(options='INVALID')
        self.assert_form_invalid(fields, data)

    def get_valid_data(self, **kwargs):
        data = {
            'type': 'Test',
            'name': 'test',
            'state': 1,
            'username': 'test',
            'password': 'xxx',
            'options': json.dumps({}),
        }
        data.update(kwargs)
        return data

    def form_is_valid(self, fields, data):
        with override_serializer(fields):
            site = AdminSite()
            model_admin = structure_admin.PrivateServiceSettingsAdmin(
                structure_models.PrivateServiceSettings, site)
            form = model_admin.get_form(request)(data)
            return form.is_valid()

    def assert_form_valid(self, fields, data):
        self.assertTrue(self.form_is_valid(fields, data))

    def assert_form_invalid(self, fields, data):
        self.assertFalse(self.form_is_valid(fields, data))


class ProjectAdminTest(TestCase):
    def setUp(self):
        super(ProjectAdminTest, self).setUp()
        self.fixture = fixtures.ProjectFixture()
        self.project = self.fixture.project
        self.created_by = factories.UserFactory()

    def change_project(self, **kwargs):
        site = AdminSite()
        model_admin = structure_admin.ProjectAdmin(structure_models.Project, site)

        request = MockRequest()
        request.user = self.created_by

        form_for_data = model_admin.get_form(request)(instance=self.project)
        post_data = form_for_data.initial
        post_data.update(kwargs)

        form = model_admin.get_form(request)(instance=self.project, data=post_data)
        form.save()

        self.project.refresh_from_db()
        return self.project

    def test_new_users_are_added(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        project = self.change_project(support_users=[user1.pk, user2.pk])

        # Asset
        self.assertTrue(project.has_user(user1, structure_models.ProjectRole.SUPPORT))
        self.assertTrue(project.has_user(user2, structure_models.ProjectRole.SUPPORT))

    def test_old_users_are_deleted_and_existing_are_preserved(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()
        self.project.add_user(user1, structure_models.ProjectRole.SUPPORT)

        # Act
        project = self.change_project(support_users=[user2.pk])

        # Asset
        self.assertFalse(project.has_user(user1, structure_models.ProjectRole.SUPPORT))
        self.assertTrue(project.has_user(user2, structure_models.ProjectRole.SUPPORT))

    def test_user_may_have_only_one_role_in_the_same_project(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        with self.assertRaises(ValueError):
            self.change_project(support_users=[user1.pk, user2.pk], managers=[user1.pk, user2.pk])


class CustomerAdminTest(TestCase):
    def setUp(self):
        super(CustomerAdminTest, self).setUp()
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.created_by = factories.UserFactory()

    def change_customer(self, **kwargs):
        site = AdminSite()
        model_admin = structure_admin.CustomerAdmin(structure_models.Customer, site)

        request = MockRequest()
        request.user = self.created_by

        form_for_data = model_admin.get_form(request)(instance=self.customer)
        post_data = form_for_data.initial

        dt_now = datetime.now()
        dt_date = dt_now.strftime("%Y-%m-%d")
        dt_time = dt_now.strftime("%H:%M:%S")

        post_data.update(dict(
            accounting_start_date_0=dt_date,
            accounting_start_date_1=dt_time
        ))

        post_data.update(kwargs)

        form = model_admin.get_form(request)(instance=self.customer, data=post_data)
        form.save()

        self.customer.refresh_from_db()
        return self.customer

    def test_new_users_are_added(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        customer = self.change_customer(support_users=[user1.pk, user2.pk])

        # Asset
        self.assertTrue(customer.has_user(user1, structure_models.CustomerRole.SUPPORT))
        self.assertTrue(customer.has_user(user2, structure_models.CustomerRole.SUPPORT))

    def test_old_users_are_deleted_and_existing_are_preserved(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()
        self.customer.add_user(user1, structure_models.CustomerRole.SUPPORT)

        # Act
        customer = self.change_customer(support_users=[user2.pk])

        # Asset
        self.assertFalse(customer.has_user(user1, structure_models.CustomerRole.SUPPORT))
        self.assertTrue(customer.has_user(user2, structure_models.CustomerRole.SUPPORT))

    def test_user_may_have_only_one_role_in_the_same_customer(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        with self.assertRaises(ValueError):
            self.change_customer(support_users=[user1.pk, user2.pk], owners=[user1.pk, user2.pk])
