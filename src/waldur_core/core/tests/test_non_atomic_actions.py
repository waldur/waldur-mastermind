from unittest import mock

from django.test import SimpleTestCase, override_settings
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.test import APIRequestFactory

from waldur_core.core import views as core_views


class SampleViewSet(core_views.ActionsViewSet):
    permission_classes = ()
    non_atomic_actions = ("bulk",)

    @extend_schema(request=None)
    @action(detail=False, methods=["post"])
    def bulk(self, request):
        return Response({"ok": True})

    @extend_schema(request=None)
    @action(detail=False, methods=["post"])
    def single(self, request):
        return Response({"ok": True})


@override_settings(WALDUR_CORE={"USE_ATOMIC_TRANSACTION": True})
class NonAtomicActionsTest(SimpleTestCase):
    def call(self, name):
        view = SampleViewSet.as_view({"post": name})
        request = APIRequestFactory().post(f"/{name}/")
        with mock.patch("waldur_core.core.mixins.transaction.atomic") as atomic:
            response = view(request)
        self.assertEqual(response.status_code, 200)
        return atomic

    def test_a_listed_action_runs_outside_the_request_transaction(self):
        self.assertFalse(self.call("bulk").called)

    def test_other_actions_still_run_in_the_request_transaction(self):
        self.assertTrue(self.call("single").called)
