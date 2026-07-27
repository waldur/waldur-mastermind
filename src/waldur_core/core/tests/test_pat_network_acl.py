from datetime import timedelta

from constance.test import override_config
from django.core.cache import cache
from django.utils import timezone
from rest_framework import status, test

from waldur_core.core.models import PersonalAccessToken, User
from waldur_core.logging.models import Event
from waldur_core.permissions.enums import PermissionEnum
from waldur_core.structure.tests import fixtures

# The endpoint the existing PAT auth suite authenticates against — a PAT with
# the default LIST_ORDERS scope gets 200 here (models with no registered PAT
# filtering rule pass through unfiltered).
PROBE_URL = "/api/customers/"
PAT_URL = "/api/personal-access-tokens/"


def create_pat(user, allowed_networks=None, name="acl-pat"):
    expires_at = timezone.now() + timedelta(days=30)
    full_token, prefix, token_hash = PersonalAccessToken.generate_token(expires_at)
    pat = PersonalAccessToken.objects.create(
        user=user,
        name=name,
        token_prefix=prefix,
        token_hash=token_hash,
        scopes=[PermissionEnum.LIST_ORDERS.value],
        allowed_networks=allowed_networks or [],
        expires_at=expires_at,
    )
    pat._plaintext_token = full_token
    return pat


@override_config(PAT_ENABLED=True)
class PatNetworkAclEnforcementTest(test.APITestCase):
    def setUp(self):
        # Both the usage-stats and the denial-audit debounces live in the
        # cache; without a clear they leak across tests in the same process.
        cache.clear()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])

    def _get(self, pat, remote_addr="203.0.113.5", xff=None):
        extra = {"REMOTE_ADDR": remote_addr}
        if xff is not None:
            extra["HTTP_X_FORWARDED_FOR"] = xff
        return self.client.get(
            PROBE_URL,
            HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}",
            **extra,
        )

    def test_empty_acl_allows_any_source(self):
        pat = create_pat(self.user)
        self.assertEqual(self._get(pat).status_code, status.HTTP_200_OK)

    def test_ip_inside_acl_is_allowed(self):
        pat = create_pat(self.user, ["203.0.113.0/24"])
        self.assertEqual(self._get(pat).status_code, status.HTTP_200_OK)

    def test_ip_outside_acl_is_denied(self):
        pat = create_pat(self.user, ["198.51.100.0/24"])
        response = self._get(pat)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_denial_message_does_not_leak_that_token_is_valid(self):
        pat = create_pat(self.user, ["198.51.100.0/24"])
        response = self._get(pat)
        self.assertEqual(str(response.data["detail"]), "Invalid token.")

    def test_ipv6_acl_match(self):
        pat = create_pat(self.user, ["2001:db8::/32"])
        self.assertEqual(
            self._get(pat, remote_addr="2001:db8::1").status_code, status.HTTP_200_OK
        )

    def test_unresolvable_ip_fails_closed(self):
        pat = create_pat(self.user, ["203.0.113.0/24"])
        response = self._get(pat, remote_addr="not-an-ip")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_forwarded_header_supplies_the_client_ip(self):
        # The ingress overwrites X-Forwarded-For with the real client address,
        # so its first entry is authoritative and the ACL is checked against it
        # even when REMOTE_ADDR is the proxy's own in-cluster address.
        pat = create_pat(self.user, ["203.0.113.0/24"])
        response = self._get(pat, remote_addr="10.0.0.1", xff="203.0.113.5")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_forwarded_header_outside_acl_is_denied(self):
        pat = create_pat(self.user, ["198.51.100.0/24"])
        response = self._get(pat, remote_addr="10.0.0.1", xff="203.0.113.5")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_braced_forwarded_header_is_rejected_not_500(self):
        # get_ip_address returns the raw X-Forwarded-For; a value that looks
        # like a format placeholder must never reach the event-message
        # .format() template. Normalisation renders it unresolvable, so the ACL
        # check fails closed with a clean 401 instead of a 500 that would tell
        # the caller the token is valid.
        pat = create_pat(self.user, ["203.0.113.0/24"])
        response = self._get(pat, remote_addr="10.0.0.1", xff="{oops}")
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_braced_token_name_still_denies_with_401(self):
        """A name with braces must not blow up the audit event into a 500.

        The event message is run through ``.format()``, so a user-controlled
        name inlined into the template would raise and leak — via the status
        code — that the token exists and is otherwise valid.
        """
        pat = create_pat(self.user, ["198.51.100.0/24"], name="evil{oops}name")
        response = self._get(pat)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(str(response.data["detail"]), "Invalid token.")

    def test_denial_emits_audit_event(self):
        pat = create_pat(self.user, ["198.51.100.0/24"])
        self._get(pat)
        events = Event.objects.filter(event_type="pat_access_denied_from_ip")
        self.assertEqual(events.count(), 1)
        event = events.get()
        self.assertEqual(event.context["pat_uuid"], pat.uuid.hex)
        self.assertEqual(event.context["source_ip"], "203.0.113.5")

    def test_denial_does_not_record_usage(self):
        """The ACL check must run before usage stats are written.

        Otherwise a rejected request lets the caller write attacker-controlled
        state onto a token they were just denied.
        """
        pat = create_pat(self.user, ["198.51.100.0/24"])
        self._get(pat)
        pat.refresh_from_db()
        self.assertEqual(pat.use_count, 0)
        self.assertIsNone(pat.last_used_ip)

    def test_denial_does_not_emit_new_ip_event(self):
        """A denial must not fire — nor burn the debounce key of — the new-IP alert.

        The token has already been used from an in-ACL address, so a usage
        update from this off-network address would look like a new-IP event
        and would set the 10-minute ``pat_usage`` key that suppresses the
        owner's next genuine one.
        """
        pat = create_pat(self.user, ["198.51.100.0/24"])
        PersonalAccessToken.objects.filter(pk=pat.pk).update(
            last_used_ip="198.51.100.7"
        )
        self._get(pat)
        self.assertEqual(
            Event.objects.filter(event_type="pat_used_from_new_ip").count(), 0
        )

    def test_repeated_denial_from_same_ip_emits_one_event(self):
        """The audit event is debounced; the denial itself never is."""
        pat = create_pat(self.user, ["198.51.100.0/24"])
        first = self._get(pat)
        second = self._get(pat)
        self.assertEqual(first.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(second.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            Event.objects.filter(event_type="pat_access_denied_from_ip").count(), 1
        )

    def test_denial_from_a_different_ip_emits_its_own_event(self):
        pat = create_pat(self.user, ["198.51.100.0/24"])
        self._get(pat, remote_addr="203.0.113.5")
        self._get(pat, remote_addr="203.0.113.9")
        events = Event.objects.filter(event_type="pat_access_denied_from_ip")
        self.assertEqual(events.count(), 2)
        self.assertEqual(
            {event.context["source_ip"] for event in events},
            {"203.0.113.5", "203.0.113.9"},
        )


@override_config(PAT_ENABLED=True)
class PatKnownTokenRejectionAuditTest(test.APITestCase):
    """A known token presented after it should no longer work is audited.

    These are distinct from the ACL denial (correct-token, wrong-network):
    the token exists but is revoked, expired, or its owner is inactive / no
    longer permitted PATs. Each is a credential-replay signal.
    """

    def setUp(self):
        cache.clear()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])

    def _get(self, pat, remote_addr="203.0.113.5"):
        return self.client.get(
            PROBE_URL,
            HTTP_AUTHORIZATION=f"Bearer {pat._plaintext_token}",
            REMOTE_ADDR=remote_addr,
        )

    def _rejections(self):
        return Event.objects.filter(event_type="pat_authentication_rejected")

    def _revoke(self, pat):
        PersonalAccessToken.objects.filter(pk=pat.pk).update(is_active=False)

    def test_revoked_token_use_is_audited(self):
        pat = create_pat(self.user)
        self._revoke(pat)
        response = self._get(pat)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        event = self._rejections().get()
        self.assertEqual(event.context["reason"], "revoked")
        self.assertEqual(event.context["pat_uuid"], pat.uuid.hex)
        self.assertEqual(event.context["source_ip"], "203.0.113.5")

    def test_inactive_user_token_use_is_audited(self):
        # Deactivating a user via save() cascades to revoking their tokens (a
        # post_save handler), which the earlier is_active branch would then
        # report as "revoked". Bypass that signal with a direct UPDATE so the
        # user_inactive branch — the safety net for a token still active under a
        # deactivated owner — is exercised in isolation.
        pat = create_pat(self.user)
        User.objects.filter(pk=self.user.pk).update(is_active=False)
        response = self._get(pat)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self._rejections().get().context["reason"], "user_inactive")

    def test_permission_revoked_token_use_is_audited(self):
        pat = create_pat(self.user)
        self.user.can_use_personal_access_tokens = False
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        response = self._get(pat)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(
            self._rejections().get().context["reason"], "permission_revoked"
        )

    def test_unknown_token_is_not_audited(self):
        # No token record -> nothing to debounce on -> must not be audited,
        # or an attacker floods the event table with forged bearer tokens.
        response = self.client.get(
            PROBE_URL,
            HTTP_AUTHORIZATION="Bearer w_9999999999_bogus",
            REMOTE_ADDR="203.0.113.5",
        )
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self._rejections().count(), 0)

    def test_response_stays_generic(self):
        pat = create_pat(self.user)
        self._revoke(pat)
        self.assertEqual(str(self._get(pat).data["detail"]), "Invalid token.")

    def test_rejection_is_debounced_per_reason_and_ip(self):
        pat = create_pat(self.user)
        self._revoke(pat)
        self._get(pat)
        self._get(pat)
        self.assertEqual(self._rejections().count(), 1)

    def test_rejection_from_a_new_ip_emits_its_own_event(self):
        pat = create_pat(self.user)
        self._revoke(pat)
        self._get(pat, remote_addr="203.0.113.5")
        self._get(pat, remote_addr="203.0.113.9")
        self.assertEqual(self._rejections().count(), 2)

    def test_braced_token_name_does_not_500(self):
        pat = create_pat(self.user, name="evil{oops}name")
        self._revoke(pat)
        response = self._get(pat)
        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)
        self.assertEqual(self._rejections().get().context["pat_name"], "evil{oops}name")


@override_config(PAT_ENABLED=True)
class PatNewIpEventTest(test.APITestCase):
    def setUp(self):
        # The usage debounce lives in the cache and is exactly what these tests
        # exercise; without a clear it leaks across tests in the same process.
        cache.clear()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.pat = create_pat(self.user)

    def _get(self, remote_addr):
        return self.client.get(
            PROBE_URL,
            HTTP_AUTHORIZATION=f"Bearer {self.pat._plaintext_token}",
            REMOTE_ADDR=remote_addr,
        )

    def _new_ip_events(self):
        return Event.objects.filter(event_type="pat_used_from_new_ip")

    def test_new_ip_event_fires_inside_debounce_window(self):
        self._get("203.0.113.5")
        # Second request lands inside the 10-minute usage debounce — the window
        # a replayed stolen token would land in.
        self._get("198.51.100.9")
        self.assertEqual(self._new_ip_events().count(), 1)

    def test_same_ip_does_not_emit_new_ip_event(self):
        self._get("203.0.113.5")
        self._get("203.0.113.5")
        self.assertFalse(self._new_ip_events().exists())

    def test_ip_change_writes_through_the_debounce(self):
        """An IP change must update ``last_used_ip`` even mid-debounce.

        If the debounce swallowed the write, the stored IP would stay stale and
        every later request from the new address would look like another change.
        """
        self._get("203.0.113.5")
        self._get("198.51.100.9")
        self.pat.refresh_from_db()
        self.assertEqual(self.pat.last_used_ip, "198.51.100.9")

    def test_new_ip_event_does_not_re_fire_from_the_settled_ip(self):
        """Write-through is what settles the new IP, and the event proves it.

        The count alone no longer isolates write-through — the per-IP emit
        debounce would suppress a repeat from the same address anyway. The
        third address does isolate it: ``previous`` is read from the stored
        ``last_used_ip``, so a swallowed write reports the address from two
        hops back and the audit trail lies about where the token moved from.
        """
        self._get("203.0.113.5")
        self._get("198.51.100.9")
        self._get("198.51.100.9")
        self.assertEqual(self._new_ip_events().count(), 1)

        self._get("192.0.2.77")
        event = self._new_ip_events().get(context__source_ip="192.0.2.77")
        self.assertIn("previous: 198.51.100.9", event.message)

    def test_repeated_use_from_same_ip_is_debounced(self):
        """The gate exists to avoid a DB write per request — keep it that way."""
        self._get("203.0.113.5")
        self._get("203.0.113.5")
        self._get("203.0.113.5")
        self.pat.refresh_from_db()
        self.assertEqual(self.pat.use_count, 1)

    def test_alternating_ips_emit_one_event_per_ip(self):
        """The emit is debounced per (token, IP); the usage write is not.

        Write-through alone only covers a settled address. A token holder who
        alternates two source IPs makes every request look like a change, so
        without a per-IP emit debounce each one lands an Event, a Feed row and
        a notification task — unbounded amplification from a valid token.
        """
        self._get("203.0.113.5")
        for _ in range(3):
            self._get("198.51.100.9")
            self._get("203.0.113.5")
        self.assertEqual(
            self._new_ip_events().filter(context__source_ip="198.51.100.9").count(), 1
        )
        self.assertEqual(
            self._new_ip_events().filter(context__source_ip="203.0.113.5").count(), 1
        )

    def test_third_ip_still_emits_despite_earlier_suppression(self):
        """Suppression must never extend forward to an address not yet seen.

        This is what the IP in the cache key buys: drop it and the first new-IP
        event silences every later one for ten minutes.
        """
        self._get("203.0.113.5")
        self._get("198.51.100.9")
        self._get("203.0.113.5")
        self._get("192.0.2.77")
        self.assertEqual(
            self._new_ip_events().filter(context__source_ip="192.0.2.77").count(), 1
        )

    def test_braced_token_name_does_not_break_the_new_ip_event(self):
        """``emit()`` runs ``.format()`` over the template.

        A user-controlled name inlined into it raises ``KeyError`` and turns the
        owner's first legitimate use from a new address into an HTTP 500.
        """
        self.pat = create_pat(self.user, name="evil{oops}name")
        self._get("203.0.113.5")
        response = self._get("198.51.100.9")
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        event = self._new_ip_events().get()
        self.assertEqual(event.context["pat_name"], "evil{oops}name")
        self.assertIn("evil{oops}name", event.message)


@override_config(PAT_ENABLED=True)
class PatNetworkAclApiTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.client.force_authenticate(user=self.user)

    def _create_payload(self, **overrides):
        payload = {
            "name": "acl token",
            "scopes": [PermissionEnum.LIST_ORDERS.value],
            "expires_at": (timezone.now() + timedelta(days=30)).isoformat(),
        }
        payload.update(overrides)
        return payload

    def test_create_accepts_and_normalises_acl(self):
        response = self.client.post(
            PAT_URL,
            self._create_payload(allowed_networks=["203.0.113.5", "198.51.100.0/24"]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertEqual(pat.allowed_networks, ["203.0.113.5/32", "198.51.100.0/24"])

    def test_create_defaults_to_unrestricted(self):
        response = self.client.post(PAT_URL, self._create_payload(), format="json")
        pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertEqual(pat.allowed_networks, [])

    def test_create_rejects_invalid_acl(self):
        response = self.client.post(
            PAT_URL,
            self._create_payload(allowed_networks=["203.0.113.5/24"]),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(PAT_ENABLED=True, PAT_MAX_ACL_ENTRIES=2)
    def test_create_rejects_too_many_entries(self):
        response = self.client.post(
            PAT_URL,
            self._create_payload(
                allowed_networks=["10.1.0.0/16", "10.2.0.0/16", "10.3.0.0/16"]
            ),
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_create_with_braced_name_does_not_500(self):
        """A user-controlled name with braces must not blow up the audit emit.

        emit() runs .format() over the template; inlining the name would raise
        KeyError inside emit — an HTTP 500 after the token already exists.
        """
        response = self.client.post(
            PAT_URL, self._create_payload(name="evil{oops}name"), format="json"
        )
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)

    def test_list_exposes_acl(self):
        create_pat(self.user, ["203.0.113.0/24"])
        response = self.client.get(PAT_URL)
        self.assertEqual(response.data[0]["allowed_networks"], ["203.0.113.0/24"])

    def test_set_network_acl_updates_and_emits_event(self):
        pat = create_pat(self.user, ["203.0.113.0/24"])
        response = self.client.post(
            f"{PAT_URL}{pat.uuid}/set_network_acl/",
            {"allowed_networks": ["198.51.100.7"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pat.refresh_from_db()
        self.assertEqual(pat.allowed_networks, ["198.51.100.7/32"])
        event = Event.objects.filter(event_type="pat_network_acl_updated").first()
        self.assertIsNotNone(event)
        self.assertEqual(event.context["old_allowed_networks"], ["203.0.113.0/24"])
        self.assertEqual(event.context["new_allowed_networks"], ["198.51.100.7/32"])

    def test_set_network_acl_can_clear_the_acl(self):
        pat = create_pat(self.user, ["203.0.113.0/24"])
        response = self.client.post(
            f"{PAT_URL}{pat.uuid}/set_network_acl/",
            {"allowed_networks": []},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        pat.refresh_from_db()
        self.assertEqual(pat.allowed_networks, [])

    def test_set_network_acl_rejects_invalid_entries(self):
        pat = create_pat(self.user, [])
        response = self.client.post(
            f"{PAT_URL}{pat.uuid}/set_network_acl/",
            {"allowed_networks": ["0.0.0.0/0"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    @override_config(PAT_ENABLED=True, PAT_MAX_ACL_ENTRIES=2)
    def test_set_network_acl_rejects_too_many_entries(self):
        pat = create_pat(self.user, [])
        response = self.client.post(
            f"{PAT_URL}{pat.uuid}/set_network_acl/",
            {"allowed_networks": ["10.1.0.0/16", "10.2.0.0/16", "10.3.0.0/16"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_set_network_acl_with_braced_name_does_not_500(self):
        """The set_network_acl emit must survive a braced token name too."""
        pat = create_pat(self.user, ["203.0.113.0/24"], name="evil{oops}name")
        response = self.client.post(
            f"{PAT_URL}{pat.uuid}/set_network_acl/",
            {"allowed_networks": ["198.51.100.7"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_rotate_carries_the_acl_over(self):
        pat = create_pat(self.user, ["203.0.113.0/24"])
        response = self.client.post(f"{PAT_URL}{pat.uuid}/rotate/")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        new_pat = PersonalAccessToken.objects.get(uuid=response.data["uuid"])
        self.assertEqual(new_pat.allowed_networks, ["203.0.113.0/24"])

    def test_set_network_acl_rejects_an_inactive_token(self):
        """Mirrors rotate. Also how the rotate race surfaces: if rotate won the
        lock, the token this request holds is already revoked, and silently
        editing its ACL would leave the user believing they had tightened the
        live one.
        """
        pat = create_pat(self.user, [])
        pat.is_active = False
        pat.save(update_fields=["is_active"])
        response = self.client.post(
            f"{PAT_URL}{pat.uuid}/set_network_acl/",
            {"allowed_networks": ["203.0.113.0/24"]},
            format="json",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        pat.refresh_from_db()
        self.assertEqual(pat.allowed_networks, [])

    def test_rotate_after_set_network_acl_carries_the_new_acl(self):
        pat = create_pat(self.user, ["203.0.113.0/24"])
        self.client.post(
            f"{PAT_URL}{pat.uuid}/set_network_acl/",
            {"allowed_networks": ["198.51.100.0/24"]},
            format="json",
        )
        response = self.client.post(f"{PAT_URL}{pat.uuid}/rotate/")
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        self.assertEqual(response.data["allowed_networks"], ["198.51.100.0/24"])


@override_config(PAT_ENABLED=True)
class PatViaPatBlockedTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.pat = create_pat(self.user)

    def test_set_network_acl_cannot_be_called_with_a_pat(self):
        target = create_pat(self.user, name="target")
        response = self.client.post(
            f"{PAT_URL}{target.uuid}/set_network_acl/",
            {"allowed_networks": []},
            format="json",
            HTTP_AUTHORIZATION=f"Bearer {self.pat._plaintext_token}",
            REMOTE_ADDR="203.0.113.5",
        )
        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_config(PAT_ENABLED=True, PAT_MAX_AUDIT_EVENTS_PER_HOUR=3)
class PatEventBudgetTest(test.APITestCase):
    """A valid token must not be usable to grow the event table without bound.

    The per-(token, IP) debounce bounds repeats from one address but not the
    number of addresses — an IPv6 /64 supplies effectively unlimited ones.
    """

    def setUp(self):
        cache.clear()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.pat = create_pat(self.user)

    def _get(self, remote_addr):
        return self.client.get(
            PROBE_URL,
            HTTP_AUTHORIZATION=f"Bearer {self.pat._plaintext_token}",
            REMOTE_ADDR=remote_addr,
        )

    def _churn(self):
        """Settle on one address, then rotate through eight more."""
        self._get("203.0.113.1")
        for octet in range(2, 10):
            self._get(f"203.0.113.{octet}")

    def test_new_ip_events_stop_at_the_ceiling(self):
        self._churn()
        self.assertEqual(
            Event.objects.filter(event_type="pat_used_from_new_ip").count(), 3
        )

    def test_usage_write_stops_at_the_ceiling(self):
        """Over budget the usage UPDATE stops too — that is the DB-write bound."""
        self._churn()
        self.pat.refresh_from_db()
        # 1 first-use write + 3 budgeted churn write-throughs.
        self.assertEqual(self.pat.use_count, 4)
        self.assertEqual(self.pat.last_used_ip, "203.0.113.4")

    def test_requests_still_succeed_over_budget(self):
        """The ceiling suppresses auditing, never the request itself."""
        self._get("203.0.113.1")
        for octet in range(2, 10):
            response = self._get(f"203.0.113.{octet}")
        self.assertEqual(response.status_code, status.HTTP_200_OK)

    def test_ceiling_is_logged_once(self):
        self._get("203.0.113.1")
        with self.assertLogs("waldur_core.core.authentication", level="WARNING") as cm:
            for octet in range(2, 10):
                self._get(f"203.0.113.{octet}")
        hits = [line for line in cm.output if "hourly audit ceiling" in line]
        self.assertEqual(len(hits), 1)


@override_config(PAT_ENABLED=True, PAT_MAX_AUDIT_EVENTS_PER_HOUR=3)
class PatRejectionBudgetTest(test.APITestCase):
    def setUp(self):
        cache.clear()
        self.fixture = fixtures.CustomerFixture()
        self.user = self.fixture.owner
        self.user.can_use_personal_access_tokens = True
        self.user.save(update_fields=["can_use_personal_access_tokens"])
        self.pat = create_pat(self.user)
        self.pat.is_active = False
        self.pat.save(update_fields=["is_active"])

    def _get(self, remote_addr):
        return self.client.get(
            PROBE_URL,
            HTTP_AUTHORIZATION=f"Bearer {self.pat._plaintext_token}",
            REMOTE_ADDR=remote_addr,
        )

    def test_rejection_events_stop_at_the_ceiling(self):
        for octet in range(1, 10):
            self.assertEqual(
                self._get(f"203.0.113.{octet}").status_code,
                status.HTTP_401_UNAUTHORIZED,
            )
        self.assertEqual(
            Event.objects.filter(event_type="pat_authentication_rejected").count(), 3
        )

    def test_rejection_budget_is_separate_from_churn_budget(self):
        """A rejection flood must not starve the owner's new-IP audit trail."""
        for octet in range(1, 10):
            self._get(f"203.0.113.{octet}")

        self.pat.is_active = True
        self.pat.save(update_fields=["is_active"])
        self._get("198.51.100.1")
        self._get("198.51.100.2")

        self.assertEqual(
            Event.objects.filter(event_type="pat_used_from_new_ip").count(), 1
        )
