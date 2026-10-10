from datetime import timedelta
from unittest import mock

import httpx
import respx
from constance.test import override_config
from django.test import TestCase
from django.utils import timezone
from freezegun import freeze_time
from rest_framework import status, test

from waldur_core.logging.enums import EventType
from waldur_core.logging.models import Event
from waldur_core.structure.tests import factories as structure_factories
from waldur_mastermind.matrix_chat import (
    crypto_setup,
    matrix_client,
    models,
    recovery_keys,
    tasks,
    views,
)
from waldur_mastermind.matrix_chat.tests.test_recovery_keys import (
    KEY_INFO,
    OTHER_KEY_INFO,
    OTHER_RECOVERY_KEY,
    PUBLISHED_MASTER,
    RECOVERY_KEY,
    STORED_MASTER,
)

HOMESERVER = "https://matrix.example.com"
MATRIX = dict(
    MATRIX_ENABLED=True,
    MATRIX_HOMESERVER_URL=HOMESERVER,
    MATRIX_HOMESERVER_DOMAIN="matrix.example.com",
    MATRIX_APPSERVICE_AS_TOKEN="test-as-token",
)
MXID = "@alice:matrix.example.com"
MASTER_KEY = {"user_id": MXID, "usage": ["master"], "keys": {"ed25519:M": "M"}}
LEASE_URL = "/api/matrix/crypto/lease/"
RELEASE_URL = "/api/matrix/crypto/lease/release/"
ESCROW_URL = "/api/matrix/crypto/escrow/"
RECOVERY_KEY_URL = "/api/matrix/credentials/recovery-key/"


def _profile(**kwargs):
    return models.MatrixUserProfile.objects.create(
        user=structure_factories.UserFactory(),
        matrix_user_id=MXID,
        provisioned=True,
        **kwargs,
    )


@override_config(**MATRIX)
@mock.patch.object(matrix_client, "set_password")
@mock.patch.object(
    matrix_client, "get_secret_storage_state", return_value=(None, None, False)
)
@mock.patch.object(matrix_client, "get_cross_signing_master_key", return_value=None)
@mock.patch.object(matrix_client, "ensure_user_exists", return_value=MXID)
@mock.patch.object(tasks, "scrub_temporary_matrix_password")
class CryptoSetupViewTest(test.APITestCase):
    def setUp(self):
        self.profile = _profile()
        self.client.force_authenticate(self.profile.user)

    def _lease(self, kind="bootstrap"):
        return self.client.post(LEASE_URL, {"kind": kind})

    def _escrow(self, lease, key=RECOVERY_KEY):
        return self.client.post(ESCROW_URL, {"lease": lease, "recovery_key": key})

    def test_first_setup_leases_then_escrows(self, scrub, *mocks):
        response = self._lease()
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["temporary_password"])

        response = self._escrow(response.data["lease"])

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.recovery_key, RECOVERY_KEY)
        scrub.delay.assert_not_called()

    def test_escrow_keeps_the_lease_until_it_is_released(self, *mocks):
        lease = self._lease().data["lease"]
        self._escrow(lease)

        # The holder is still uploading the keys that go with the escrowed one.
        self.assertEqual(self._lease().data["state"], "in_progress")

        response = self.client.post(RELEASE_URL, {"lease": lease})

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.crypto_lease, "")

    def test_releasing_someone_elses_lease_does_nothing(self, *mocks):
        lease = self._lease().data["lease"]

        self.client.post(RELEASE_URL, {"lease": "not-the-lease"})

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.crypto_lease, lease)

    def test_reset_when_storage_lacks_the_master_key(
        self, scrub, ensure, master, key_info, set_password
    ):
        # A setup that died after escrow and before storing the cross-signing
        # keys: the key opens secret storage, which still unlocks nothing.
        self.profile.recovery_key = RECOVERY_KEY
        self.profile.save()
        master.return_value = MASTER_KEY
        key_info.return_value = ("K1", KEY_INFO, False)

        self.assertEqual(self._lease("reset").status_code, status.HTTP_200_OK)

    def test_reset_is_recorded_and_tracks_the_temporary_password(
        self, scrub, ensure, master, key_info, set_password
    ):
        master.return_value = MASTER_KEY

        response = self._lease("reset")

        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.profile.refresh_from_db()
        self.assertIsNotNone(self.profile.crypto_temporary_password_until)
        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MATRIX_ENCRYPTION_RESET_STARTED
            ).exists()
        )

    def test_escrow_is_recorded(self, *mocks):
        self._escrow(self._lease().data["lease"])

        self.assertTrue(
            Event.objects.filter(
                event_type=EventType.MATRIX_RECOVERY_KEY_ESCROWED
            ).exists()
        )

    def test_oversized_recovery_key_is_refused_before_decoding(self, *mocks):
        lease = self._lease().data["lease"]

        response = self._escrow(lease, key="2" * 100_000)

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_malformed_lease_is_refused(self, *mocks):
        response = self._escrow("ünïcode lease")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_release_scrubs_the_password_now(
        self, scrub, ensure, master, key_info, set_password
    ):
        master.return_value = MASTER_KEY
        lease = self._lease("reset").data["lease"]

        self.client.post(RELEASE_URL, {"lease": lease})

        scrub.delay.assert_called_once_with(MXID, lease)

    def test_oidc_access_token_is_refused(self, *mocks):
        # May belong to another client of the identity provider.
        with mock.patch.object(views, "get_auth_method", return_value="oidc"):
            self.assertEqual(self._lease().status_code, status.HTTP_403_FORBIDDEN)

    def test_personal_access_token_is_refused(self, *mocks):
        with mock.patch.object(views, "get_auth_method", return_value="pat"):
            self.assertEqual(self._lease().status_code, status.HTTP_403_FORBIDDEN)
            self.assertEqual(
                self._escrow("lease").status_code, status.HTTP_403_FORBIDDEN
            )

    def test_impersonation_is_refused(self, *mocks):
        self.profile.user.impersonator = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.profile.user)

        self.assertEqual(self._lease().status_code, status.HTTP_403_FORBIDDEN)

    def test_a_second_window_waits_for_the_lease(self, *mocks):
        self._lease()

        response = self._lease()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["state"], "in_progress")
        self.assertIn("Retry-After", response.headers)

    def test_an_expired_lease_can_be_taken_over(self, *mocks):
        first = self._lease().data["lease"]
        with freeze_time(
            timezone.now() + crypto_setup.LEASE_TTL + timedelta(seconds=1)
        ):
            second = self._lease()
            self.assertEqual(second.status_code, status.HTTP_200_OK)
            self.assertEqual(self._escrow(first).data["state"], "no_lease")
            self.assertEqual(
                self._escrow(second.data["lease"]).status_code,
                status.HTTP_204_NO_CONTENT,
            )

    def test_only_the_lease_holder_can_escrow(self, *mocks):
        self._lease()

        response = self._escrow("not-the-lease")

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["state"], "no_lease")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.recovery_key, "")

    def test_set_up_account_gets_no_bootstrap_lease(
        self, scrub, ensure, master, key_info, set_password
    ):
        self.profile.recovery_key = RECOVERY_KEY
        self.profile.save()
        master.return_value = MASTER_KEY

        response = self._lease()

        self.assertEqual(response.data["state"], "set_up")

    def test_stale_key_without_an_identity_is_set_up_again(self, *mocks):
        # E.g. after moving to a new homeserver: the old key opens nothing there.
        self.profile.recovery_key = RECOVERY_KEY
        self.profile.save()
        other = "EsSz ykH7 LCZx 7Cae cmKD wcmY JRXi Ybtu 8iQ3 t8Ez nRwK pUY1"

        response = self._escrow(self._lease().data["lease"], key=other)

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)

    def test_identity_without_escrowed_key_is_reported_locked(
        self, scrub, ensure, master, key_info, set_password
    ):
        master.return_value = MASTER_KEY

        response = self._lease()

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["state"], "locked")

    def test_malformed_recovery_key_is_refused(self, *mocks):
        lease = self._lease().data["lease"]

        response = self._escrow(lease, key="not a recovery key")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)

    def test_reset_of_locked_identity(
        self, scrub, ensure, master, key_info, set_password
    ):
        master.return_value = MASTER_KEY

        response = self._lease("reset")

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        password = response.data["temporary_password"]
        self.assertTrue(password)
        set_password.assert_called_once_with(MXID, password)
        scrub.apply_async.assert_called_once()
        self.assertEqual(
            scrub.apply_async.call_args.kwargs["countdown"],
            int(crypto_setup.LEASE_TTL.total_seconds()),
        )

        response = self._escrow(response.data["lease"])

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        scrub.delay.assert_called_once()
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.recovery_key, RECOVERY_KEY)

    def test_reset_replaces_a_key_that_no_longer_opens_secret_storage(
        self, scrub, ensure, master, key_info, set_password
    ):
        self.profile.recovery_key = RECOVERY_KEY
        self.profile.save()
        master.return_value = MASTER_KEY
        key_info.return_value = ("K1", OTHER_KEY_INFO, True)

        self.assertEqual(self._lease("reset").status_code, status.HTTP_200_OK)

    def test_reset_refused_while_the_escrowed_key_works(
        self, scrub, ensure, master, key_info, set_password
    ):
        self.profile.recovery_key = RECOVERY_KEY
        self.profile.save()
        master.return_value = MASTER_KEY
        key_info.return_value = ("K1", KEY_INFO, True)

        response = self._lease("reset")

        self.assertEqual(response.data["state"], "not_locked")
        set_password.assert_not_called()

    def test_reset_refused_without_an_identity(self, scrub, ensure, master, *mocks):
        response = self._lease("reset")

        self.assertEqual(response.data["state"], "not_locked")

    def test_failed_password_set_releases_the_lease(
        self, scrub, ensure, master, key_info, set_password
    ):
        master.return_value = MASTER_KEY
        set_password.side_effect = matrix_client.MatrixAdminRequired("not admin")

        response = self._lease("reset")

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.crypto_lease, "")
        scrub.apply_async.assert_not_called()

    def test_disabled_chat_is_not_found(self, *mocks):
        with override_config(MATRIX_ENABLED=False):
            self.assertEqual(self._lease().status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_is_rejected(self, *mocks):
        self.client.force_authenticate(None)
        self.assertEqual(self._lease().status_code, status.HTTP_401_UNAUTHORIZED)

    def test_bootstrap_escrow_is_not_checked_against_the_homeserver(
        self, scrub, ensure, master, key_info, set_password
    ):
        # Secret storage does not exist yet when a first setup escrows its key.
        with mock.patch.object(matrix_client, "get_stored_master_key") as stored:
            self._escrow(self._lease().data["lease"])

        stored.assert_not_called()


@override_config(**MATRIX)
@mock.patch.object(
    matrix_client,
    "get_stored_master_key",
    return_value=("K1", KEY_INFO, STORED_MASTER),
)
@mock.patch.object(
    matrix_client, "get_secret_storage_state", return_value=(None, None, False)
)
@mock.patch.object(
    matrix_client, "get_cross_signing_master_key", return_value=PUBLISHED_MASTER
)
@mock.patch.object(matrix_client, "ensure_user_exists", return_value=MXID)
@mock.patch.object(matrix_client, "set_password")
@mock.patch.object(tasks, "scrub_temporary_matrix_password")
class ImportRecoveryKeyTest(test.APITestCase):
    """A key set up or reset in another client, which Waldur never saw."""

    def setUp(self):
        self.profile = _profile()
        self.client.force_authenticate(self.profile.user)

    def _import(self, key=RECOVERY_KEY):
        response = self.client.post(LEASE_URL, {"kind": "import"})
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        lease = response.data["lease"]
        return lease, self.client.post(
            ESCROW_URL, {"lease": lease, "recovery_key": key}
        )

    def test_key_from_another_client_is_escrowed(self, scrub, set_password, *mocks):
        lease, response = self._import()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.recovery_key, RECOVERY_KEY)
        set_password.assert_not_called()
        scrub.delay.assert_not_called()
        scrub.apply_async.assert_not_called()

    def test_the_lease_ends_with_the_escrow(self, *mocks):
        # An import uploads nothing after it, so nothing needs the lease.
        self._import()

        self.profile.refresh_from_db()
        self.assertEqual(self.profile.crypto_lease, "")
        self.assertEqual(self.profile.crypto_lease_kind, "")
        self.assertIsNone(self.profile.crypto_lease_expires_at)

    def test_is_recorded_as_an_import(self, *mocks):
        self._import()

        event = Event.objects.get(event_type=EventType.MATRIX_RECOVERY_KEY_ESCROWED)
        self.assertIn("another Matrix client", event.message)
        self.assertNotIn(RECOVERY_KEY, event.message)

    def test_key_from_a_reset_in_another_client_replaces_the_stale_one(self, *mocks):
        self.profile.recovery_key = OTHER_RECOVERY_KEY
        self.profile.save()

        _, response = self._import()

        self.assertEqual(response.status_code, status.HTTP_204_NO_CONTENT)
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.recovery_key, RECOVERY_KEY)

    def test_a_key_that_does_not_open_storage_is_refused(self, *mocks):
        lease, response = self._import(key=OTHER_RECOVERY_KEY)

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["state"], "wrong_key")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.recovery_key, "")
        # The holder may try another key under the same lease.
        self.assertEqual(self.profile.crypto_lease, lease)

    def test_storage_of_an_older_identity_is_refused(
        self, scrub, set_password, ensure, master, *mocks
    ):
        # The key opens secret storage, but the master key stored there is not
        # the one the homeserver publishes now.
        master.return_value = {"keys": {"ed25519:" + "A" * 43: "A" * 43}}

        _, response = self._import()

        self.assertEqual(response.data["state"], "wrong_key")

    def test_storage_without_the_master_key_is_refused(
        self, scrub, set_password, ensure, master, state, stored
    ):
        stored.return_value = ("K1", KEY_INFO, None)

        _, response = self._import()

        self.assertEqual(response.data["state"], "wrong_key")

    def test_an_identity_gone_since_the_lease_is_not_a_wrong_key(
        self, scrub, set_password, ensure, master, *mocks
    ):
        lease = self.client.post(LEASE_URL, {"kind": "import"}).data["lease"]
        master.return_value = None

        response = self.client.post(
            ESCROW_URL, {"lease": lease, "recovery_key": RECOVERY_KEY}
        )

        self.assertEqual(response.status_code, status.HTTP_409_CONFLICT)
        self.assertEqual(response.data["state"], "not_locked")
        self.profile.refresh_from_db()
        self.assertEqual(self.profile.recovery_key, "")

    def test_refused_while_the_escrowed_key_works(
        self, scrub, set_password, ensure, master, state, stored
    ):
        self.profile.recovery_key = RECOVERY_KEY
        self.profile.save()
        state.return_value = ("K1", KEY_INFO, True)

        response = self.client.post(LEASE_URL, {"kind": "import"})

        self.assertEqual(response.data["state"], "not_locked")

    def test_refused_without_an_identity(
        self, scrub, set_password, ensure, master, *mocks
    ):
        master.return_value = None

        response = self.client.post(LEASE_URL, {"kind": "import"})

        self.assertEqual(response.data["state"], "not_locked")

    def test_impersonation_is_refused(self, *mocks):
        self.profile.user.impersonator = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.profile.user)

        response = self.client.post(LEASE_URL, {"kind": "import"})

        self.assertEqual(response.status_code, status.HTTP_403_FORBIDDEN)


@override_config(**MATRIX)
@mock.patch.object(matrix_client, "get_secret_storage_state")
class RecoveryKeyViewTest(test.APITestCase):
    def setUp(self):
        self.profile = _profile(recovery_key=RECOVERY_KEY)
        self.client.force_authenticate(self.profile.user)

    def _show(self):
        return self.client.post(RECOVERY_KEY_URL)

    def test_owner_gets_a_key_that_unlocks(self, key_info):
        key_info.return_value = ("K1", KEY_INFO, True)

        response = self._show()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response.data["recovery_key"], RECOVERY_KEY)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        key_info.assert_called_once_with(MXID)

    def test_viewing_is_recorded_without_the_key(self, key_info):
        key_info.return_value = ("K1", KEY_INFO, True)

        self._show()

        event = Event.objects.get(event_type=EventType.MATRIX_RECOVERY_KEY_VIEWED)
        self.assertNotIn(RECOVERY_KEY, event.message)
        self.assertNotIn(RECOVERY_KEY, str(event.context))

    def test_stale_key_is_not_shown(self, key_info):
        # The user reset encryption in Element; the old key opens nothing.
        key_info.return_value = ("K1", OTHER_KEY_INFO, True)

        response = self._show()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["recovery_key"])
        self.assertFalse(
            Event.objects.filter(
                event_type=EventType.MATRIX_RECOVERY_KEY_VIEWED
            ).exists()
        )

    def test_no_key_before_encryption_is_set_up(self, key_info):
        self.profile.recovery_key = ""
        self.profile.save()

        response = self._show()

        self.assertIsNone(response.data["recovery_key"])
        key_info.assert_not_called()

    def test_user_without_a_matrix_profile_gets_no_key(self, key_info):
        self.client.force_authenticate(structure_factories.UserFactory())

        response = self._show()

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertIsNone(response.data["recovery_key"])

    def test_unreachable_homeserver(self, key_info):
        key_info.side_effect = matrix_client.MatrixClientError("down")

        response = self._show()

        self.assertEqual(response.status_code, status.HTTP_503_SERVICE_UNAVAILABLE)
        self.assertNotIn(RECOVERY_KEY, str(response.data))

    def test_impersonation_is_refused(self, key_info):
        self.profile.user.impersonator = structure_factories.UserFactory(is_staff=True)
        self.client.force_authenticate(self.profile.user)

        self.assertEqual(self._show().status_code, status.HTTP_403_FORBIDDEN)
        key_info.assert_not_called()

    def test_personal_access_token_is_refused(self, key_info):
        with mock.patch.object(views, "get_auth_method", return_value="pat"):
            self.assertEqual(self._show().status_code, status.HTTP_403_FORBIDDEN)

    def test_oidc_access_token_is_refused(self, key_info):
        with mock.patch.object(views, "get_auth_method", return_value="oidc"):
            self.assertEqual(self._show().status_code, status.HTTP_403_FORBIDDEN)

    def test_refusal_does_not_use_up_the_rate_limit(self, key_info):
        with (
            mock.patch.object(
                views.ScopedRateThrottle, "allow_request", return_value=True
            ) as allow,
            mock.patch.object(views, "get_auth_method", return_value="pat"),
        ):
            self.assertEqual(self._show().status_code, status.HTTP_403_FORBIDDEN)

        allow.assert_not_called()

    def test_read_only_is_not_allowed(self, key_info):
        self.assertEqual(
            self.client.get(RECOVERY_KEY_URL).status_code,
            status.HTTP_405_METHOD_NOT_ALLOWED,
        )

    def test_disabled_chat_is_not_found(self, key_info):
        with override_config(MATRIX_ENABLED=False):
            self.assertEqual(self._show().status_code, status.HTTP_404_NOT_FOUND)

    def test_anonymous_is_rejected(self, key_info):
        self.client.force_authenticate(None)
        self.assertEqual(self._show().status_code, status.HTTP_401_UNAUTHORIZED)


@override_config(**MATRIX)
@mock.patch.object(matrix_client, "set_password")
class ScrubTemporaryPasswordTest(TestCase):
    def test_replaces_the_password(self, set_password):
        _profile()

        tasks.scrub_temporary_matrix_password(MXID, "lease-1")

        set_password.assert_called_once()
        self.assertEqual(set_password.call_args.args[0], MXID)

    def test_a_newer_bootstrap_lease_does_not_keep_the_password(self, set_password):
        _profile(
            crypto_lease="lease-2",
            crypto_lease_kind=models.CryptoLeaseKinds.BOOTSTRAP,
            crypto_lease_expires_at=timezone.now() + timedelta(minutes=5),
            crypto_temporary_password_until=timezone.now(),
        )

        tasks.scrub_temporary_matrix_password(MXID, "lease-1")

        set_password.assert_called_once()
        profile = models.MatrixUserProfile.objects.get(matrix_user_id=MXID)
        self.assertIsNone(profile.crypto_temporary_password_until)

    def test_sweep_scrubs_passwords_left_behind(self, set_password):
        _profile(crypto_temporary_password_until=timezone.now() - timedelta(minutes=1))

        with mock.patch.object(tasks.scrub_temporary_matrix_password, "delay") as delay:
            tasks.scrub_expired_temporary_passwords()

        delay.assert_called_once_with(MXID)

    def test_leaves_a_newer_reset_alone(self, set_password):
        _profile(
            crypto_lease="lease-2",
            crypto_lease_kind=models.CryptoLeaseKinds.RESET,
            crypto_lease_expires_at=timezone.now() + timedelta(minutes=5),
        )

        tasks.scrub_temporary_matrix_password(MXID, "lease-1")

        set_password.assert_not_called()


@override_config(**MATRIX)
class HomeserverLookupTest(TestCase):
    @respx.mock
    def test_master_key(self):
        route = respx.post(f"{HOMESERVER}/_matrix/client/v3/keys/query").mock(
            return_value=httpx.Response(200, json={"master_keys": {MXID: MASTER_KEY}})
        )

        self.assertEqual(matrix_client.get_cross_signing_master_key(MXID), MASTER_KEY)
        self.assertEqual(
            route.calls.last.request.headers["Authorization"], "Bearer test-as-token"
        )

    @respx.mock
    def test_no_master_key(self):
        respx.post(f"{HOMESERVER}/_matrix/client/v3/keys/query").mock(
            return_value=httpx.Response(200, json={"device_keys": {MXID: {}}})
        )

        self.assertIsNone(matrix_client.get_cross_signing_master_key(MXID))

    @respx.mock
    def test_secret_storage_key_info_is_read_as_the_user(self):
        base = f"{HOMESERVER}/_matrix/client/v3/user/%40alice%3Amatrix.example.com/account_data"
        default = respx.get(f"{base}/m.secret_storage.default_key").mock(
            return_value=httpx.Response(200, json={"key": "K1"})
        )
        respx.get(f"{base}/m.secret_storage.key.K1").mock(
            return_value=httpx.Response(200, json=KEY_INFO)
        )
        respx.get(f"{base}/m.cross_signing.master").mock(
            return_value=httpx.Response(200, json={"encrypted": {"K1": {}}})
        )

        self.assertEqual(
            matrix_client.get_secret_storage_state(MXID), ("K1", KEY_INFO, True)
        )
        self.assertEqual(default.calls.last.request.url.params["user_id"], MXID)

    @respx.mock
    def test_no_secret_storage(self):
        base = f"{HOMESERVER}/_matrix/client/v3/user/%40alice%3Amatrix.example.com/account_data"
        respx.get(f"{base}/m.secret_storage.default_key").mock(
            return_value=httpx.Response(404, json={"errcode": "M_NOT_FOUND"})
        )

        self.assertIsNone(matrix_client.get_secret_storage_key_info(MXID))

    @respx.mock
    def test_a_query_failure_is_not_a_missing_identity(self):
        respx.post(f"{HOMESERVER}/_matrix/client/v3/keys/query").mock(
            return_value=httpx.Response(
                200, json={"failures": {"matrix.example.com": {}}, "master_keys": {}}
            )
        )

        with self.assertRaises(matrix_client.MatrixClientError):
            matrix_client.get_cross_signing_master_key(MXID)


class MalformedKeyDescriptionTest(TestCase):
    def test_bad_iv_is_not_confirmed_and_does_not_raise(self):
        for info in (
            {**KEY_INFO, "iv": "AAAAAAAAAAA"},
            {**KEY_INFO, "iv": 12345},
            {**KEY_INFO, "mac": ["x"]},
            {**KEY_INFO, "iv": "%%%not base64%%%"},
        ):
            with self.subTest(info=info):
                self.assertFalse(recovery_keys.recovery_key_opens(RECOVERY_KEY, info))
