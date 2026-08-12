import json
import unittest
from collections import namedtuple

from rest_framework import serializers
from rest_framework.generics import ListAPIView
from rest_framework.request import Request
from rest_framework.response import Response
from rest_framework.test import (
    APIRequestFactory,
    APITestCase,
    force_authenticate,
)
from rest_framework.views import APIView

from waldur_core.core import utils
from waldur_core.core.fields import TimestampField
from waldur_core.core.models import DESCRIPTION_LENGTH
from waldur_core.core.serializers import (
    Base64Field,
    DictSerializerField,
    GenericRelatedField,
    HTMLCleanField,
    RestrictedSerializerMixin,
)
from waldur_core.core.tests.helpers import EXPANDING_DESCRIPTION
from waldur_core.logging.utils import get_loggable_models
from waldur_core.structure.models import Project, UserAgreement
from waldur_core.structure.serializers import CustomerSerializer
from waldur_core.structure.tests.factories import CustomerFactory, UserFactory


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


class DictFieldTestSerializer(serializers.Serializer):
    DOCKER_IMAGES = DictSerializerField()


class DictSerializerFieldTest(unittest.TestCase):
    def setUp(self):
        self.python_dict = {"python": {"image": "python:3.12-alpine"}}
        self.python_json = """{
    "python": {
        "image": "python:3.12-alpine"
    }
    }"""

        self.TestSerializer = DictFieldTestSerializer

    def test_dict_field_serialization(self):
        """Test that Python dict is properly serialized to JSON string."""
        data = {"DOCKER_IMAGES": self.python_dict}
        serializer = self.TestSerializer(data)
        expected_dict = json.loads(self.python_json)
        actual_dict = json.loads(serializer.data["DOCKER_IMAGES"])
        self.assertEqual(
            actual_dict,
            expected_dict,
            f"Serialized JSON is different from expected. Expected: {expected_dict} Got: {actual_dict}",
        )

    def test_dict_field_deserialization(self):
        """Test that JSON string is properly deserialized to Python dict."""
        data = {"DOCKER_IMAGES": self.python_json}
        serializer = self.TestSerializer(data=data)

        self.assertTrue(
            serializer.is_valid(), f"Validation failed: {serializer.errors}"
        )

        self.assertEqual(
            serializer.validated_data["DOCKER_IMAGES"],
            self.python_dict,
            f"Deserialization failed: Expected: {self.python_dict} Got: {serializer.validated_data['DOCKER_IMAGES']}",
        )

    def test_invalid_json_handling(self):
        """Test that invalid JSON is rejected."""
        data = {"DOCKER_IMAGES": '{"invalid": json}'}
        serializer = self.TestSerializer(data=data)

        self.assertFalse(serializer.is_valid(), "Serializer should reject invalid JSON")
        self.assertIn(
            "DOCKER_IMAGES",
            serializer.errors,
            f"Expected validation error for DOCKER_IMAGES field. Got errors: {serializer.errors}",
        )

    def test_none_is_rejected(self):
        """Test that None values are rejected."""
        data = {"DOCKER_IMAGES": None}
        serializer = self.TestSerializer(data=data)

        # Should not be valid
        self.assertFalse(serializer.is_valid(), "Serializer should reject None values")


class GenericRelatedFieldTest(APITestCase):
    def setUp(self):
        self.user = UserFactory(is_staff=True)
        self.request = APIRequestFactory().get("/")
        self.request.user = self.user

        self.field = GenericRelatedField(related_models=get_loggable_models())
        self.field.root._context = {"request": self.request}

    def test_if_related_object_exists_it_is_deserialized(self):
        customer = CustomerFactory()
        valid_url = CustomerFactory.get_url(customer)
        self.assertEqual(self.field.to_internal_value(valid_url), customer)

    def test_if_related_object_does_not_exist_validation_error_is_raised(self):
        customer = CustomerFactory()
        valid_url = CustomerFactory.get_url(customer)
        customer.delete()
        self.assertRaises(
            serializers.ValidationError, self.field.to_internal_value, valid_url
        )

    def test_if_user_does_not_have_permissions_for_related_object_validation_error_is_raised(
        self,
    ):
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


Child = namedtuple("User", ("name", "url", "id"))

Parent = namedtuple("Parent", ("id", "parent_name", "child"))


class ChildRestrictedSerializer(RestrictedSerializerMixin, serializers.Serializer):
    name = serializers.ReadOnlyField()
    url = serializers.ReadOnlyField()
    id = serializers.ReadOnlyField()


class ParentSerializer(RestrictedSerializerMixin, serializers.Serializer):
    id = serializers.ReadOnlyField()
    parent_name = serializers.ReadOnlyField()
    child = ChildRestrictedSerializer()


class RestrictedSerializerView(APIView):
    def get(self, request):
        user = Child(name="Walter", url="http://example.com/Walter", id=1)
        serializer = ChildRestrictedSerializer(user, context={"request": request})
        return Response(serializer.data)


class NestedRestrictedSerializerView(APIView):
    def get(self, request):
        # Define data structures for our test objects
        child_obj = Child(
            name="Collision Jr.", url="http://example.com/CollisionJr", id=2
        )
        parent_obj = Parent(id=1, parent_name="Collision Sr.", child=child_obj)
        # Serialize the parent object
        serializer = ParentSerializer(parent_obj, context={"request": request})
        return Response(serializer.data)


class UserListView(ListAPIView):
    """
    A view that uses the mixin via its serializer_class in a `many=True` context.
    """

    # Use our serializer that has the mixin
    serializer_class = ChildRestrictedSerializer

    # Create a fake queryset to avoid database setup
    queryset = [
        Child(id=1, name="Walter", url="http://example.com/Walter"),
        Child(id=2, name="Jesse", url="http://example.com/Jesse"),
    ]


class RestrictedSerializerTest(APITestCase):
    def setUp(self):
        """
        Set up common objects for all tests in this class.
        """
        self.factory = APIRequestFactory()
        self.user = UserFactory()

    def make_request(self, view, fields=None):
        """
        Generalized helper to make an authenticated GET request to a given view.

        :param view: The view class to be tested.
        :param fields: An optional list of strings for the 'field' query parameter.
        :return: The response object from the view.
        """
        query_params = {}
        if fields:
            query_params[RestrictedSerializerMixin.FIELDS_PARAM_NAME] = fields

        request = self.factory.get("/", query_params)
        force_authenticate(request, self.user)
        response = view.as_view()(request)
        return response

    def test_serializer_returns_fields_required_in_request(self):
        """Tests the mixin on a simple, non-nested serializer."""
        fields_to_request = ["name", "url"]

        # Use the helper for the simple view
        response = self.make_request(RestrictedSerializerView, fields=fields_to_request)

        self.assertEqual(200, response.status_code)
        self.assertEqual(fields_to_request, list(response.data.keys()))

    def test_mixin_is_not_applied_on_nested_serializer_with_field_name_collision(self):
        fields_to_request = ["id", "child"]
        response = self.make_request(
            NestedRestrictedSerializerView, fields=fields_to_request
        )

        self.assertEqual(200, response.status_code)

        # 1. Assert the parent was filtered correctly
        self.assertEqual(
            set(fields_to_request),
            set(response.data.keys()),
            "Parent serializer did not return the correct fields.",
        )

        # 2. Assert the nested serializer was NOT filtered and returned ALL its fields
        expected_child_fields = {"id", "name", "url"}
        actual_child_fields = set(response.data["child"].keys())

        self.assertEqual(
            expected_child_fields,
            actual_child_fields,
            "Nested serializer was incorrectly filtered due to a name collision.",
        )

    def test_mixin_works_with_many_true(self):
        """
        Verify that field filtering is applied correctly for list views (`many=True`).
        """
        response = self.make_request(UserListView, fields=["id", "name"])

        # Assert that EACH item in the list has been correctly filtered
        for row in response.data:
            self.assertEqual(
                {"id", "name"},
                set(row.keys()),
                "Fields were not filtered correctly on items in a list response.",
            )


class SlugSerializerMixinTest(APITestCase):
    """Test SlugSerializerMixin uniqueness validation for staff users."""

    def setUp(self):
        """Set up test data and users."""
        self.factory = APIRequestFactory()
        self.staff_user = UserFactory(is_staff=True)
        self.regular_user = UserFactory(is_staff=False)

        # Create two customers with different slugs
        self.customer1 = CustomerFactory(name="Test Customer 1", slug="test-customer-1")
        self.customer2 = CustomerFactory(name="Test Customer 2", slug="test-customer-2")

        self.CustomerSerializer = CustomerSerializer

    def test_staff_cannot_set_duplicate_slug_on_create(self):
        """Test that staff cannot create a customer with duplicate slug."""
        request = Request(self.factory.post("/"))
        request.user = self.staff_user

        # Try to create a new customer with an existing slug
        data = {
            "name": "New Customer",
            "slug": self.customer1.slug,  # Use existing slug
        }

        serializer = self.CustomerSerializer(data=data, context={"request": request})

        # Should not be valid due to duplicate slug
        self.assertFalse(serializer.is_valid())
        self.assertIn("slug", serializer.errors)
        self.assertIn(
            "An object with this slug already exists.", str(serializer.errors["slug"])
        )

    def test_staff_cannot_set_duplicate_slug_on_update(self):
        """Test that staff cannot update a customer to have a duplicate slug."""
        request = Request(self.factory.patch("/"))
        request.user = self.staff_user

        # Try to update customer2's slug to match customer1's slug
        data = {
            "slug": self.customer1.slug,  # Use existing slug from customer1
        }

        serializer = self.CustomerSerializer(
            instance=self.customer2,
            data=data,
            partial=True,
            context={"request": request},
        )

        # Should not be valid due to duplicate slug
        self.assertFalse(serializer.is_valid())
        self.assertIn("slug", serializer.errors)
        self.assertIn(
            "An object with this slug already exists.", str(serializer.errors["slug"])
        )

    def test_staff_can_update_slug_to_unique_value(self):
        """Test that staff can update a customer's slug to a unique value."""
        request = Request(self.factory.patch("/"))
        request.user = self.staff_user

        # Update customer2's slug to a unique value
        data = {
            "slug": "unique-slug-123",
        }

        serializer = self.CustomerSerializer(
            instance=self.customer2,
            data=data,
            partial=True,
            context={"request": request},
        )

        # Should be valid
        self.assertTrue(serializer.is_valid())
        serializer.save()

        # Check that slug was updated
        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.slug, "unique-slug-123")

    def test_staff_can_update_without_changing_slug(self):
        """Test that staff can update a customer without changing its slug."""
        request = Request(self.factory.patch("/"))
        request.user = self.staff_user

        # Update customer2 with the same slug (no change)
        data = {
            "slug": self.customer2.slug,  # Same slug as before
            "name": "Updated Customer 2",
        }

        serializer = self.CustomerSerializer(
            instance=self.customer2,
            data=data,
            partial=True,
            context={"request": request},
        )

        # Should be valid - not changing slug to a duplicate
        self.assertTrue(serializer.is_valid())
        serializer.save()

        # Check that name was updated but slug remains the same
        self.customer2.refresh_from_db()
        self.assertEqual(self.customer2.slug, "test-customer-2")
        self.assertEqual(self.customer2.name, "Updated Customer 2")

    def test_regular_user_cannot_edit_slug(self):
        """Test that regular (non-staff) users cannot edit slug."""
        request = Request(self.factory.patch("/"))
        request.user = self.regular_user

        # Try to update slug as regular user
        data = {
            "slug": "new-slug-456",
        }

        serializer = self.CustomerSerializer(
            instance=self.customer2,
            data=data,
            partial=True,
            context={"request": request},
        )

        # The slug field should be read-only for non-staff users
        # So the slug should not be in validated_data
        serializer.is_valid()
        self.assertNotIn("slug", serializer.validated_data)


class HTMLCleanFieldTestSerializer(serializers.Serializer):
    content = HTMLCleanField(
        required=False, allow_blank=True, max_length=DESCRIPTION_LENGTH
    )


class HTMLCleanFieldTest(unittest.TestCase):
    def test_plain_text_within_limit_is_accepted(self):
        field = HTMLCleanField(max_length=DESCRIPTION_LENGTH)
        value = field.to_internal_value("plain text")
        self.assertEqual(value, "plain text")

    def test_value_expanded_by_html_clean_is_rejected_when_over_limit(self):
        field = HTMLCleanField(max_length=DESCRIPTION_LENGTH)
        # Exactly at pre-clean limit, but & -> &amp; expands past DESCRIPTION_LENGTH.
        with self.assertRaises(serializers.ValidationError) as ctx:
            field.to_internal_value("&" * DESCRIPTION_LENGTH)
        self.assertIn("too long", str(ctx.exception).lower())

    def test_expanded_value_error_names_html_sanitisation_as_the_cause(self):
        # An input shorter than the limit that only overflows once escaped must
        # not be reported as simply exceeding the limit — the user is looking at
        # a box with fewer characters in it than the number they are quoted.
        field = HTMLCleanField(max_length=DESCRIPTION_LENGTH)
        with self.assertRaises(serializers.ValidationError) as ctx:
            field.to_internal_value(EXPANDING_DESCRIPTION)
        self.assertIn("sanitisation", str(ctx.exception))

    def test_value_without_max_length_does_not_crash_after_html_clean(self):
        field = HTMLCleanField()
        value = field.to_internal_value("&" * DESCRIPTION_LENGTH)
        self.assertGreater(len(value), DESCRIPTION_LENGTH)

    def test_serializer_returns_400_when_cleaned_description_exceeds_limit(self):
        serializer = HTMLCleanFieldTestSerializer(
            data={"content": "Norouzi, M., & Hinton, G. " * 220}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("content", serializer.errors)

    def test_plain_oversize_value_keeps_the_standard_max_length_message(self):
        # Nothing to do with sanitisation: the value was too long as typed.
        serializer = HTMLCleanFieldTestSerializer(
            data={"content": "a" * (DESCRIPTION_LENGTH + 1)}
        )
        self.assertFalse(serializer.is_valid())
        self.assertEqual(
            serializer.errors["content"][0].code,
            "max_length",
        )
        self.assertNotIn("sanitisation", str(serializer.errors["content"]))


class HTMLCleanFieldModelSerializer(serializers.ModelSerializer):
    """An HTMLCleanField declared without max_length over a bounded column."""

    description = HTMLCleanField(required=False, allow_blank=True)

    class Meta:
        model = Project
        fields = ("name", "description")


class UnboundedHTMLCleanFieldModelSerializer(serializers.ModelSerializer):
    """The same declaration over an unbounded TextField column."""

    content = HTMLCleanField(required=False, allow_blank=True)

    class Meta:
        model = UserAgreement
        fields = ("content",)


class HTMLCleanFieldMaxLengthInferenceTest(APITestCase):
    """A declaration without max_length must not silently drop the length guard.

    A plain ModelSerializer inherits the model field's max_length validator.
    Redeclaring the field as HTMLCleanField used to throw that away, so an
    oversized value reached Postgres and raised DataError (HTTP 500) instead of
    being rejected with 400. The limit is now inferred from the model field.
    """

    def test_max_length_is_inferred_from_bounded_model_field(self):
        field = HTMLCleanFieldModelSerializer().fields["description"]
        self.assertEqual(field.max_length, DESCRIPTION_LENGTH)

    def test_inferred_limit_rejects_plain_oversize_value(self):
        serializer = HTMLCleanFieldModelSerializer(
            data={"name": "project", "description": "a" * (DESCRIPTION_LENGTH + 1)}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("description", serializer.errors)

    def test_inferred_limit_rejects_value_expanded_by_html_clean(self):
        serializer = HTMLCleanFieldModelSerializer(
            data={"name": "project", "description": EXPANDING_DESCRIPTION}
        )
        self.assertFalse(serializer.is_valid())
        self.assertIn("sanitisation", str(serializer.errors["description"]))

    def test_explicit_max_length_is_not_overridden(self):
        class ExplicitSerializer(HTMLCleanFieldModelSerializer):
            description = HTMLCleanField(required=False, max_length=10)

        self.assertEqual(ExplicitSerializer().fields["description"].max_length, 10)

    def test_unbounded_model_field_stays_unbounded(self):
        # TextField has no max_length, so nothing is inferred and long values pass.
        field = UnboundedHTMLCleanFieldModelSerializer().fields["content"]
        self.assertIsNone(field.max_length)
        self.assertGreater(
            len(field.to_internal_value("&" * DESCRIPTION_LENGTH)),
            DESCRIPTION_LENGTH,
        )

    def test_field_on_plain_serializer_is_not_affected(self):
        # No model to infer from, so the field stays unbounded.
        class PlainSerializer(serializers.Serializer):
            content = HTMLCleanField(required=False, allow_blank=True)

        self.assertIsNone(PlainSerializer().fields["content"].max_length)
