"""
Tests for user data access visibility endpoint.
"""

from constance.test.unittest import override_config
from rest_framework import status, test

from waldur_core.permissions.fixtures import CustomerRole, ProjectRole
from waldur_core.structure.tests import factories, fixtures


class UserDataAccessPermissionTest(test.APITestCase):
    """Test permission checks for data access endpoint."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.owner = self.fixture.owner
        self.other_user = factories.UserFactory()

    def _get_data_access_url(self, user):
        return factories.UserFactory.get_url(user, action="data_access")

    def test_anonymous_user_cannot_access_data_access_endpoint(self):
        response = self.client.get(self._get_data_access_url(self.owner))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_see_own_data_access(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(self._get_data_access_url(self.owner))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_user_cannot_see_other_user_data_access(self):
        """
        When user tries to access data of a user not visible to them,
        they get 404 (not 403) to prevent information disclosure.
        """
        self.client.force_authenticate(self.other_user)
        response = self.client.get(self._get_data_access_url(self.owner))
        # Returns 404 (not 403) because the user can't even see the target user
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_can_see_any_user_data_access(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self._get_data_access_url(self.owner))
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_can_see_any_user_data_access(self):
        self.client.force_authenticate(self.support)
        response = self.client.get(self._get_data_access_url(self.owner))
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class UserDataAccessAdministrativeTest(test.APITestCase):
    """Test administrative access section of data access endpoint."""

    def setUp(self):
        self.regular_user = factories.UserFactory()
        self.staff1 = factories.UserFactory(is_staff=True)
        self.staff2 = factories.UserFactory(is_staff=True)
        self.support1 = factories.UserFactory(is_support=True)

    def _get_data_access_url(self, user):
        return factories.UserFactory.get_url(user, action="data_access")

    def test_regular_user_sees_description_only(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self._get_data_access_url(self.regular_user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin_access = response.data["administrative_access"]

        # Regular user should only see description
        self.assertIn("description", admin_access)

        # Regular user should NOT see counts or individual users
        self.assertNotIn("staff_count", admin_access)
        self.assertNotIn("support_count", admin_access)
        self.assertNotIn("users", admin_access)

    def test_staff_sees_individual_admin_users(self):
        self.client.force_authenticate(self.staff1)
        response = self.client.get(self._get_data_access_url(self.regular_user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin_access = response.data["administrative_access"]

        # Staff should see counts
        self.assertIn("staff_count", admin_access)
        self.assertIn("support_count", admin_access)
        self.assertIn("description", admin_access)

        # Staff should see individual users
        self.assertIn("users", admin_access)
        self.assertIsInstance(admin_access["users"], list)
        self.assertGreater(len(admin_access["users"]), 0)

        # Check user structure
        user_data = admin_access["users"][0]
        self.assertIn("user_uuid", user_data)
        self.assertIn("username", user_data)
        self.assertIn("full_name", user_data)
        self.assertIn("access_type", user_data)

    def test_support_sees_individual_admin_users(self):
        self.client.force_authenticate(self.support1)
        response = self.client.get(self._get_data_access_url(self.regular_user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        admin_access = response.data["administrative_access"]
        self.assertIn("users", admin_access)


class UserDataAccessOrganizationalTest(test.APITestCase):
    """Test organizational access section of data access endpoint."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.owner = self.fixture.owner
        self.customer = self.fixture.customer
        self.project = self.fixture.project

        # Add another user to the same customer
        self.customer_user = factories.UserFactory()
        self.customer.add_user(self.customer_user, CustomerRole.SUPPORT)

        # Add another user to the same project
        self.project_user = factories.UserFactory()
        self.project.add_user(self.project_user, ProjectRole.MEMBER)

    def _get_data_access_url(self, user):
        return factories.UserFactory.get_url(user, action="data_access")

    def test_organizational_access_shows_individual_users(self):
        """All users should see individual organizational members (peer visibility)."""
        self.client.force_authenticate(self.owner)
        response = self.client.get(self._get_data_access_url(self.owner))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        org_access = response.data["organizational_access"]

        # Should have at least one scope
        self.assertIsInstance(org_access, list)
        self.assertGreater(len(org_access), 0)

        # Find customer scope
        customer_scopes = [s for s in org_access if s["scope_type"] == "customer"]
        self.assertGreater(len(customer_scopes), 0)

        customer_scope = customer_scopes[0]
        self.assertEqual(customer_scope["scope_uuid"], str(self.customer.uuid))
        self.assertIn("users", customer_scope)
        self.assertIsInstance(customer_scope["users"], list)

    def test_organizational_access_shows_customer_users(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(self._get_data_access_url(self.owner))

        org_access = response.data["organizational_access"]
        customer_scopes = [s for s in org_access if s["scope_type"] == "customer"]

        # Check if customer_user appears in the customer scope
        customer_scope = customer_scopes[0]
        user_uuids = [u["user_uuid"] for u in customer_scope["users"]]
        self.assertIn(str(self.customer_user.uuid), user_uuids)

    def test_organizational_access_shows_project_users(self):
        self.client.force_authenticate(self.owner)
        response = self.client.get(self._get_data_access_url(self.owner))

        org_access = response.data["organizational_access"]
        project_scopes = [s for s in org_access if s["scope_type"] == "project"]

        # Check if project_user appears in the project scope
        if project_scopes:
            project_scope = project_scopes[0]
            user_uuids = [u["user_uuid"] for u in project_scope["users"]]
            self.assertIn(str(self.project_user.uuid), user_uuids)

    def test_organizational_access_excludes_target_user(self):
        """The target user should not appear in their own organizational access list."""
        self.client.force_authenticate(self.owner)
        response = self.client.get(self._get_data_access_url(self.owner))

        org_access = response.data["organizational_access"]

        # Collect all user UUIDs from all scopes
        all_user_uuids = []
        for scope in org_access:
            all_user_uuids.extend([u["user_uuid"] for u in scope["users"]])

        # Target user should not appear in the list
        self.assertNotIn(str(self.owner.uuid), all_user_uuids)


class UserDataAccessSummaryTest(test.APITestCase):
    """Test summary section of data access endpoint."""

    def setUp(self):
        self.fixture = fixtures.ProjectFixture()
        self.owner = self.fixture.owner
        self.staff = factories.UserFactory(is_staff=True)

    def _get_data_access_url(self, user):
        return factories.UserFactory.get_url(user, action="data_access")

    def test_regular_user_summary_hides_admin_count(self):
        """Regular users should not see total_administrative_access count."""
        self.client.force_authenticate(self.owner)
        response = self.client.get(self._get_data_access_url(self.owner))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        summary = response.data["summary"]

        self.assertIn("total_administrative_access", summary)
        self.assertIn("total_organizational_access", summary)
        self.assertIn("total_provider_access", summary)

        # Admin count should be null for regular users
        self.assertIsNone(summary["total_administrative_access"])
        # Other counts should be non-negative integers
        self.assertGreaterEqual(summary["total_organizational_access"], 0)
        self.assertGreaterEqual(summary["total_provider_access"], 0)

    def test_staff_summary_includes_all_counts(self):
        """Staff users should see all counts including total_administrative_access."""
        self.client.force_authenticate(self.staff)
        response = self.client.get(self._get_data_access_url(self.owner))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        summary = response.data["summary"]

        # All counts should be non-negative integers for staff
        self.assertIsNotNone(summary["total_administrative_access"])
        self.assertGreaterEqual(summary["total_administrative_access"], 0)
        self.assertGreaterEqual(summary["total_organizational_access"], 0)
        self.assertGreaterEqual(summary["total_provider_access"], 0)


class UserDataAccessServiceProviderTest(test.APITestCase):
    """Test service provider access section of data access endpoint."""

    def setUp(self):
        self.user = factories.UserFactory()

    def _get_data_access_url(self, user):
        return factories.UserFactory.get_url(user, action="data_access")

    def test_service_provider_access_is_empty_without_consents(self):
        """User without consents should have empty service provider access."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self._get_data_access_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        provider_access = response.data["service_provider_access"]

        self.assertIsInstance(provider_access, list)
        self.assertEqual(len(provider_access), 0)


class GetClientIpTest(test.APITestCase):
    """Test IP address extraction and validation."""

    def test_valid_ipv4_is_returned(self):
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["REMOTE_ADDR"] = "192.168.1.1"
        request.META.pop("HTTP_X_FORWARDED_FOR", None)
        self.assertEqual(get_client_ip(request), "192.168.1.1")

    def test_valid_ipv6_is_returned(self):
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "::1"
        self.assertEqual(get_client_ip(request), "::1")

    def test_hostname_in_x_forwarded_for_returns_none(self):
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "spoofed.example.com"
        self.assertIsNone(get_client_ip(request))

    def test_garbage_value_returns_none(self):
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "not-an-ip-at-all"
        self.assertIsNone(get_client_ip(request))

    def test_none_request_returns_none(self):
        from waldur_core.structure.utils_data_access import get_client_ip

        self.assertIsNone(get_client_ip(None))

    def test_x_forwarded_for_takes_first_ip(self):
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "10.0.0.1, 192.168.1.1"
        self.assertEqual(get_client_ip(request), "10.0.0.1")

    def test_ipv6_is_canonicalised(self):
        """One client must not reach the audit log under two spellings.

        ``ip_address`` is queried to answer "every access from X"; a raw
        mixed-case value and its canonical form are different strings, so the
        same accessor would split across rows the query cannot join.
        """
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "2001:DB8::1"
        self.assertEqual(get_client_ip(request), "2001:db8::1")

    def test_ipv4_mapped_ipv6_is_unwrapped(self):
        """A dual-stack listener reports IPv4 clients as ``::ffff:10.0.0.1``."""
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "::ffff:10.0.0.1"
        self.assertEqual(get_client_ip(request), "10.0.0.1")

    def test_host_port_suffix_is_stripped(self):
        """Azure Application Gateway writes ``host:port`` into X-Forwarded-For.

        Rejecting it outright loses the accessor's address from the GDPR audit
        record entirely, which is worse than recording the host part.
        """
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "10.0.0.1:5678"
        self.assertEqual(get_client_ip(request), "10.0.0.1")

    def test_unparseable_address_is_logged(self):
        """Dropping the address silently hides a misconfigured proxy."""
        from waldur_core.structure.utils_data_access import get_client_ip

        request = self.client.get("/").wsgi_request
        request.META["HTTP_X_FORWARDED_FOR"] = "not-an-ip-at-all"
        with self.assertLogs(
            "waldur_core.structure.utils_data_access", level="WARNING"
        ) as cm:
            self.assertIsNone(get_client_ip(request))
        self.assertIn("not-an-ip-at-all", "".join(cm.output))


class LogUserDataAccessTransactionSafetyTest(test.APITestCase):
    """Test that data access logging does not corrupt the outer DB transaction."""

    def setUp(self):
        self.user = factories.UserFactory()
        self.staff = factories.UserFactory(is_staff=True)

    @override_config(USER_DATA_ACCESS_LOGGING_ENABLED=True)
    def test_spoofed_ip_does_not_cause_500_on_user_me(self):
        """Spoofed X-Forwarded-For with a hostname should not crash /api/users/me/."""
        self.client.force_authenticate(self.staff)
        response = self.client.get(
            "/api/users/me/",
            HTTP_X_FORWARDED_FOR="spoofed.example.com",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    @override_config(USER_DATA_ACCESS_LOGGING_ENABLED=True)
    def test_spoofed_ip_logs_none_ip_address(self):
        """When IP is invalid, log entry should be created with ip_address=None."""
        from waldur_core.logging.models import UserDataAccessLog

        self.client.force_authenticate(self.staff)
        self.client.get(
            factories.UserFactory.get_url(self.user),
            HTTP_X_FORWARDED_FOR="spoofed.example.com",
        )
        log_entry = UserDataAccessLog.objects.filter(
            target_user=self.user,
            accessor=self.staff,
        ).first()
        self.assertIsNotNone(log_entry)
        self.assertIsNone(log_entry.ip_address)


class UserDataAccessLoggingTest(test.APITestCase):
    """Test user data access logging functionality."""

    def setUp(self):
        self.user = factories.UserFactory()
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)

    def _get_user_url(self, user):
        return factories.UserFactory.get_url(user)

    @override_config(USER_DATA_ACCESS_LOGGING_ENABLED=True)
    def test_accessing_user_creates_log_entry(self):
        """Accessing a user via API should create a log entry with only personal data fields."""
        from waldur_core.logging.models import UserDataAccessLog
        from waldur_core.structure.serializers import UserSerializer

        self.client.force_authenticate(self.staff)
        response = self.client.get(self._get_user_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check log entry was created
        log_entry = UserDataAccessLog.objects.filter(
            target_user=self.user,
            accessor=self.staff,
        ).first()

        self.assertIsNotNone(log_entry)
        self.assertEqual(log_entry.accessor_type, "staff")
        self.assertGreater(len(log_entry.accessed_fields), 0)

        # Verify only personal data fields are logged, not technical fields
        technical_fields = {
            "url",
            "uuid",
            "slug",
            "token",
            "token_lifetime",
            "token_expires_at",
            "is_staff",
            "is_active",
            "is_support",
            "permissions",
            "has_active_session",
        }
        for field in log_entry.accessed_fields:
            self.assertIn(
                field,
                UserSerializer.PERSONAL_DATA_FIELDS,
                f"Field '{field}' should not be logged - not personal data",
            )
            self.assertNotIn(
                field,
                technical_fields,
                f"Technical field '{field}' should not be logged",
            )

    @override_config(
        USER_DATA_ACCESS_LOGGING_ENABLED=True,
        USER_DATA_ACCESS_LOG_SELF_ACCESS=False,
    )
    def test_self_access_not_logged_by_default(self):
        """Self-access should not be logged when USER_DATA_ACCESS_LOG_SELF_ACCESS is False."""
        from waldur_core.logging.models import UserDataAccessLog

        self.client.force_authenticate(self.user)
        response = self.client.get(self._get_user_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check no log entry was created for self-access
        log_count = UserDataAccessLog.objects.filter(
            target_user=self.user,
            accessor=self.user,
        ).count()

        self.assertEqual(log_count, 0)

    @override_config(USER_DATA_ACCESS_LOGGING_ENABLED=False)
    def test_logging_disabled_creates_no_entries(self):
        """When logging is disabled, no log entries should be created."""
        from waldur_core.logging.models import UserDataAccessLog

        self.client.force_authenticate(self.staff)
        response = self.client.get(self._get_user_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Check no log entry was created
        log_count = UserDataAccessLog.objects.filter(
            target_user=self.user,
            accessor=self.staff,
        ).count()

        self.assertEqual(log_count, 0)


class UserDataAccessHistoryTest(test.APITestCase):
    """Test user data access history endpoint."""

    def setUp(self):
        self.user = factories.UserFactory()
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.other_user = factories.UserFactory()

        # Create some log entries
        from waldur_core.logging.models import UserDataAccessLog

        self.log_entry = UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.staff,
            accessor_type="staff",
            accessed_fields=["username", "email", "full_name"],
            ip_address="192.168.1.100",
            context={"endpoint": "/api/users/"},
        )

    def _get_history_url(self, user):
        return factories.UserFactory.get_url(user, action="data_access_history")

    def test_anonymous_cannot_access_history(self):
        response = self.client.get(self._get_history_url(self.user))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_user_can_see_own_history(self):
        self.client.force_authenticate(self.user)
        response = self.client.get(self._get_history_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreater(len(response.data), 0)

    def test_user_cannot_see_other_user_history(self):
        """Other users cannot see a user's access history."""
        self.client.force_authenticate(self.other_user)
        response = self.client.get(self._get_history_url(self.user))

        # Returns 404 because the user can't see the target user
        self.assertEqual(response.status_code, status.HTTP_404_NOT_FOUND)

    def test_staff_can_see_any_user_history(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self._get_history_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_can_see_any_user_history(self):
        self.client.force_authenticate(self.support)
        response = self.client.get(self._get_history_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_regular_user_sees_anonymized_accessor(self):
        """Regular users should see accessor_category, not accessor details."""
        self.client.force_authenticate(self.user)
        response = self.client.get(self._get_history_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        log_data = response.data[0]
        self.assertIn("accessor_category", log_data)
        self.assertEqual(log_data["accessor_category"], "Platform administrator")
        self.assertNotIn("accessor", log_data)
        self.assertNotIn("ip_address", log_data)

    def test_staff_sees_full_accessor_details(self):
        """Staff should see full accessor details including identity and IP."""
        self.client.force_authenticate(self.staff)
        response = self.client.get(self._get_history_url(self.user))

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        log_data = response.data[0]
        self.assertIn("accessor", log_data)
        self.assertIn("ip_address", log_data)
        self.assertIn("context", log_data)

        # Check accessor details
        self.assertEqual(log_data["accessor"]["uuid"], str(self.staff.uuid))
        self.assertEqual(log_data["accessor"]["username"], self.staff.username)

    def test_history_filter_by_accessor_type(self):
        """Test filtering history by accessor type."""
        self.client.force_authenticate(self.staff)

        # Create another log entry with different accessor type
        from waldur_core.logging.models import UserDataAccessLog

        UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.support,
            accessor_type="support",
            accessed_fields=["username"],
        )

        response = self.client.get(
            self._get_history_url(self.user), {"accessor_type": "staff"}
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only show staff entries
        for entry in response.data:
            self.assertEqual(entry["accessor_type"], "staff")


class GlobalDataAccessLogsPermissionTest(test.APITestCase):
    """Test permission checks for global data access logs endpoint."""

    def setUp(self):
        self.url = "/api/data-access-logs/"
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.regular_user = factories.UserFactory()

    def test_anonymous_cannot_access(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_access(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_access(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_support_can_access(self):
        self.client.force_authenticate(self.support)
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, status.HTTP_200_OK)


class GlobalDataAccessLogsListTest(test.APITestCase):
    """Test list functionality for global data access logs endpoint."""

    def setUp(self):
        from waldur_core.logging.models import UserDataAccessLog

        self.url = "/api/data-access-logs/"
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.user1 = factories.UserFactory()
        self.user2 = factories.UserFactory()

        # Create log entries
        self.log1 = UserDataAccessLog.objects.create(
            target_user=self.user1,
            accessor=self.staff,
            accessor_type="staff",
            accessed_fields=["username", "email"],
            ip_address="192.168.1.100",
            context={"endpoint": "/api/users/"},
        )
        self.log2 = UserDataAccessLog.objects.create(
            target_user=self.user2,
            accessor=self.support,
            accessor_type="support",
            accessed_fields=["full_name"],
            ip_address="10.0.0.1",
        )

    def test_list_returns_all_logs(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertGreaterEqual(len(response.data), 2)

    def test_response_includes_target_user_details(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Find the log entry for user1
        log_data = next(
            (log for log in response.data if log["uuid"] == str(self.log1.uuid)), None
        )
        self.assertIsNotNone(log_data)

        # Check user field is present and correct
        self.assertIn("user", log_data)
        self.assertEqual(log_data["user"]["uuid"], str(self.user1.uuid))
        self.assertEqual(log_data["user"]["username"], self.user1.username)
        self.assertIn("full_name", log_data["user"])

    def test_response_includes_accessor_details(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)

        log_data = next(
            (log for log in response.data if log["uuid"] == str(self.log1.uuid)), None
        )

        self.assertIn("accessor", log_data)
        self.assertEqual(log_data["accessor"]["uuid"], str(self.staff.uuid))
        self.assertEqual(log_data["accessor"]["username"], self.staff.username)

    def test_response_includes_all_fields(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)

        log_data = next(
            (log for log in response.data if log["uuid"] == str(self.log1.uuid)), None
        )

        # Check all expected fields are present
        self.assertIn("uuid", log_data)
        self.assertIn("timestamp", log_data)
        self.assertIn("accessor_type", log_data)
        self.assertIn("accessed_fields", log_data)
        self.assertIn("user", log_data)
        self.assertIn("accessor", log_data)
        self.assertIn("ip_address", log_data)
        self.assertIn("context", log_data)


class GlobalDataAccessLogsFilterTest(test.APITestCase):
    """Test filtering for global data access logs endpoint."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from waldur_core.logging.models import UserDataAccessLog

        self.url = "/api/data-access-logs/"
        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.user1 = factories.UserFactory(
            username="alice_test", first_name="Alice", last_name="Smith"
        )
        self.user2 = factories.UserFactory(
            username="bob_test", first_name="Bob", last_name="Jones"
        )

        # Create log entries with different dates
        self.old_log = UserDataAccessLog.objects.create(
            target_user=self.user1,
            accessor=self.staff,
            accessor_type="staff",
            accessed_fields=["username"],
        )
        # Manually update timestamp for old log
        self.old_log.timestamp = timezone.now() - timedelta(days=10)
        self.old_log.save()

        self.recent_log = UserDataAccessLog.objects.create(
            target_user=self.user2,
            accessor=self.support,
            accessor_type="support",
            accessed_fields=["email"],
        )

    def test_filter_by_user_uuid(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"user_uuid": str(self.user1.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for log in response.data:
            self.assertEqual(log["user"]["uuid"], str(self.user1.uuid))

    def test_filter_by_accessor_uuid(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"accessor_uuid": str(self.staff.uuid)})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for log in response.data:
            self.assertEqual(log["accessor"]["uuid"], str(self.staff.uuid))

    def test_filter_by_accessor_type(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"accessor_type": "support"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        for log in response.data:
            self.assertEqual(log["accessor_type"], "support")

    def test_filter_by_date_range(self):
        from datetime import timedelta

        from django.utils import timezone

        self.client.force_authenticate(self.staff)

        # Filter for recent logs only (last 5 days)
        start_date = (timezone.now() - timedelta(days=5)).date()
        response = self.client.get(self.url, {"start_date": start_date.isoformat()})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should only include the recent log, not the old one
        log_uuids = [log["uuid"] for log in response.data]
        self.assertIn(str(self.recent_log.uuid), log_uuids)
        self.assertNotIn(str(self.old_log.uuid), log_uuids)

    def test_filter_by_query(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"query": "alice"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Should find the log for alice (user1)
        log_uuids = [log["uuid"] for log in response.data]
        self.assertIn(str(self.old_log.uuid), log_uuids)


class GlobalDataAccessLogsOrderingTest(test.APITestCase):
    """Test ordering for global data access logs endpoint."""

    def setUp(self):
        from datetime import timedelta

        from django.utils import timezone

        from waldur_core.logging.models import UserDataAccessLog

        self.url = "/api/data-access-logs/"
        self.staff = factories.UserFactory(is_staff=True)
        self.user = factories.UserFactory()

        # Create logs with different timestamps
        self.log1 = UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.staff,
            accessor_type="staff",
            accessed_fields=["username"],
        )
        self.log1.timestamp = timezone.now() - timedelta(hours=2)
        self.log1.save()

        self.log2 = UserDataAccessLog.objects.create(
            target_user=self.user,
            accessor=self.staff,
            accessor_type="staff",
            accessed_fields=["email"],
        )
        self.log2.timestamp = timezone.now() - timedelta(hours=1)
        self.log2.save()

    def test_default_ordering_is_timestamp_descending(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url)

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Most recent should be first
        log_uuids = [log["uuid"] for log in response.data]
        self.assertEqual(log_uuids.index(str(self.log2.uuid)), 0)

    def test_ordering_by_timestamp_ascending(self):
        self.client.force_authenticate(self.staff)
        response = self.client.get(self.url, {"o": "timestamp"})

        self.assertEqual(response.status_code, status.HTTP_200_OK)

        # Oldest should be first
        log_uuids = [log["uuid"] for log in response.data]
        idx1 = log_uuids.index(str(self.log1.uuid))
        idx2 = log_uuids.index(str(self.log2.uuid))
        self.assertLess(idx1, idx2)


class GlobalDataAccessLogsDeleteTest(test.APITestCase):
    """Test delete functionality for global data access logs endpoint."""

    def setUp(self):
        from waldur_core.logging.models import UserDataAccessLog

        self.staff = factories.UserFactory(is_staff=True)
        self.support = factories.UserFactory(is_support=True)
        self.regular_user = factories.UserFactory()
        self.target_user = factories.UserFactory()

        self.log_entry = UserDataAccessLog.objects.create(
            target_user=self.target_user,
            accessor=self.staff,
            accessor_type="staff",
            accessed_fields=["username", "email"],
            ip_address="192.168.1.100",
        )

    def _get_detail_url(self, log_entry):
        return f"/api/data-access-logs/{log_entry.uuid}/"

    def test_anonymous_cannot_delete(self):
        response = self.client.delete(self._get_detail_url(self.log_entry))
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_regular_user_cannot_delete(self):
        self.client.force_authenticate(self.regular_user)
        response = self.client.delete(self._get_detail_url(self.log_entry))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_support_cannot_delete(self):
        """Support users can view logs but cannot delete them."""
        self.client.force_authenticate(self.support)
        response = self.client.delete(self._get_detail_url(self.log_entry))
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)

    def test_staff_can_delete(self):
        """Staff users can delete log entries."""
        from waldur_core.logging.models import UserDataAccessLog

        self.client.force_authenticate(self.staff)
        response = self.client.delete(self._get_detail_url(self.log_entry))

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.assertFalse(
            UserDataAccessLog.objects.filter(uuid=self.log_entry.uuid).exists()
        )
