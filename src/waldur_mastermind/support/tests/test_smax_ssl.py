from unittest import mock

from constance.test.unittest import override_config
from django.test import TestCase

from waldur_mastermind.support.backend import smax_utils

CERTIFICATE = (
    "-----BEGIN CERTIFICATE-----\n"
    "MIIBszCCAVmgAwIBAgIUI3v0000000000000000000000000000000\n"
    "-----END CERTIFICATE-----\n"
)


@override_config(
    SMAX_API_URL="https://smax.example.com/",
    SMAX_TENANT_ID="123456789",
    SMAX_LOGIN="user@example.com",
    SMAX_PASSWORD="password",
)
class SmaxVerifySslTest(TestCase):
    @override_config(SMAX_VERIFY_SSL=False, SMAX_CERTIFICATE=CERTIFICATE)
    def test_verification_disabled_ignores_certificate(self):
        self.assertIs(smax_utils.get_smax_verify_ssl(), False)

    @override_config(SMAX_VERIFY_SSL=True, SMAX_CERTIFICATE="")
    def test_no_certificate_falls_back_to_default_bundle(self):
        self.assertIs(smax_utils.get_smax_verify_ssl(), True)

    @override_config(SMAX_VERIFY_SSL=True, SMAX_CERTIFICATE=CERTIFICATE)
    def test_certificate_is_written_to_temp_file(self):
        file_path = smax_utils.get_smax_verify_ssl()
        with open(file_path) as fh:
            self.assertEqual(fh.read(), CERTIFICATE)

    @override_config(SMAX_VERIFY_SSL=True, SMAX_CERTIFICATE=CERTIFICATE)
    def test_auth_passes_verify(self):
        backend = smax_utils.SmaxBackend()
        with mock.patch.object(smax_utils, "requests") as mock_requests:
            mock_requests.post.return_value = mock.Mock(status_code=200, text="token")
            backend.auth()
        verify = mock_requests.post.call_args.kwargs["verify"]
        self.assertEqual(verify, smax_utils.get_smax_verify_ssl())

    @override_config(SMAX_VERIFY_SSL=True, SMAX_CERTIFICATE=CERTIFICATE)
    def test_get_passes_verify(self):
        backend = smax_utils.SmaxBackend()
        backend.lwsso_cookie_key = "token"
        with mock.patch.object(smax_utils, "requests") as mock_requests:
            mock_requests.get.return_value = mock.Mock(status_code=200)
            backend._get("ems/Person")
        verify = mock_requests.get.call_args.kwargs["verify"]
        self.assertEqual(verify, smax_utils.get_smax_verify_ssl())

    @override_config(SMAX_VERIFY_SSL=True, SMAX_CERTIFICATE=CERTIFICATE)
    def test_post_passes_verify(self):
        backend = smax_utils.SmaxBackend()
        backend.lwsso_cookie_key = "token"
        with mock.patch.object(smax_utils, "requests") as mock_requests:
            mock_requests.post.return_value = mock.Mock(status_code=200)
            backend.post("ems/bulk", json={})
        verify = mock_requests.post.call_args.kwargs["verify"]
        self.assertEqual(verify, smax_utils.get_smax_verify_ssl())

    @override_config(SMAX_VERIFY_SSL=False, SMAX_CERTIFICATE="")
    def test_request_passes_verify_false(self):
        backend = smax_utils.SmaxBackend()
        backend.lwsso_cookie_key = "token"
        with mock.patch.object(smax_utils, "requests") as mock_requests:
            mock_requests.post.return_value = mock.Mock(status_code=200)
            backend.post("ems/bulk", json={})
        self.assertIs(mock_requests.post.call_args.kwargs["verify"], False)
