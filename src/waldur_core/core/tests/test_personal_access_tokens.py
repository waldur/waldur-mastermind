import ast
import hashlib
import pathlib
from datetime import timedelta

from constance.test import override_config
from django.utils import timezone
from rest_framework import status, test
from rest_framework.authtoken.models import Token

from waldur_core.core.models import PersonalAccessToken, User
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.tests import fixtures


def _create_pat(user, scopes=None, expires_at=None, name="test-pat", is_active=True):
    """Helper: create a PAT directly in the DB."""
    if scopes is None:
        scopes = [PermissionEnum.LIST_ORDERS.value]
    if expires_at is None:
        expires_at = timezone.now() + timedelta(days=30)
    full_token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
    pat = PersonalAccessToken.objects.create(
        user=user,
        name=name,
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=scopes,
        expires_at=expires_at,
        is_active=is_active,
    )
    pat._plaintext_token = full_token
    return pat


PAT_URL = "/api/personal-access-tokens/"


class PersonalAccessTokenCRUDTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user=self.user)

    def test_create_returns_plaintext_once(self):
        response = self.client.post(
            PAT_URL,
            {
                "name": "My token",
                "scopes": [PermissionEnum.LIST_ORDERS.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("token", response.data)
        self.assertTrue(response.data["token"].startswith("w_"))
        self.assertEqual(response["Cache-Control"], "no-store")

    def test_list_does_not_show_plaintext(self):
        _create_pat(self.user)
        response = self.client.get(PAT_URL)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(len(response.data), 1)
        self.assertNotIn("token", response.data[0])
        self.assertNotIn("token_hash", response.data[0])

    def test_retrieve_does_not_show_plaintext(self):
        pat = _create_pat(self.user)
        response = self.client.get(f"{PAT_URL}{pat.uuid}/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("token", response.data)
        self.assertNotIn("token_hash", response.data)

    def test_destroy_soft_revokes(self):
        pat = _create_pat(self.user)
        response = self.client.delete(f"{PAT_URL}{pat.uuid}/")
        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        pat.refresh_from_db()
        self.assertFalse(pat.is_active)

    def test_update_not_allowed(self):
        pat = _create_pat(self.user)
        response = self.client.put(
            f"{PAT_URL}{pat.uuid}/",
            {"name": "updated"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_patch_not_allowed(self):
        pat = _create_pat(self.user)
        response = self.client.patch(
            f"{PAT_URL}{pat.uuid}/",
            {"name": "updated"},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_405_METHOD_NOT_ALLOWED)

    def test_rotate_revokes_old_creates_new(self):
        pat = _create_pat(self.user)
        old_hash = pat.token_hash
        response = self.client.post(f"{PAT_URL}{pat.uuid}/rotate/")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertIn("token", response.data)

        pat.refresh_from_db()
        self.assertFalse(pat.is_active)

        new_pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertTrue(new_pat.is_active)
        self.assertNotEqual(new_pat.token_hash, old_hash)
        self.assertEqual(new_pat.scopes, pat.scopes)

    def test_user_can_only_see_own_tokens(self):
        _create_pat(self.user)
        other_user = User.objects.create_user(username="other", password="pass")
        _create_pat(other_user, name="other-pat")

        response = self.client.get(PAT_URL)
        self.assertEqual(len(response.data), 1)
        self.assertEqual(response.data[0]["name"], "test-pat")


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenAuthenticationTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        # Ensure a DRF Token exists so other auth code doesn't break
        Token.objects.get_or_create(user=self.user)

    def test_pat_authenticates_request(self):
        pat = _create_pat(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_expired_pat_rejected(self):
        pat = _create_pat(self.user, expires_at=timezone.now() - timedelta(seconds=1))
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_revoked_pat_rejected(self):
        pat = _create_pat(self.user, is_active=False)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_inactive_user_pat_rejected(self):
        pat = _create_pat(self.user)
        self.user.is_active = False
        self.user.save(update_fields=["is_active"])
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_all_failure_modes_return_same_message(self):
        """All failure modes should return 'Invalid token.' to avoid info leakage."""
        pat = _create_pat(self.user, is_active=False)

        # Revoked
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        resp = self.client.get("/api/customers/")
        self.assertEqual(resp.data["detail"], "Invalid token.")

        # Non-existent
        self.client.credentials(
            HTTP_AUTHORIZATION="Bearer w_9999999999_doesnotexist12345678901234567890"
        )
        resp = self.client.get("/api/customers/")
        self.assertEqual(resp.data["detail"], "Invalid token.")

    @override_config(PAT_ENABLED=False)
    def test_kill_switch_rejects_all_pats(self):
        pat = _create_pat(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/customers/")
        # With PAT disabled, request falls through to next auth (no auth → 401)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)


class PersonalAccessTokenScopeEnforcementTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.staff
        Token.objects.get_or_create(user=self.user)

    def test_staff_pat_scope_check_via_has_permission(self):
        """PAT scope ceiling applies even for staff through has_permission()."""
        from waldur_core.permissions.utils import has_permission

        self.assertTrue(self.user.is_staff)

        pat = _create_pat(
            self.user,
            scopes=[PermissionEnum.LIST_ORDERS.value],
        )

        # Build a fake request with PAT as auth
        from django.test import RequestFactory

        factory = RequestFactory()
        request = factory.get("/")
        request.user = self.user
        request.auth = pat

        # Permission that IS in PAT scopes → should pass for staff
        self.assertTrue(
            has_permission(request, PermissionEnum.LIST_ORDERS, self.fixture.customer)
        )

        # Permission NOT in PAT scopes → should be blocked despite staff status
        self.assertFalse(
            has_permission(
                request,
                PermissionEnum.CREATE_CUSTOMER_PERMISSION,
                self.fixture.customer,
            )
        )


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenSecurityTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        Token.objects.get_or_create(user=self.user)

    def test_pat_via_pat_blocked_create(self):
        """Cannot create a PAT using PAT authentication."""
        pat = _create_pat(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.post(
            PAT_URL,
            {
                "name": "Sneaky token",
                "scopes": [PermissionEnum.LIST_ORDERS.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pat_via_pat_blocked_destroy(self):
        """Cannot revoke a PAT using PAT authentication."""
        pat = _create_pat(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.delete(f"{PAT_URL}{pat.uuid}/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_pat_via_pat_blocked_rotate(self):
        """Cannot rotate a PAT using PAT authentication."""
        pat = _create_pat(self.user)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.post(f"{PAT_URL}{pat.uuid}/rotate/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    @override_config(PAT_MAX_LIFETIME_DAYS=30)
    def test_cannot_exceed_max_lifetime(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            PAT_URL,
            {
                "name": "Long token",
                "scopes": [PermissionEnum.LIST_ORDERS.value],
                "expires_at": (timezone.now() + timedelta(days=365)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(PAT_MAX_TOKENS_PER_USER=2)
    def test_cannot_exceed_max_tokens_per_user(self):
        self.client.force_authenticate(user=self.user)
        _create_pat(self.user, name="pat1")
        _create_pat(self.user, name="pat2")

        response = self.client.post(
            PAT_URL,
            {
                "name": "pat3",
                "scopes": [PermissionEnum.LIST_ORDERS.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_invalid_scopes_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            PAT_URL,
            {
                "name": "Bad scopes",
                "scopes": ["nonexistent_permission"],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_scopes_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            PAT_URL,
            {
                "name": "No scopes",
                "scopes": [],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_past_expires_at_rejected(self):
        self.client.force_authenticate(user=self.user)
        response = self.client.post(
            PAT_URL,
            {
                "name": "Expired",
                "scopes": [PermissionEnum.LIST_ORDERS.value],
                "expires_at": (timezone.now() - timedelta(days=1)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenUsageTrackingTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        Token.objects.get_or_create(user=self.user)

    def test_usage_stats_updated_on_use(self):
        pat = _create_pat(self.user)
        self.assertIsNone(pat.last_used_at)
        self.assertEqual(pat.use_count, 0)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        self.client.get("/api/customers/")

        pat.refresh_from_db()
        self.assertIsNotNone(pat.last_used_at)
        self.assertEqual(pat.use_count, 1)


class PersonalAccessTokenDeactivationCascadeTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner

    def test_user_deactivation_revokes_all_pats(self):
        _create_pat(self.user, name="pat1")
        _create_pat(self.user, name="pat2")
        self.assertEqual(
            PersonalAccessToken.objects.filter(user=self.user, is_active=True).count(),
            2,
        )

        self.user.is_active = False
        self.user.save()

        self.assertEqual(
            PersonalAccessToken.objects.filter(user=self.user, is_active=True).count(),
            0,
        )


class PersonalAccessTokenCleanupTaskTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner

    def test_cleanup_deactivates_expired_tokens(self):
        from waldur_core.core.tasks import cleanup_expired_personal_access_tokens

        _create_pat(
            self.user,
            name="expired",
            expires_at=timezone.now() - timedelta(hours=1),
        )
        _create_pat(
            self.user,
            name="active",
            expires_at=timezone.now() + timedelta(days=30),
        )

        cleanup_expired_personal_access_tokens()

        expired = PersonalAccessToken.objects.get(name="expired")
        active = PersonalAccessToken.objects.get(name="active")
        self.assertFalse(expired.is_active)
        self.assertTrue(active.is_active)


class PersonalAccessTokenModelTest(test.APITestCase):
    def test_generate_token_format(self):
        expires_at = timezone.now() + timedelta(days=30)
        full_token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
        self.assertTrue(full_token.startswith("w_"))
        # Verify embedded timestamp: w_<ts>_<random>
        parts = full_token.split("_", 2)
        self.assertEqual(len(parts), 3)
        self.assertEqual(int(parts[1]), int(expires_at.timestamp()))
        self.assertEqual(prefix, full_token[:8])
        self.assertEqual(token_hash, hashlib.sha256(full_token.encode()).hexdigest())
        self.assertEqual(len(token_hash), 64)

    def test_is_expired_property(self):
        user = User.objects.create_user(username="exptest", password="pass")
        pat = _create_pat(user, expires_at=timezone.now() - timedelta(seconds=1))
        self.assertTrue(pat.is_expired)

        pat2 = _create_pat(
            user,
            name="future",
            expires_at=timezone.now() + timedelta(days=1),
        )
        self.assertFalse(pat2.is_expired)


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenStaffScopeEnforcementTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.staff_user = self.fixture.staff
        self.staff_user.can_use_personal_access_tokens = True
        self.staff_user.save(update_fields=["can_use_personal_access_tokens"])
        self.regular_user = self.fixture.owner
        self.regular_user.can_use_personal_access_tokens = True
        self.regular_user.save(update_fields=["can_use_personal_access_tokens"])
        Token.objects.get_or_create(user=self.staff_user)
        Token.objects.get_or_create(user=self.regular_user)

    def test_staff_pat_without_staff_scope_cannot_access_admin_endpoint(self):
        pat = _create_pat(
            self.staff_user,
            scopes=[PermissionEnum.LIST_ORDERS.value],
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/override-settings/")
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_pat_with_staff_scope_can_access_admin_endpoint(self):
        pat = _create_pat(
            self.staff_user,
            scopes=[PermissionEnum.STAFF_ACCESS.value],
        )
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/override-settings/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_non_staff_user_cannot_request_staff_scope(self):
        self.client.force_authenticate(user=self.regular_user)
        response = self.client.post(
            PAT_URL,
            {
                "name": "Staff scope attempt",
                "scopes": [PermissionEnum.STAFF_ACCESS.value],
                "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
            },
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_session_auth_staff_not_affected(self):
        self.client.force_authenticate(user=self.staff_user)
        response = self.client.get("/api/override-settings/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class BannedPermissionClassUsageTest(test.APITestCase):
    """Ensure DRF's IsAdminUser is never used directly in production code.

    All staff-gated views must use PATScopeAwareIsAdminUser instead, so that
    PAT scope enforcement is not bypassed.
    """

    # Attribute name that must not be referenced in production code.
    BANNED_ATTR = "IsAdminUser"

    # Modules whose direct import of IsAdminUser is banned.
    BANNED_IMPORT_MODULES = {"rest_framework.permissions"}

    # Import aliases that commonly bind rest_framework.permissions.
    # If a file does `from rest_framework import permissions as rf_permissions`,
    # then `rf_permissions.IsAdminUser` must also be caught.
    RF_PERMISSION_ALIASES = {"rf_permissions", "permissions"}

    # Directories that are allowed to reference banned classes.
    IGNORE_PATTERNS = {"tests", "migrations", "conftest"}

    @staticmethod
    def _src_root():
        return pathlib.Path(__file__).resolve().parents[3]  # …/src

    def _should_skip(self, path: pathlib.Path) -> bool:
        return any(part in self.IGNORE_PATTERNS for part in path.parts)

    def _collect_rf_permission_aliases(self, tree):
        """Return the set of local names that bind rest_framework.permissions."""
        aliases = set()
        for node in ast.walk(tree):
            # `from rest_framework import permissions as rf_permissions`
            if isinstance(node, ast.ImportFrom) and node.module == "rest_framework":
                for alias in node.names:
                    if alias.name == "permissions":
                        aliases.add(alias.asname or alias.name)
            # `import rest_framework.permissions as rfp`
            if isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "rest_framework.permissions":
                        aliases.add(alias.asname or alias.name)
        return aliases

    def test_no_direct_usage_of_drf_is_admin_user(self):
        """Scan all non-test Python files under src/ for banned references."""
        violations = []
        src = self._src_root()

        for py_file in src.rglob("*.py"):
            if self._should_skip(py_file):
                continue
            try:
                source = py_file.read_text()
                tree = ast.parse(source, filename=str(py_file))
            except SyntaxError:
                continue

            rel = py_file.relative_to(src)

            # 1. Direct import: `from rest_framework.permissions import IsAdminUser`
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.ImportFrom)
                    and node.module in self.BANNED_IMPORT_MODULES
                ):
                    for alias in node.names:
                        if alias.name == self.BANNED_ATTR:
                            violations.append(
                                f"{rel}:{node.lineno} — "
                                f"from {node.module} import {self.BANNED_ATTR}"
                            )

            # 2. Attribute access: `rf_permissions.IsAdminUser`
            rf_aliases = self._collect_rf_permission_aliases(tree)
            for node in ast.walk(tree):
                if (
                    isinstance(node, ast.Attribute)
                    and node.attr == self.BANNED_ATTR
                    and isinstance(node.value, ast.Name)
                    and node.value.id in rf_aliases
                ):
                    violations.append(
                        f"{rel}:{node.lineno} — {node.value.id}.{self.BANNED_ATTR}"
                    )

        self.assertEqual(
            violations,
            [],
            "Found direct usage of DRF's IsAdminUser in production code. "
            "Use PATScopeAwareIsAdminUser from waldur_core.core.permissions instead.\n"
            + "\n".join(violations),
        )


class PersonalAccessTokenAvailableScopesTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user=self.user)

    def test_available_scopes_returns_permissions(self):
        response = self.client.get(f"{PAT_URL}available_scopes/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertTrue(len(response.data) > 0)
        self.assertIn("permission", response.data[0])
        self.assertIn("description", response.data[0])


# ---------------------------------------------------------------------------
# Entity-binding tests (`allowed_scopes` on PersonalAccessToken)
# ---------------------------------------------------------------------------


def _binding(entity):
    """Build a stored binding dict for a given entity."""
    from django.contrib.contenttypes.models import ContentType

    ct = ContentType.objects.get_for_model(type(entity))
    return {"content_type_id": ct.id, "object_id": entity.id}


def _fake_request(user, pat=None):
    """A minimal request object suitable for permission-check helpers."""
    from django.test import RequestFactory

    request = RequestFactory().get("/")
    request.user = user
    request.auth = pat
    return request


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenBindingCreateTest(test.APITestCase):
    """Validate the create-flow rules for `allowed_scopes`.

    The positive cases authenticate as a staff user so the per-binding
    permission check (which depends on the role-permission map loaded from
    `permissions.yaml`, not loaded in the test settings) is bypassed —
    we're testing the binding-shape validation in isolation. The negative
    cases use a non-staff user so the privilege-escalation guard fires.
    """

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.staff = self.fixture.staff
        self.staff.can_use_personal_access_tokens = True
        self.staff.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user=self.staff)

    def _payload(self, **overrides):
        payload = {
            "name": "binding-test",
            "scopes": [PermissionEnum.UPDATE_CUSTOMER.value],
            "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_create_with_single_customer_binding(self):
        response = self.client.post(
            PAT_URL,
            self._payload(
                allowed_scopes=[
                    {"type": "customer", "uuid": str(self.fixture.customer.uuid)}
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertEqual(len(pat.allowed_scopes), 1)
        self.assertEqual(
            pat.allowed_scopes[0],
            _binding(self.fixture.customer),
        )

    def test_create_with_mixed_type_bindings(self):
        response = self.client.post(
            PAT_URL,
            self._payload(
                allowed_scopes=[
                    {"type": "customer", "uuid": str(self.fixture.customer.uuid)},
                    {"type": "project", "uuid": str(self.fixture.project.uuid)},
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED, response.data)
        pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertEqual(len(pat.allowed_scopes), 2)

    def test_reject_binding_to_entity_user_lacks_permission_on(self):
        # `member` only holds PROJECT.MEMBER → no customer-level permission.
        self.fixture.member.can_use_personal_access_tokens = True
        self.fixture.member.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user=self.fixture.member)
        response = self.client.post(
            PAT_URL,
            self._payload(
                allowed_scopes=[
                    {"type": "customer", "uuid": str(self.fixture.customer.uuid)}
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_scopes", response.data)

    def test_reject_binding_to_unknown_uuid(self):
        import uuid as uuid_mod

        response = self.client.post(
            PAT_URL,
            self._payload(
                allowed_scopes=[{"type": "customer", "uuid": str(uuid_mod.uuid4())}],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reject_unknown_type_key(self):
        response = self.client.post(
            PAT_URL,
            self._payload(
                allowed_scopes=[
                    {"type": "not_a_real_type", "uuid": str(self.fixture.customer.uuid)}
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_staff_access_with_bindings_rejected(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            PAT_URL,
            self._payload(
                scopes=[PermissionEnum.STAFF_ACCESS.value],
                allowed_scopes=[
                    {"type": "customer", "uuid": str(self.fixture.customer.uuid)}
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("allowed_scopes", response.data)

    def test_support_access_with_bindings_rejected(self):
        self.fixture.staff.is_support = True
        self.fixture.staff.save()
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.post(
            PAT_URL,
            self._payload(
                scopes=[PermissionEnum.SUPPORT_ACCESS.value],
                allowed_scopes=[
                    {"type": "customer", "uuid": str(self.fixture.customer.uuid)}
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_empty_allowed_scopes_is_legacy_behaviour(self):
        response = self.client.post(
            PAT_URL,
            self._payload(allowed_scopes=[]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertEqual(pat.allowed_scopes, [])

    def test_response_includes_resolved_bindings(self):
        response = self.client.post(
            PAT_URL,
            self._payload(
                allowed_scopes=[
                    {"type": "customer", "uuid": str(self.fixture.customer.uuid)}
                ],
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        bindings = response.data["allowed_scopes"]
        self.assertEqual(len(bindings), 1)
        self.assertEqual(bindings[0]["type"], "customer")
        # DRF UUIDField serializes to string in the response.
        self.assertEqual(str(bindings[0]["uuid"]), str(self.fixture.customer.uuid))


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenBindingEnforcementTest(test.APITestCase):
    """Validate `has_permission` against PAT entity bindings.

    Uses a staff user so the underlying user-role check passes
    unconditionally — the PAT binding layer is the only thing under test.
    """

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.staff = self.fixture.staff

    def test_descendant_allowed(self):
        """PAT bound to a Customer authorises actions on Projects under it."""
        from waldur_core.permissions.utils import has_permission

        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.UPDATE_PROJECT.value],
        )
        pat.allowed_scopes = [_binding(self.fixture.customer)]
        pat.save(update_fields=["allowed_scopes"])

        request = _fake_request(self.staff, pat)
        self.assertTrue(
            has_permission(request, PermissionEnum.UPDATE_PROJECT, self.fixture.project)
        )

    def test_sibling_customer_denied(self):
        """PAT bound to Customer X denies actions on Customer Y."""
        from waldur_core.permissions.utils import has_permission
        from waldur_core.structure.tests import factories as structure_factories

        other_customer = structure_factories.CustomerFactory()

        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.UPDATE_CUSTOMER.value],
        )
        pat.allowed_scopes = [_binding(self.fixture.customer)]
        pat.save(update_fields=["allowed_scopes"])

        request = _fake_request(self.staff, pat)
        self.assertFalse(
            has_permission(request, PermissionEnum.UPDATE_CUSTOMER, other_customer)
        )

    def test_inverse_denied_child_binding_does_not_authorise_parent(self):
        """PAT bound to a Project must NOT authorise actions on its Customer."""
        from waldur_core.permissions.utils import has_permission

        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.UPDATE_CUSTOMER.value],
        )
        pat.allowed_scopes = [_binding(self.fixture.project)]
        pat.save(update_fields=["allowed_scopes"])

        request = _fake_request(self.staff, pat)
        self.assertFalse(
            has_permission(
                request, PermissionEnum.UPDATE_CUSTOMER, self.fixture.customer
            )
        )

    def test_scope_none_denied_for_scoped_pat(self):
        """A scoped PAT cannot perform scope-less (global) checks."""
        from waldur_core.permissions.utils import has_permission

        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.UPDATE_CUSTOMER.value],
        )
        pat.allowed_scopes = [_binding(self.fixture.customer)]
        pat.save(update_fields=["allowed_scopes"])

        request = _fake_request(self.staff, pat)
        self.assertFalse(has_permission(request, PermissionEnum.UPDATE_CUSTOMER, None))

    def test_scope_none_allowed_for_unscoped_pat(self):
        """Legacy (empty bindings) PAT does not short-circuit on scope=None."""
        from waldur_core.permissions.utils import has_permission

        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.UPDATE_CUSTOMER.value],
        )
        # No bindings → legacy behaviour: PAT layer does not block; staff
        # bypass applies normally.

        request = _fake_request(self.staff, pat)
        self.assertTrue(
            has_permission(
                request, PermissionEnum.UPDATE_CUSTOMER, self.fixture.customer
            )
        )

    def test_scoped_pat_restricts_staff_bypass(self):
        """Even a staff user's PAT is restricted to its bindings."""
        from waldur_core.permissions.utils import has_permission
        from waldur_core.structure.tests import factories as structure_factories

        other_customer = structure_factories.CustomerFactory()

        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.UPDATE_CUSTOMER.value],
        )
        pat.allowed_scopes = [_binding(self.fixture.customer)]
        pat.save(update_fields=["allowed_scopes"])

        request = _fake_request(self.staff, pat)
        # Bound customer — allowed.
        self.assertTrue(
            has_permission(
                request, PermissionEnum.UPDATE_CUSTOMER, self.fixture.customer
            )
        )
        # Other customer — denied despite is_staff.
        self.assertFalse(
            has_permission(request, PermissionEnum.UPDATE_CUSTOMER, other_customer)
        )

    def test_has_any_permission_applies_binding(self):
        """has_any_permission respects entity bindings."""
        from waldur_core.permissions.utils import has_any_permission
        from waldur_core.structure.tests import factories as structure_factories

        other_customer = structure_factories.CustomerFactory()

        pat = _create_pat(
            self.staff,
            scopes=[
                PermissionEnum.UPDATE_CUSTOMER.value,
                PermissionEnum.UPDATE_CUSTOMER_PERMISSION.value,
            ],
        )
        pat.allowed_scopes = [_binding(self.fixture.customer)]
        pat.save(update_fields=["allowed_scopes"])

        request = _fake_request(self.staff, pat)
        self.assertTrue(
            has_any_permission(
                request,
                [
                    PermissionEnum.UPDATE_CUSTOMER,
                    PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
                ],
                self.fixture.customer,
            )
        )
        self.assertFalse(
            has_any_permission(
                request,
                [
                    PermissionEnum.UPDATE_CUSTOMER,
                    PermissionEnum.UPDATE_CUSTOMER_PERMISSION,
                ],
                other_customer,
            )
        )

    def test_dead_binding_does_not_break_other_bindings(self):
        """Deleting one bound entity leaves the other bindings working."""
        from waldur_core.permissions.utils import has_permission
        from waldur_core.structure.tests import factories as structure_factories

        extra_customer = structure_factories.CustomerFactory()

        pat = _create_pat(
            self.staff,
            scopes=[PermissionEnum.UPDATE_CUSTOMER.value],
        )
        pat.allowed_scopes = [
            _binding(self.fixture.customer),
            _binding(extra_customer),
        ]
        pat.save(update_fields=["allowed_scopes"])

        extra_customer.delete()

        request = _fake_request(self.staff, pat)
        self.assertTrue(
            has_permission(
                request, PermissionEnum.UPDATE_CUSTOMER, self.fixture.customer
            )
        )


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenRotatePreservesBindingsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        Token.objects.get_or_create(user=self.user)
        self.client.force_authenticate(user=self.user)

    def test_rotate_preserves_allowed_scopes(self):
        pat = _create_pat(
            self.user,
            scopes=[PermissionEnum.UPDATE_CUSTOMER.value],
        )
        pat.allowed_scopes = [_binding(self.fixture.customer)]
        pat.save(update_fields=["allowed_scopes"])

        response = self.client.post(f"{PAT_URL}{pat.uuid}/rotate/")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

        new_pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertEqual(new_pat.allowed_scopes, pat.allowed_scopes)


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenAvailableBindingTargetsTest(test.APITestCase):
    def setUp(self):
        self.fixture = fixtures.ProjectFixture()

    def test_owner_sees_customer_targets(self):
        # The CustomerFixture's `owner` cached_property attaches LIST_PROJECTS
        # (among others) to the CUSTOMER.OWNER role; UPDATE_CUSTOMER is NOT
        # attached in the fixture, so we assert on LIST_PROJECTS instead.
        self.client.force_authenticate(user=self.fixture.owner)
        response = self.client.get(f"{PAT_URL}available_binding_targets/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        by_perm = {row["permission"]: row["types"] for row in response.data}
        self.assertIn(PermissionEnum.LIST_PROJECTS.value, by_perm)
        self.assertIn("customer", by_perm[PermissionEnum.LIST_PROJECTS.value])

    def test_staff_sees_all_types(self):
        self.client.force_authenticate(user=self.fixture.staff)
        response = self.client.get(f"{PAT_URL}available_binding_targets/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Staff response covers all non-global permissions.
        by_perm = {row["permission"]: row["types"] for row in response.data}
        self.assertIn(PermissionEnum.UPDATE_CUSTOMER.value, by_perm)
        self.assertIn("project", by_perm[PermissionEnum.UPDATE_CUSTOMER.value])
        # STAFF/SUPPORT not advertised (they can't be bound).
        self.assertNotIn(PermissionEnum.STAFF_ACCESS.value, by_perm)


@override_config(PAT_ENABLED=True)
class PersonalAccessTokenPerUserGateTest(test.APITestCase):
    """Per-user enablement: only users with the flag may create/use PATs."""

    def setUp(self):
        self.fixture = fixtures.CustomerFixture()

    def _create_payload(self):
        return {
            "name": "My token",
            "scopes": [PermissionEnum.LIST_ORDERS.value],
            "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
        }

    def test_user_without_flag_cannot_create(self):
        user = self.fixture.owner
        self.assertFalse(user.can_use_personal_access_tokens)
        self.client.force_authenticate(user=user)
        response = self.client.post(PAT_URL, self._create_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_user_with_flag_can_create(self):
        user = self.fixture.owner
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user=user)
        response = self.client.post(PAT_URL, self._create_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_staff_cannot_create_without_flag(self):
        staff = self.fixture.staff
        self.assertFalse(staff.can_use_personal_access_tokens)
        self.client.force_authenticate(user=staff)
        response = self.client.post(PAT_URL, self._create_payload(), format="json")
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_revoking_flag_disables_existing_token(self):
        user = self.fixture.owner
        user.can_use_personal_access_tokens = True
        user.save(update_fields=["can_use_personal_access_tokens"])
        pat = _create_pat(user)

        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

        user.can_use_personal_access_tokens = False
        user.save(update_fields=["can_use_personal_access_tokens"])
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_staff_token_rejected_without_flag(self):
        staff = self.fixture.staff
        pat = _create_pat(staff)
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}")
        response = self.client.get("/api/customers/")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
