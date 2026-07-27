from datetime import timedelta

from constance.test import override_config
from django.test import RequestFactory
from django.utils import timezone
from rest_framework import test

from waldur_core.core.models import PersonalAccessToken
from waldur_core.logging.middleware import (
    CaptureEventContextMiddleware,
    get_event_context,
    reset_event_context,
    set_current_auth,
)
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.permissions.fixtures import CustomerRole
from waldur_core.structure.tests import factories, fixtures


class SetCurrentAuthTest(test.APITestCase):
    def tearDown(self):
        reset_event_context()

    def test_session_auth_is_recorded(self):
        set_current_auth(None)
        self.assertEqual(get_event_context()["auth_method"], "session")

    def test_pat_identity_is_recorded(self):
        pat = PersonalAccessToken(name="my token")
        set_current_auth(pat)
        context = get_event_context()
        self.assertEqual(context["auth_method"], "pat")
        self.assertEqual(context["pat_name"], "my token")
        self.assertEqual(context["pat_uuid"], pat.uuid.hex)

    def test_non_pat_auth_does_not_add_pat_keys(self):
        set_current_auth(None)
        self.assertNotIn("pat_uuid", get_event_context())


@override_config(PAT_ENABLED=True)
class PatAttributionEndToEndTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.customer = self.fixture.customer
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        # Roles start empty in the test DB; grant the owner the permission the
        # project-create POST below exercises (mirrors ProjectCreateTest's setup).
        CustomerRole.OWNER.add_permission(PermissionEnum.CREATE_PROJECT)

        expires_at = timezone.now() + timedelta(days=30)
        full_token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
        self.pat = PersonalAccessToken.objects.create(
            user=self.user,
            name="ci token",
            token_prefix=prefix,
            token_hash=token_hash,
            scopes=[PermissionEnum.CREATE_PROJECT.value],
            expires_at=expires_at,
        )
        self.plaintext = full_token

    def test_event_emitted_during_pat_request_is_marked(self):
        # The ticket's real requirement: an ordinary viewset/serializer event —
        # here PROJECT_CREATION_SUCCEEDED, emitted unconditionally from the
        # post-save handler well after authentication — must carry the PAT
        # attribution. The handler does NOT pass pat_uuid/pat_name itself (its
        # event_context is just {"project": instance}), so their presence proves
        # the attribution was stamped onto the shared event context by the
        # late-binding hook and inherited without touching the emit site.
        # Project creation is used over customer update because its handler fires
        # unconditionally, whereas customer update depends on FieldTracker state
        # that model_utils resets on save via receiver-order-dependent timing.
        response = self.client.post(
            factories.ProjectFactory.get_list_url(),
            {
                "name": "attributed project",
                "customer": factories.CustomerFactory.get_url(self.customer),
            },
            HTTP_AUTHORIZATION=f"Bearer {self.plaintext}",
        )
        self.assertEqual(response.status_code, 201, response.data)

        event = Event.objects.filter(event_type="project_creation_succeeded").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.context["auth_method"], "pat")
        self.assertEqual(event.context["pat_uuid"], self.pat.uuid.hex)
        self.assertEqual(event.context["pat_name"], "ci token")

    def test_session_request_is_marked_as_session(self):
        self.client.force_authenticate(user=self.user)
        self.client.post(
            "/api/personal-access-tokens/",
            {
                "name": "session made",
                "scopes": [PermissionEnum.LIST_ORDERS.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
        )
        event = Event.objects.filter(event_type="pat_created").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.context["auth_method"], "session")


class EventAuthMethodFilterTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.staff = self.fixture.staff
        self.client.force_authenticate(user=self.staff)
        Event.objects.create(
            event_type="resource_deleted",
            message="via pat",
            context={"auth_method": "pat", "pat_uuid": "a" * 32},
        )
        Event.objects.create(
            event_type="resource_deleted",
            message="via session",
            context={"auth_method": "session"},
        )

    def test_filter_by_auth_method(self):
        response = self.client.get("/api/events/", {"auth_method": "pat"})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["message"], "via pat")

    def test_filter_by_pat_uuid(self):
        response = self.client.get("/api/events/", {"pat_uuid": "a" * 32})
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["message"], "via pat")


class NoIpContextTest(test.APITestCase):
    def test_user_context_survives_missing_ip_address(self):
        request = RequestFactory().get("/")
        request.META.pop("REMOTE_ADDR", None)
        fixture = fixtures.CustomerFixture()
        request.user = fixture.owner

        CaptureEventContextMiddleware(lambda r: None).process_request(request)
        context = get_event_context()
        self.assertIsNotNone(context)
        self.assertIn("user_uuid", context)
        self.assertNotIn("ip_address", context)
