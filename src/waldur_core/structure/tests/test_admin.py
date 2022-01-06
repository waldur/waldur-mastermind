import json
import unittest
from datetime import datetime

import mock
from django.contrib.admin.sites import AdminSite
from django.db.models.deletion import ProtectedError
from django.test import TestCase
from rest_framework import serializers as rf_serializers

from waldur_core.structure import admin as structure_admin
from waldur_core.structure import models as structure_models
from waldur_core.structure.tests import factories, fixtures


class MockRequest:
    pass


class MockSuperUser:
    def has_perm(self, perm):
        return True


request = MockRequest()
request.user = MockSuperUser()


class ServiceSettingAdminTest(TestCase):
    def test_if_required_field_value_is_provided_form_is_valid(self):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            backend_url = rf_serializers.CharField()

        self.assertTrue(
            self.form_is_valid(ServiceOptionsSerializer, backend_url='http://test.net')
        )

    @unittest.skip('TODO: fails randomly')
    def test_if_required_extra_field_value_is_provided_form_is_valid(self):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source='options.tenant')

        self.assertTrue(
            self.form_is_valid(
                ServiceOptionsSerializer, options=json.dumps({'tenant': 1})
            )
        )

    def test_if_required_extra_field_value_is_not_provided_form_is_invalid(self):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source='options.tenant')

        self.assertFalse(self.form_is_valid(ServiceOptionsSerializer))

    def test_if_options_is_not_valid_json_form_is_invalid(self):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source='options.tenant')

        self.assertFalse(
            self.form_is_valid(ServiceOptionsSerializer, options='INVALID')
        )

    def test_if_required_field_is_not_filled_but_it_has_got_default_value_form_is_valid(
        self,
    ):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source='options.tenant', default='admin')

        self.assertTrue(self.form_is_valid(ServiceOptionsSerializer))

    def form_is_valid(self, serializer_class, **kwargs):
        data = {
            'type': 'Test',
            'name': 'test',
            'state': 1,
            'username': 'test',
            'password': 'xxx',
            'options': json.dumps({}),
        }
        data.update(kwargs)
        with mock.patch(
            'waldur_core.structure.admin.get_options_serializer_class'
        ) as mock_class:
            with mock.patch(
                'waldur_core.structure.serializers.ServiceOptionsSerializer.get_subclasses'
            ) as mock_subclasses:
                with mock.patch(
                    'waldur_core.structure.admin.get_service_type'
                ) as mock_key:
                    mock_key.return_value = 'Test'
                    mock_class.return_value = serializer_class
                    mock_subclasses.return_value = [serializer_class]
                    site = AdminSite()
                    model_admin = structure_admin.PrivateServiceSettingsAdmin(
                        structure_models.PrivateServiceSettings, site
                    )
                    form = model_admin.get_form(request)(data)
                    return form.is_valid()


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
        project = self.change_project(members=[user1.pk, user2.pk])

        # Asset
        self.assertTrue(project.has_user(user1, structure_models.ProjectRole.MEMBER))
        self.assertTrue(project.has_user(user2, structure_models.ProjectRole.MEMBER))

    def test_old_users_are_deleted_and_existing_are_preserved(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()
        self.project.add_user(user1, structure_models.ProjectRole.MEMBER)

        # Act
        project = self.change_project(members=[user2.pk])

        # Asset
        self.assertFalse(project.has_user(user1, structure_models.ProjectRole.MEMBER))
        self.assertTrue(project.has_user(user2, structure_models.ProjectRole.MEMBER))

    def test_user_may_have_only_one_role_in_the_same_project(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        with self.assertRaises(ValueError):
            self.change_project(
                members=[user1.pk, user2.pk], managers=[user1.pk, user2.pk]
            )


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

        post_data.update(
            dict(accounting_start_date_0=dt_date, accounting_start_date_1=dt_time)
        )

        post_data.update(kwargs)

        form = model_admin.get_form(request)(instance=self.customer, data=post_data)
        form.save()

        self.customer.refresh_from_db()
        return self.customer

    def test_new_support_users_are_added(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        customer = self.change_customer(support_users=[user1.pk, user2.pk])

        # Asset
        self.assertTrue(customer.has_user(user1, structure_models.CustomerRole.SUPPORT))
        self.assertTrue(customer.has_user(user2, structure_models.CustomerRole.SUPPORT))

    def test_new_customer_owners_are_added(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        customer = self.change_customer(owners=[user1.pk, user2.pk])

        # Asset
        self.assertTrue(customer.has_user(user1, structure_models.CustomerRole.OWNER))
        self.assertTrue(customer.has_user(user2, structure_models.CustomerRole.OWNER))

    def test_new_service_managers_are_added(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        customer = self.change_customer(service_managers=[user1.pk, user2.pk])

        # Asset
        self.assertTrue(
            customer.has_user(user1, structure_models.CustomerRole.SERVICE_MANAGER)
        )
        self.assertTrue(
            customer.has_user(user2, structure_models.CustomerRole.SERVICE_MANAGER)
        )

    def test_user_can_be_owner_and_service_manager(self):
        # Arrange
        user = factories.UserFactory()
        self.customer.add_user(user, structure_models.CustomerRole.OWNER)

        # Act
        customer = self.change_customer(service_managers=[user], owners=[user])

        # Asset
        self.assertTrue(
            customer.has_user(user, structure_models.CustomerRole.SERVICE_MANAGER)
        )
        self.assertTrue(customer.has_user(user, structure_models.CustomerRole.OWNER))

    def test_old_users_are_deleted_and_existing_are_preserved(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()
        self.customer.add_user(user1, structure_models.CustomerRole.SUPPORT)

        # Act
        customer = self.change_customer(support_users=[user2.pk])

        # Asset
        self.assertFalse(
            customer.has_user(user1, structure_models.CustomerRole.SUPPORT)
        )
        self.assertTrue(customer.has_user(user2, structure_models.CustomerRole.SUPPORT))

    def test_user_may_have_only_one_role_in_the_same_customer(self):
        # Arrange
        user1 = factories.UserFactory()
        user2 = factories.UserFactory()

        # Act
        with self.assertRaises(ValueError):
            self.change_customer(
                support_users=[user1.pk, user2.pk], owners=[user1.pk, user2.pk]
            )

    def test_customer_deleting_is_possible_only_if_related_project_is_removed(self):
        site = AdminSite()
        model_admin = structure_admin.CustomerAdmin(structure_models.Customer, site)
        project = factories.ProjectFactory(customer=self.customer)
        request = MockRequest()
        queryset = structure_models.Customer.objects.filter(pk=self.customer.id)

        self.assertRaises(
            ProtectedError, model_admin.delete_queryset, request, queryset
        )
        project_id = project.id
        project.delete()

        # A project exists in DB because we use soft-delete for projects.
        self.assertTrue(structure_models.Project.objects.filter(pk=project_id).exists())

        model_admin.delete_queryset(request, queryset)
        self.assertRaises(
            structure_models.Customer.DoesNotExist, self.customer.refresh_from_db
        )
