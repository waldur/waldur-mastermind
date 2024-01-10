import unittest
from collections import namedtuple

from rest_framework import serializers
from rest_framework.response import Response
from rest_framework.test import (
    APIRequestFactory,
    APITransactionTestCase,
    force_authenticate,
)
from rest_framework.views import APIView

from waldur_core.core import utils
from waldur_core.core.fields import TimestampField
from waldur_core.core.serializers import (
    Base64Field,
    GenericRelatedField,
    RestrictedSerializerMixin,
)
from waldur_core.logging.utils import get_loggable_models


class Base64Serializer(serializers.Serializer):
    content = Base64Field()


class Base64FieldTest(unittest.TestCase):
    def test_text_gets_base64_encoded_on_serialization(self):
        serializer = Base64Serializer(instance={"content": "hello"})
        actual = serializer.data["content"]

        self.assertEqual(b"aGVsbG8=", actual)

    def test_text_remains_base64_encoded_on_deserialization(self):
        serializer = Base64Serializer(data={"content": "Zm9vYmFy"})

        self.assertTrue(serializer.is_valid())

        actual = serializer.validated_data["content"]

        self.assertEqual("Zm9vYmFy", actual)

    def test_deserialization_fails_validation_on_incorrect_base64(self):
        serializer = Base64Serializer(data={"content": "***NOT-BASE-64***"})

        self.assertFalse(serializer.is_valid())
        self.assertIn(
            "content", serializer.errors, "There should be errors for content field"
        )
        self.assertIn(
            "This field should a be valid Base64 encoded string.",
            serializer.errors["content"],
        )


class GenericRelatedFieldTest(APITransactionTestCase):
    def setUp(self):
        from waldur_core.structure.tests.factories import UserFactory

        self.user = UserFactory(is_staff=True)
        self.request = APIRequestFactory().get("/")
        self.request.user = self.user

        self.field = GenericRelatedField(related_models=get_loggable_models())
        self.field.root._context = {"request": self.request}

    def test_if_related_object_exists_it_is_deserialized(self):
        from waldur_core.structure.tests.factories import CustomerFactory

        customer = CustomerFactory()
        valid_url = CustomerFactory.get_url(customer)
        self.assertEqual(self.field.to_internal_value(valid_url), customer)

    def test_if_related_object_does_not_exist_validation_error_is_raised(self):
        from waldur_core.structure.tests.factories import CustomerFactory

        customer = CustomerFactory()
        valid_url = CustomerFactory.get_url(customer)
        customer.delete()
        self.assertRaises(
            serializers.ValidationError, self.field.to_internal_value, valid_url
        )

    def test_if_user_does_not_have_permissions_for_related_object_validation_error_is_raised(
        self,
    ):
        from waldur_core.structure.tests.factories import CustomerFactory

        customer = CustomerFactory()
        valid_url = CustomerFactory.get_url(customer)
        self.user.is_staff = False
        self.user.save()
        self.assertRaises(
            serializers.ValidationError, self.field.to_internal_value, valid_url
        )

    def test_if_uuid_is_invalid_validation_error_is_raised(self):
        invalid_url = "https://example.com/api/customers/invalid/"
        self.assertRaises(
            serializers.ValidationError, self.field.to_internal_value, invalid_url
        )


class TimestampSerializer(serializers.Serializer):
    content = TimestampField()


class TimestampFieldTest(unittest.TestCase):
    def setUp(self):
        self.datetime = utils.timeshift(days=-1)
        self.timestamp = utils.datetime_to_timestamp(self.datetime)

    def test_datetime_serialized_as_timestamp(self):
        serializer = TimestampSerializer(instance={"content": self.datetime})
        actual = serializer.data["content"]
        self.assertEqual(self.timestamp, actual)

    def test_timestamp_parsed_as_datetime(self):
        serializer = TimestampSerializer(data={"content": str(self.timestamp)})
        self.assertTrue(serializer.is_valid())
        actual = serializer.validated_data["content"]
        self.assertEqual(self.datetime, actual)

    def test_incorrect_timestamp(self):
        serializer = TimestampSerializer(data={"content": "NOT_A_UNIX_TIMESTAMP"})
        self.assertFalse(serializer.is_valid())
        self.assertIn(
            "content", serializer.errors, "There should be errors for content field"
        )
        self.assertIn(
            'Value "NOT_A_UNIX_TIMESTAMP" should be valid UNIX timestamp.',
            serializer.errors["content"],
        )


class RestrictedSerializer(RestrictedSerializerMixin, serializers.Serializer):
    name = serializers.ReadOnlyField()
    url = serializers.ReadOnlyField()
    id = serializers.ReadOnlyField()


class RestrictedSerializerView(APIView):
    def get(self, request):
        User = namedtuple("User", ("name", "url", "id"))
        user = User(name="Walter", url="http://example.com/Walter", id=1)
        serializer = RestrictedSerializer(user, context={"request": request})
        return Response(serializer.data)


class RestrictedSerializerTest(APITransactionTestCase):
    def test_serializer_returns_fields_required_in_request(self):
        fields = ["name", "url"]
        response = self.make_request(fields)
        self.assertEqual(fields, list(response.data.keys()))

    def make_request(self, fields):
        from waldur_core.structure.tests.factories import UserFactory

        request = APIRequestFactory().get("/", {"field": fields})
        force_authenticate(request, UserFactory())
        response = RestrictedSerializerView.as_view()(request)
        return response
