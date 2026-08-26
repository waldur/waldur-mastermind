import unittest
from unittest.mock import MagicMock, Mock, patch

import pytest
from django.apps import apps
from drf_spectacular.openapi import AutoSchema
from drf_spectacular.utils import OpenApiParameter, OpenApiResponse
from rest_framework import serializers, status, viewsets
from rest_framework.serializers import ListSerializer

from waldur_core.core import signals as core_signals
from waldur_core.core.openapi_inspector import WaldurOpenApiInspector
from waldur_core.core.serializers import RestrictedSerializerMixin
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.utils import permission_factory


class WaldurOpenApiInspectorTest(unittest.TestCase):
    def setUp(self):
        self.view = MagicMock(spec=viewsets.ViewSet)

    def test_get_operation_with_permissions(self):
        """
        Verify that `x-permissions` field is added when a view action has a `_permissions` list.
        """
        # Arrange
        self.view.action = "create"
        self.view.create_permissions = [
            permission_factory(PermissionEnum.APPROVE_ORDER, ["*", "project"])
        ]
        inspector = WaldurOpenApiInspector()
        inspector.view = self.view

        # Act
        with patch.object(AutoSchema, "get_operation", return_value={}):
            operation = inspector.get_operation(
                "/api/dummies/", r"^/api/dummies/$", "/api/", "POST", MagicMock()
            )

        # Assert
        self.assertIn("x-permissions", operation)
        self.assertEqual(
            operation["x-permissions"],
            [{"permission": "ORDER.APPROVE", "scopes": ["*", "project"]}],
        )

    def test_get_operation_without_permissions(self):
        """
        Verify that `x-permissions` is not added if the action has no `_permissions` defined.
        """
        # Arrange
        self.view.action = "create"
        inspector = WaldurOpenApiInspector()
        inspector.view = self.view

        # Act
        with patch.object(AutoSchema, "get_operation", return_value={}):
            operation = inspector.get_operation(
                "/api/dummies/", r"^/api/dummies/$", "/api/", "POST", MagicMock()
            )

        # Assert
        self.assertNotIn("x-permissions", operation)

    def _head_operation(
        self, *, detail, action="list", count_enabled=False, count_disabled=False
    ):
        self.view.action = action
        self.view.detail = detail
        # The action callable carries the @count_action / @no_count_action
        # flags. Both are set explicitly because a bare MagicMock answers any
        # getattr with a truthy Mock, which would silently satisfy either flag.
        action_fn = MagicMock()
        action_fn.count_enabled = count_enabled
        action_fn.count_disabled = count_disabled
        setattr(self.view, action, action_fn)
        inspector = WaldurOpenApiInspector()
        inspector.view = self.view
        with patch.object(AutoSchema, "get_operation", return_value={}):
            return inspector.get_operation(
                "/api/dummies/", r"^/api/dummies/$", "/api/", "HEAD", MagicMock()
            )

    def test_head_count_emitted_for_collection(self):
        """A collection (list) endpoint keeps its HEAD `count` operation."""
        operation = self._head_operation(detail=False)

        self.assertIsNotNone(operation)
        self.assertEqual(
            operation["responses"], {"200": {"description": "No response body"}}
        )

    def test_head_count_emitted_for_opted_in_detail_action(self):
        """
        A detail-scoped action that opts in via @count_action (e.g.
        `/projects/{uuid}/list_users/`) keeps its HEAD `count` operation.
        """
        operation = self._head_operation(
            detail=True, action="list_users", count_enabled=True
        )

        self.assertIsNotNone(operation)
        self.assertEqual(
            operation["responses"], {"200": {"description": "No response body"}}
        )

    def test_head_count_suppressed_for_detail_action_without_opt_in(self):
        """A detail-scoped action that did not opt in gets no HEAD `count`."""
        operation = self._head_operation(
            detail=True, action="list_users", count_enabled=False
        )

        self.assertIsNone(operation)

    def test_head_count_suppressed_for_object_detail(self):
        """A single-object detail view (retrieve) does not get a HEAD `count`."""
        operation = self._head_operation(detail=True, action="retrieve")

        self.assertIsNone(operation)

    def test_head_count_suppressed_for_opted_out_collection_action(self):
        """
        A collection action returning a single object opts out via
        @no_count_action (e.g. `/users/dashboard-general-stats/`), so the SDK
        does not gain a dead `*Count` method.
        """
        operation = self._head_operation(
            detail=False, action="dashboard_general_stats", count_disabled=True
        )

        self.assertIsNone(operation)


class _TestSerializer(RestrictedSerializerMixin, serializers.ModelSerializer):
    """Test serializer for OpenAPI inspector tests."""

    class Meta:
        model = Mock()
        fields = ("id", "name", "url")


class _TestSerializerWithSignalFields(
    RestrictedSerializerMixin, serializers.ModelSerializer
):
    """Test serializer that will have signal-based fields added."""

    class Meta:
        model = Mock()
        fields = ("id", "name", "url")


def add_test_signal_fields(sender, fields, **kwargs):
    """Signal handler that adds test fields."""
    fields["signal_field_1"] = serializers.SerializerMethodField()
    fields["signal_field_2"] = serializers.ReadOnlyField()


class TestWaldurOpenApiInspectorSignalFields(unittest.TestCase):
    """Test cases for WaldurOpenApiInspector signal-based field inclusion."""

    def setUp(self):
        """Set up test fixtures."""
        self.inspector = WaldurOpenApiInspector()
        self.inspector.method = "GET"
        self.inspector.path = "/api/test/"

    def test_get_override_parameters_basic_serializer(self):
        """Test that basic serializer without signals works correctly."""
        # Mock the get_response_serializers method
        serializer = _TestSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        self.assertIsInstance(result[0], OpenApiParameter)
        self.assertEqual(result[0].name, RestrictedSerializerMixin.FIELDS_PARAM_NAME)
        self.assertEqual(set(result[0].enum), {"id", "name", "url"})

    def test_get_override_parameters_with_signal_fields(self):
        """Test that signal-based fields are included in field options."""
        # Connect our test signal handler
        core_signals.pre_serializer_fields.connect(
            sender=_TestSerializerWithSignalFields,
            receiver=add_test_signal_fields,
        )

        try:
            # Mock the get_response_serializers method
            serializer = _TestSerializerWithSignalFields()
            self.inspector.get_response_serializers = Mock(return_value=serializer)

            result = self.inspector.get_override_parameters()

            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], OpenApiParameter)
            self.assertEqual(
                result[0].name, RestrictedSerializerMixin.FIELDS_PARAM_NAME
            )

            # Should include both Meta fields and signal-added fields
            expected_fields = {"id", "name", "url", "signal_field_1", "signal_field_2"}
            self.assertEqual(set(result[0].enum), expected_fields)

        finally:
            # Clean up signal connection
            core_signals.pre_serializer_fields.disconnect(
                sender=_TestSerializerWithSignalFields,
                receiver=add_test_signal_fields,
            )

    def test_get_override_parameters_with_list_serializer(self):
        """Test that signal fields work with ListSerializer wrapper."""
        # Connect our test signal handler
        core_signals.pre_serializer_fields.connect(
            sender=_TestSerializerWithSignalFields,
            receiver=add_test_signal_fields,
        )

        try:
            # Create a ListSerializer wrapper
            child_serializer = _TestSerializerWithSignalFields()
            list_serializer = ListSerializer(child=child_serializer)

            self.inspector.get_response_serializers = Mock(return_value=list_serializer)

            result = self.inspector.get_override_parameters()

            self.assertEqual(len(result), 1)
            self.assertIsInstance(result[0], OpenApiParameter)

            # Should include both Meta fields and signal-added fields
            expected_fields = {"id", "name", "url", "signal_field_1", "signal_field_2"}
            self.assertEqual(set(result[0].enum), expected_fields)

        finally:
            # Clean up signal connection
            core_signals.pre_serializer_fields.disconnect(
                sender=_TestSerializerWithSignalFields,
                receiver=add_test_signal_fields,
            )

    def test_get_override_parameters_non_get_method(self):
        """Test that non-GET methods return empty parameters."""
        self.inspector.method = "POST"
        result = self.inspector.get_override_parameters()
        self.assertEqual(result, [])

    def test_get_override_parameters_non_restricted_serializer(self):
        """Test that non-RestrictedSerializerMixin serializers return empty parameters."""

        # Create a regular DRF serializer
        class RegularSerializer(serializers.ModelSerializer):
            class Meta:
                model = Mock()
                fields = ("id", "name")

        serializer = RegularSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()
        self.assertEqual(result, [])

    def test_get_override_parameters_fallback_to_meta_fields(self):
        """Test fallback to Meta fields when serializer instantiation fails."""

        # Mock a serializer class that raises an exception during instantiation
        class ProblematicSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            def __init__(self, *args, **kwargs):
                raise ValueError("Test exception")

            class Meta:
                model = Mock()
                fields = ("id", "name", "url")

        # Connect signal handler for this serializer
        core_signals.pre_serializer_fields.connect(
            sender=ProblematicSerializer,
            receiver=add_test_signal_fields,
        )

        try:
            # Mock get_response_serializers to return an instance that will trigger fallback
            mock_instance = Mock()
            mock_instance.__class__ = ProblematicSerializer
            self.inspector.get_response_serializers = Mock(return_value=mock_instance)

            result = self.inspector.get_override_parameters()

            self.assertEqual(len(result), 1)
            # Should include Meta fields + signal fields even in fallback mode
            expected_fields = {"id", "name", "url", "signal_field_1", "signal_field_2"}
            self.assertEqual(set(result[0].enum), expected_fields)

        finally:
            # Clean up signal connection
            core_signals.pre_serializer_fields.disconnect(
                sender=ProblematicSerializer,
                receiver=add_test_signal_fields,
            )

    def test_get_override_parameters_with_all_fields_meta(self):
        """Test behavior with Meta.fields = '__all__'."""

        class AllFieldsSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            def __init__(self, *args, **kwargs):
                raise ValueError("Test exception to trigger fallback")

            class Meta:
                model = Mock()
                fields = "__all__"

        mock_instance = Mock()
        mock_instance.__class__ = AllFieldsSerializer
        self.inspector.get_response_serializers = Mock(return_value=mock_instance)

        result = self.inspector.get_override_parameters()
        # Should return empty list for __all__ fields
        self.assertEqual(result, [])

    def test_get_override_parameters_single_field(self):
        """Test that single field serializers return empty parameters."""

        class SingleFieldSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            class Meta:
                model = Mock()
                fields = ("id",)

        serializer = SingleFieldSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()
        # Should return empty list for single field
        self.assertEqual(result, [])

    def test_get_override_parameters_no_fields(self):
        """Test that serializers with no fields return empty parameters."""

        class NoFieldsSerializer(
            RestrictedSerializerMixin, serializers.ModelSerializer
        ):
            class Meta:
                model = Mock()
                fields = ()

        serializer = NoFieldsSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()
        # Should return empty list for no fields
        self.assertEqual(result, [])

    def test_get_override_parameters_dict_response_with_serializer_class(self):
        """``responses={200: SerializerClass}`` must still surface ``?field=``.

        ``@extend_schema(responses={status.HTTP_200_OK: SomeSerializer})`` makes
        drf-spectacular's ``get_response_serializers()`` return the raw dict
        keyed by status code. Without normalisation, the inspector's
        ``isinstance(serializer, RestrictedSerializerMixin)`` check fails and
        the ``field`` projection parameter silently disappears from the schema,
        breaking SDK clients that rely on it (e.g. site-agent healthz).
        """
        self.inspector.get_response_serializers = Mock(
            return_value={status.HTTP_200_OK: _TestSerializer}
        )

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].name, RestrictedSerializerMixin.FIELDS_PARAM_NAME)
        self.assertEqual(set(result[0].enum), {"id", "name", "url"})

    def test_get_override_parameters_dict_response_with_serializer_instance(self):
        """``responses={200: SerializerClass()}`` must also work."""
        self.inspector.get_response_serializers = Mock(
            return_value={status.HTTP_200_OK: _TestSerializer()}
        )

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].enum), {"id", "name", "url"})

    def test_get_override_parameters_bare_serializer_class(self):
        """``responses=SerializerClass`` (no dict wrapper) must work."""
        self.inspector.get_response_serializers = Mock(return_value=_TestSerializer)

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].enum), {"id", "name", "url"})

    def test_get_override_parameters_openapi_response_wrapper(self):
        """``responses={200: OpenApiResponse(response=SerializerClass)}`` must work."""
        self.inspector.get_response_serializers = Mock(
            return_value={
                status.HTTP_200_OK: OpenApiResponse(response=_TestSerializer),
            }
        )

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].enum), {"id", "name", "url"})

    def test_get_override_parameters_dict_response_picks_2xx(self):
        """When responses dict mixes 2xx and error codes, the 2xx serializer wins."""
        self.inspector.get_response_serializers = Mock(
            return_value={
                status.HTTP_400_BAD_REQUEST: serializers.Serializer,
                status.HTTP_200_OK: _TestSerializer,
            }
        )

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        self.assertEqual(set(result[0].enum), {"id", "name", "url"})


@pytest.mark.django_db
class TestRealWorldSerializers(unittest.TestCase):
    """Integration tests with real waldur serializers."""

    def setUp(self):
        """Set up test fixtures."""
        self.inspector = WaldurOpenApiInspector()
        self.inspector.method = "GET"
        self.inspector.path = "/api/test/"

    def test_project_serializer_includes_signal_fields(self):
        """Test that ProjectSerializer includes billing and invoice signal fields."""
        from waldur_core.structure.serializers import ProjectSerializer

        # Mock the get_response_serializers method
        serializer = ProjectSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        field_enum = set(result[0].enum)

        # Check that signal-based fields are included
        self.assertIn("billing_price_estimate", field_enum)
        self.assertIn("project_credit", field_enum)

        # Check that regular Meta fields are also included
        self.assertIn("name", field_enum)
        self.assertIn("uuid", field_enum)
        self.assertIn("url", field_enum)

    def test_customer_serializer_includes_billing_fields(self):
        """Test that CustomerSerializer includes billing signal fields."""
        from waldur_core.structure.serializers import CustomerSerializer

        # Mock the get_response_serializers method
        serializer = CustomerSerializer()
        self.inspector.get_response_serializers = Mock(return_value=serializer)

        result = self.inspector.get_override_parameters()

        self.assertEqual(len(result), 1)
        field_enum = set(result[0].enum)

        # Check that billing signal-based fields are included
        self.assertIn("billing_price_estimate", field_enum)

        # Check that regular Meta fields are also included
        self.assertIn("name", field_enum)
        self.assertIn("uuid", field_enum)
        self.assertIn("url", field_enum)


@pytest.mark.django_db
def test_nullable_fk_slug_related_fields_have_allow_null():
    """Ensure every SlugRelatedField backed by a nullable FK declares allow_null=True.

    Without allow_null=True, the OpenAPI schema won't mark the field as nullable,
    and auto-generated SDK clients will crash when parsing null values
    (e.g. Python client does UUID(None) → TypeError).
    """
    violations = []

    # Collect all serializer classes from waldur apps
    serializer_classes = set()
    for app_config in apps.get_app_configs():
        if not app_config.name.startswith("waldur"):
            continue
        try:
            module = __import__(
                app_config.name + ".serializers", fromlist=["serializers"]
            )
        except ImportError:
            continue
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, serializers.Serializer)
                and attr is not serializers.Serializer
            ):
                serializer_classes.add(attr)

    for serializer_cls in sorted(serializer_classes, key=lambda c: c.__name__):
        meta = getattr(serializer_cls, "Meta", None)
        model = getattr(meta, "model", None) if meta else None

        # Get declared fields (class-level, not instantiated)
        for field_name, field in serializer_cls._declared_fields.items():
            if not isinstance(field, serializers.SlugRelatedField):
                continue
            if field.slug_field != "uuid":
                continue

            # Check if the model field is a nullable FK
            if model is None:
                continue
            try:
                model_field = model._meta.get_field(field_name)
            except Exception:
                continue
            if not getattr(model_field, "null", False):
                continue

            # Model FK is nullable — serializer field must have allow_null=True
            if not field.allow_null:
                violations.append(
                    f"{serializer_cls.__name__}.{field_name} "
                    f"(model {model.__name__}.{field_name} is nullable FK, "
                    f"but SlugRelatedField is missing allow_null=True)"
                )

    assert not violations, (
        "SlugRelatedField(slug_field='uuid') on nullable FK fields must set allow_null=True "
        "to produce correct OpenAPI specs for SDK client generation:\n"
        + "\n".join(f"  - {v}" for v in violations)
    )


class CountOperationSchemaGenerationTest(unittest.TestCase):
    """
    End-to-end schema checks for the HEAD `_count` companion operations that
    drive the generated SDK's `*Count` methods.
    """

    @staticmethod
    def _generate_schema(patterns):
        from unittest import mock

        from drf_spectacular.settings import spectacular_settings

        from waldur_core.core.openapi_generators import WaldurSchemaGenerator

        generator = WaldurSchemaGenerator(patterns=patterns)
        # Skip project-wide post-processing/validation hooks: they operate on
        # the full API surface and are irrelevant to the raw operation shape.
        with mock.patch.object(spectacular_settings, "POSTPROCESSING_HOOKS", []):
            return generator.get_schema(request=None, public=True)

    @staticmethod
    def _find_path(schema, suffix):
        for url, path_item in schema["paths"].items():
            if url.rstrip("/").endswith(suffix):
                return url, path_item
        raise AssertionError(f"No path ending in {suffix!r} in {list(schema['paths'])}")

    def test_nested_router_collection_gets_count_by_default(self):
        """
        A nested collection gets a HEAD `_count` operation by default, the same
        as a top-level collection (e.g. `/customers/{uuid}/users/`).
        """
        from django.urls import include, path
        from rest_framework import mixins, viewsets
        from rest_framework.response import Response

        from waldur_core.core.nested_routers import NestedSimpleRouter
        from waldur_core.core.routers import SortedDefaultRouter

        class _ChildSerializer(serializers.Serializer):
            name = serializers.CharField()

        class _ParentViewSet(viewsets.ViewSet):
            def list(self, request):
                return Response([])

        class _ChildViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
            serializer_class = _ChildSerializer
            queryset = apps.get_model("structure", "Customer").objects.none()

        parent_router = SortedDefaultRouter()
        parent_router.register("parents", _ParentViewSet, basename="parent")
        nested_router = NestedSimpleRouter(parent_router, "parents", lookup="parent")
        nested_router.register("children", _ChildViewSet, basename="child")

        patterns = [
            path("api/", include(parent_router.urls)),
            path("api/", include(nested_router.urls)),
        ]
        schema = self._generate_schema(patterns)

        _url, children = self._find_path(schema, "children")
        self.assertIn("get", children)
        self.assertIn(
            "head", children, "nested collection must expose a HEAD count operation"
        )
        self.assertTrue(children["head"]["operationId"].endswith("_count"))

    def test_explicit_operation_id_head_gets_distinct_count_id(self):
        """
        When a list route sets an explicit @extend_schema(operation_id=...), the
        auto-added HEAD companion inherits it and would collide; it must get a
        distinct `_count` id instead — whether or not the GET id ends in `_list`.
        """
        from django.urls import include, path
        from drf_spectacular.utils import extend_schema, extend_schema_view
        from rest_framework import mixins, viewsets

        from waldur_core.core.routers import SortedDefaultRouter

        class _ThingSerializer(serializers.Serializer):
            name = serializers.CharField()

        def _head_id_for(explicit_id):
            @extend_schema_view(list=extend_schema(operation_id=explicit_id))
            class _ThingViewSet(mixins.ListModelMixin, viewsets.GenericViewSet):
                serializer_class = _ThingSerializer
                queryset = apps.get_model("structure", "Customer").objects.none()

            router = SortedDefaultRouter()
            router.register("things", _ThingViewSet, basename="thing")
            schema = self._generate_schema([path("api/", include(router.urls))])
            _url, things = self._find_path(schema, "things")
            self.assertEqual(things["get"]["operationId"], explicit_id)
            return things["head"]["operationId"]

        # GET id ending in `_list` → swap the suffix.
        self.assertEqual(
            _head_id_for("my_custom_things_list"), "my_custom_things_count"
        )
        # GET id not ending in `_list` → append `_count` (would otherwise
        # collide with the GET and fail `--fail-on-warn`).
        self.assertEqual(_head_id_for("my_custom_things"), "my_custom_things_count")

    def test_detail_action_count_is_opt_in(self):
        """
        A detail-scoped list action gets a HEAD `_count` operation only when it
        opts in via @count_action (e.g. `/projects/{uuid}/list_users/`); a plain
        object retrieve and a non-opted-in list action do not.
        """
        from django.urls import include, path
        from drf_spectacular.utils import extend_schema
        from rest_framework import viewsets
        from rest_framework.decorators import action
        from rest_framework.response import Response

        from waldur_core.core.routers import SortedDefaultRouter
        from waldur_core.core.views import count_action

        class _ThingSerializer(serializers.Serializer):
            name = serializers.CharField()

        class _ThingViewSet(viewsets.ViewSet):
            serializer_class = _ThingSerializer

            def retrieve(self, request, pk=None):
                return Response({})

            @count_action
            @extend_schema(responses=_ThingSerializer(many=True))
            @action(detail=True, methods=["get"])
            def members(self, request, pk=None):
                return Response([])

            @extend_schema(responses=_ThingSerializer(many=True))
            @action(detail=True, methods=["get"])
            def guests(self, request, pk=None):
                return Response([])

        router = SortedDefaultRouter()
        router.register("things", _ThingViewSet, basename="thing")
        schema = self._generate_schema([path("api/", include(router.urls))])

        _url, members = self._find_path(schema, "members")
        self.assertIn("head", members)
        self.assertTrue(members["head"]["operationId"].endswith("_count"))

        _url, guests = self._find_path(schema, "guests")
        self.assertNotIn(
            "head", guests, "detail list action without opt-in must not expose a count"
        )

        _url, retrieve = self._find_path(schema, "{id}")
        self.assertNotIn(
            "head",
            retrieve,
            "single-object retrieve must not expose a count operation",
        )
