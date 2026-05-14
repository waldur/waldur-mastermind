"""
Regression test for the fix to Critical security finding #10.

`QueryViewSet` (`/api/query/`, `/api/stats/query/`) executes caller-supplied
SQL on a read replica. Before the fix, the permission gate required
`is_support` — a broader role than `is_staff` — so any compromised support
user could exfiltrate the entire database (token hashes, password hashes,
PII) at API speed even with a SELECT-only DB role.

This test confirms that:
  * a non-staff `is_support` user is now rejected with 403, and
  * a staff user still reaches the executor (proving we tightened, not broke).
"""

from rest_framework import status
from rest_framework.test import APITestCase

from waldur_core.structure.tests import factories as structure_factories


class QueryEndpointStaffOnlyTest(APITestCase):
    def test_support_only_user_is_rejected(self):
        support_user = structure_factories.UserFactory(is_support=True, is_staff=False)
        self.client.force_authenticate(support_user)

        response = self.client.post("/api/query/", {}, format="json")

        self.assertEqual(
            response.status_code,
            status.HTTP_403_FORBIDDEN,
            f"is_support user should be rejected by the permission gate; "
            f"got {response.status_code}: {response.content!r}",
        )

    def test_staff_user_still_reaches_view(self):
        staff = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(staff)

        # Empty body — view returns 400 ("Query parameter is required")
        # which proves the permission gate let us through.
        response = self.client.post("/api/query/", {}, format="json")
        self.assertEqual(
            response.status_code,
            status.HTTP_400_BAD_REQUEST,
            f"Staff was unexpectedly blocked; got {response.status_code}: "
            f"{response.content!r}",
        )
