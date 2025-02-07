import json
import unittest
from unittest import mock

from django.contrib.admin.sites import AdminSite
from django.test import TestCase
from rest_framework import serializers as rf_serializers

from waldur_core.structure import admin as structure_admin
from waldur_core.structure import models as structure_models


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
            self.form_is_valid(ServiceOptionsSerializer, backend_url="http://test.net")
        )

    @unittest.skip("TODO: fails randomly")
    def test_if_required_extra_field_value_is_provided_form_is_valid(self):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source="options.tenant")

        self.assertTrue(
            self.form_is_valid(
                ServiceOptionsSerializer, options=json.dumps({"tenant": 1})
            )
        )

    def test_if_required_extra_field_value_is_not_provided_form_is_invalid(self):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source="options.tenant")

        self.assertFalse(self.form_is_valid(ServiceOptionsSerializer))

    def test_if_options_is_not_valid_json_form_is_invalid(self):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source="options.tenant")

        self.assertFalse(
            self.form_is_valid(ServiceOptionsSerializer, options="INVALID")
        )

    @unittest.skip("TODO: fails randomly")
    def test_if_required_field_is_not_filled_but_it_has_got_default_value_form_is_valid(
        self,
    ):
        class ServiceOptionsSerializer(rf_serializers.Serializer):
            tenant = rf_serializers.CharField(source="options.tenant", default="admin")

        self.assertTrue(self.form_is_valid(ServiceOptionsSerializer))

    def form_is_valid(self, serializer_class, **kwargs):
        data = {
            "type": "Test",
            "name": "test",
            "state": 1,
            "username": "test",
            "password": "xxx",
            "options": json.dumps({}),
        }
        data.update(kwargs)
        with mock.patch(
            "waldur_core.structure.admin.get_options_serializer_class"
        ) as mock_class:
            with mock.patch(
                "waldur_core.structure.serializers.ServiceOptionsSerializer.get_subclasses"
            ) as mock_subclasses:
                with mock.patch(
                    "waldur_core.structure.admin.get_service_type"
                ) as mock_key:
                    mock_key.return_value = "Test"
                    mock_class.return_value = serializer_class
                    mock_subclasses.return_value = [serializer_class]
                    site = AdminSite()
                    model_admin = structure_admin.PrivateServiceSettingsAdmin(
                        structure_models.PrivateServiceSettings, site
                    )
                    form = model_admin.get_form(request)(data)
                    return form.is_valid()
