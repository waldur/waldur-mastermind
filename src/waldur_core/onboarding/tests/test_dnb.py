from unittest import mock

from constance.test import override_config
from django.test import TestCase

from waldur_core.onboarding.backends.base import ErrorCode, ValidationRequest
from waldur_core.onboarding.backends.dnb import client as dnb_client_module
from waldur_core.onboarding.backends.dnb.client import (
    DnbError,
    get_dnb_client,
    get_dnb_rts_client,
    reset_dnb_client,
    reset_dnb_rts_client,
)
from waldur_core.onboarding.backends.dnb.dnb_denmark import DnbDenmarkBackend
from waldur_core.onboarding.backends.dnb.dnb_finland import DnbFinlandBackend
from waldur_core.onboarding.backends.dnb.dnb_norway import DnbNorwayBackend
from waldur_core.onboarding.backends.dnb.dnb_sweden import DnbSwedenBackend
from waldur_core.onboarding.serializers import OnboardingVerificationSerializer

from .fixtures import (
    DNB_DK_COMPANY_NAME,
    DNB_DK_PERSON_IDENTIFIER,
    DNB_DK_REGISTRATION_NUMBER,
    DNB_DK_UNKNOWN_PERSON,
    DNB_FI_AUTHORIZED_PERSON,
    DNB_FI_COMPANY_NAME,
    DNB_FI_REGISTRATION_NUMBER,
    DNB_FI_UNKNOWN_PERSON,
    DNB_NO_AUTHORIZED_PERSON,
    DNB_NO_COMPANY_NAME,
    DNB_NO_REGISTRATION_NUMBER,
    DNB_NO_UNKNOWN_PERSON,
    DNB_SE_AUTHORIZED_PERSONNUMMER,
    DNB_SE_COMPANY_NAME,
    DNB_SE_REGISTRATION_NUMBER,
    DNB_SE_UNKNOWN_PERSONNUMMER,
    dnb_credit_data_finland_response,
    dnb_credit_data_norway_response,
    dnb_credit_data_sweden_response,
    dnb_credit_info_denmark_response,
    dnb_rts_denmark_response,
    dnb_rts_finland_response,
    dnb_rts_norway_response,
    dnb_rts_sweden_response,
    dnb_token_response,
)


@override_config(
    ONBOARDING_DNB_API_URL="https://sandbox-api.bisnode.com/credit-data-companies/v2",
    ONBOARDING_DNB_TOKEN_URL="https://login.bisnode.com/as/token.oauth2",
    ONBOARDING_DNB_CLIENT_ID="test-client",
    ONBOARDING_DNB_CLIENT_SECRET="test-secret",
)
class DnbClientTest(TestCase):
    def setUp(self):
        reset_dnb_client()
        self.addCleanup(reset_dnb_client)

    def _mock_response(self, payload, status_code=200):
        import json as _json

        response = mock.Mock()
        response.status_code = status_code
        response.ok = 200 <= status_code < 300
        response.json.return_value = payload
        # Mirror real requests.Response — `.text` is a JSON string so the
        # client's body-logging path can slice it like a normal string.
        response.text = _json.dumps(payload)
        if not response.ok:
            response.raise_for_status.side_effect = (
                dnb_client_module.requests.exceptions.HTTPError(response=response)
            )
        else:
            response.raise_for_status.return_value = None
        return response

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_token_success_and_cached(self, mock_post):
        mock_post.return_value = self._mock_response(dnb_token_response())
        client = get_dnb_client()

        token1 = client._get_token()
        token2 = client._get_token()

        self.assertEqual(token1, "fake-dnb-access-token")
        self.assertEqual(token2, "fake-dnb-access-token")
        # Only one network call for two get_token() invocations
        self.assertEqual(mock_post.call_count, 1)

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_token_refresh_on_expiry(self, mock_post):
        mock_post.return_value = self._mock_response(dnb_token_response(expires_in=1))
        client = get_dnb_client()

        client._get_token()
        # Simulate expiry by moving cached expiry to the past
        client._token_expires_at = 0
        client._get_token()

        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_token_missing_credentials_raises_value_error(self, mock_post):
        with override_config(
            ONBOARDING_DNB_CLIENT_ID="",
            ONBOARDING_DNB_CLIENT_SECRET="",
        ):
            reset_dnb_client()
            client = get_dnb_client()
            with self.assertRaises(ValueError) as cm:
                client._get_token()
        self.assertIn("not configured", str(cm.exception))
        mock_post.assert_not_called()

    def test_get_dnb_client_empty_api_url_raises_value_error(self):
        with override_config(ONBOARDING_DNB_API_URL=""):
            reset_dnb_client()
            with self.assertRaises(ValueError) as cm:
                get_dnb_client()
        self.assertIn("API URL is not configured", str(cm.exception))

    def test_get_dnb_client_empty_token_url_raises_value_error(self):
        with override_config(ONBOARDING_DNB_TOKEN_URL=""):
            reset_dnb_client()
            with self.assertRaises(ValueError) as cm:
                get_dnb_client()
        self.assertIn("token URL is not configured", str(cm.exception))

    def test_get_dnb_client_rejects_non_https_api_url(self):
        # A non-https API URL would send the bearer token in cleartext / to an
        # attacker-controlled host — must be rejected, not silently dialed.
        with override_config(ONBOARDING_DNB_API_URL="http://attacker.example.com"):
            reset_dnb_client()
            with self.assertRaises(ValueError) as cm:
                get_dnb_client()
        self.assertIn("https", str(cm.exception))

    def test_get_dnb_client_rejects_non_https_token_url(self):
        # The token URL carries the OAuth client_secret via HTTP Basic.
        with override_config(ONBOARDING_DNB_TOKEN_URL="http://attacker.example.com"):
            reset_dnb_client()
            with self.assertRaises(ValueError) as cm:
                get_dnb_client()
        self.assertIn("https", str(cm.exception))

    def test_get_dnb_client_rejects_hostless_url(self):
        with override_config(ONBOARDING_DNB_API_URL="https:///no-host"):
            reset_dnb_client()
            with self.assertRaises(ValueError):
                get_dnb_client()

    def test_get_dnb_rts_client_rejects_non_https_api_url(self):
        with override_config(ONBOARDING_DNB_RTS_API_URL="http://attacker.example.com"):
            reset_dnb_rts_client()
            self.addCleanup(reset_dnb_rts_client)
            with self.assertRaises(ValueError) as cm:
                get_dnb_rts_client()
        self.assertIn("https", str(cm.exception))

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_concurrent_token_refresh_is_serialized(self, mock_post):
        # N threads all see an expired token and call _get_token concurrently.
        # The lock-guarded double-check must ensure only one token fetch happens.
        import threading as _threading

        mock_post.return_value = self._mock_response(dnb_token_response())
        client = get_dnb_client()

        barrier = _threading.Barrier(8)

        def worker():
            barrier.wait()
            client._get_token()

        threads = [_threading.Thread(target=worker) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(mock_post.call_count, 1)

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_token_missing_access_token_raises_dnb_error(self, mock_post):
        # Token endpoint returned 200 but no access_token in body.
        mock_post.return_value = self._mock_response({"expires_in": 3600})
        client = get_dnb_client()
        with self.assertRaises(DnbError) as cm:
            client._get_token()
        self.assertIn("Access token missing", str(cm.exception))

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_company_malformed_json_raises_dnb_error(self, mock_post):
        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                return self._mock_response(dnb_token_response())
            bad = self._mock_response({})
            bad.json.side_effect = ValueError("not JSON")
            return bad

        mock_post.side_effect = _side_effect
        client = get_dnb_client()
        with self.assertRaises(DnbError) as cm:
            client.get_company("se", "5560021361")
        self.assertIn("non-JSON body", str(cm.exception))

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_token_uses_http_basic_auth(self, mock_post):
        # Bisnode's token endpoint requires client_secret_basic; sending
        # creds in the form body returns 401. Pin the wire format so this
        # cannot silently regress.
        mock_post.return_value = self._mock_response(dnb_token_response())
        client = get_dnb_client()

        client._get_token()

        _, kwargs = mock_post.call_args
        self.assertEqual(kwargs["auth"], ("test-client", "test-secret"))
        self.assertNotIn("client_id", kwargs["data"])
        self.assertNotIn("client_secret", kwargs["data"])
        self.assertEqual(kwargs["data"]["grant_type"], "client_credentials")

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_token_surfaces_oauth_error_description(self, mock_post):
        # RFC 6749 token errors include {error, error_description}. The
        # DnbError message must include them so operators can distinguish
        # invalid_client vs invalid_scope without reading server logs.
        mock_post.return_value = self._mock_response(
            {
                "error": "invalid_client",
                "error_description": "Client authentication failed",
            },
            status_code=401,
        )
        client = get_dnb_client()

        with self.assertRaises(DnbError) as cm:
            client._get_token()

        message = str(cm.exception)
        self.assertIn("Access token request failed", message)
        self.assertIn("invalid_client", message)
        self.assertIn("Client authentication failed", message)

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_token_network_error_raises_dnb_error(self, mock_post):
        mock_post.side_effect = dnb_client_module.requests.exceptions.ConnectionError(
            "boom"
        )
        client = get_dnb_client()
        with self.assertRaises(DnbError) as cm:
            client._get_token()
        self.assertIn("Access token request failed", str(cm.exception))

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_company_success(self, mock_post):
        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                return self._mock_response(dnb_token_response())
            return self._mock_response(
                {"registrationNumber": "556002-1361", "name": "Test AB"}
            )

        mock_post.side_effect = _side_effect
        client = get_dnb_client()

        result = client.get_company("se", "5560021361", segments=["MANAGEMENT"])

        self.assertEqual(result["name"], "Test AB")
        # Two POST calls: token + company
        self.assertEqual(mock_post.call_count, 2)

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_company_404_raises_not_found(self, mock_post):
        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                return self._mock_response(dnb_token_response())
            return self._mock_response({"error": "not found"}, status_code=404)

        mock_post.side_effect = _side_effect
        client = get_dnb_client()

        with self.assertRaises(DnbError) as cm:
            client.get_company("se", "missing-regno")
        self.assertTrue(getattr(cm.exception, "not_found", False))

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_company_401_triggers_refresh_and_retry(self, mock_post):
        call_log = {"token": 0, "company": 0}

        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                call_log["token"] += 1
                return self._mock_response(dnb_token_response())
            call_log["company"] += 1
            if call_log["company"] == 1:
                return self._mock_response({"error": "unauthorized"}, status_code=401)
            return self._mock_response({"registrationNumber": "5560021361"})

        mock_post.side_effect = _side_effect
        client = get_dnb_client()

        result = client.get_company("se", "5560021361")

        self.assertEqual(result["registrationNumber"], "5560021361")
        self.assertEqual(call_log["token"], 2)  # original + refresh
        self.assertEqual(call_log["company"], 2)  # 401 + retry

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_get_company_persistent_401_raises_auth_error(self, mock_post):
        # Two successive 401s: the retry after force-refresh is still 401,
        # so we surface a credential-specific message rather than a generic
        # HTTP error. Tells operators where to look.
        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                return self._mock_response(dnb_token_response())
            return self._mock_response({"error": "unauthorized"}, status_code=401)

        mock_post.side_effect = _side_effect
        client = get_dnb_client()

        with self.assertRaises(DnbError) as cm:
            client.get_company("se", "5560021361")
        self.assertIn("authentication failed", str(cm.exception).lower())
        self.assertIn("check client credentials", str(cm.exception))

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_check_rts_payload_includes_authority_type_default(self, mock_post):
        # NO RTS endpoint rejects requests missing `authorityType` with a 400;
        # the doc table also lists DEFAULT/PROCURATION variants. Pin the wire
        # payload so this can't silently regress to the old shape.
        captured = {}

        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                return self._mock_response(dnb_token_response())
            captured["url"] = url
            captured["body"] = kwargs.get("json")
            return self._mock_response({"interpretationLevel": "COMPLETE"})

        mock_post.side_effect = _side_effect
        client = get_dnb_client()

        client.check_rts(
            "no",
            "935276608",
            signatories=[
                {
                    "name": {"firstName": "Fire", "lastName": "Testperson"},
                    "birthDate": "1960-06-09",
                }
            ],
        )

        self.assertIn("/rts/company-signatories/detailed/NO", captured["url"])
        self.assertEqual(captured["body"]["registrationNumber"], "935276608")
        self.assertEqual(captured["body"]["authorityType"], "DEFAULT")
        self.assertEqual(captured["body"]["signTogether"], "ANY")

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_check_company_rts_hits_company_endpoint(self, mock_post):
        # DK uses the company-level endpoint: just the registration number, no
        # signatories. authorityType=None must be omitted from the body (DK).
        captured = {}

        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                return self._mock_response(dnb_token_response())
            captured["url"] = url
            captured["body"] = kwargs.get("json")
            return self._mock_response({"interpretationLevel": "COMPLETE"})

        mock_post.side_effect = _side_effect
        client = get_dnb_client()

        client.check_company_rts("dk", "34703943", authority_type=None)

        self.assertIn("/rts/company/detailed/DK", captured["url"])
        self.assertEqual(captured["body"], {"registrationNumber": "34703943"})
        self.assertNotIn("signatories", captured["body"])

    @mock.patch("waldur_core.onboarding.backends.dnb.client.requests.post")
    def test_check_rts_surfaces_message_and_validation_errors(self, mock_post):
        # Bisnode RTS 400 envelopes use {message, validationErrors:[...]};
        # the wrapped DnbError must include both so operators see *what* was
        # rejected without reading server logs. Regression guard for the
        # "400 Client Error: for url:" empty-detail case we hit on NO.
        def _side_effect(url, *args, **kwargs):
            if "token.oauth2" in url:
                return self._mock_response(dnb_token_response())
            return self._mock_response(
                {
                    "message": "Validation failed",
                    "validationErrors": [
                        {
                            "field": "authorityType",
                            "message": "must not be null",
                        },
                    ],
                },
                status_code=400,
            )

        mock_post.side_effect = _side_effect
        client = get_dnb_client()

        with self.assertRaises(DnbError) as cm:
            client.check_rts("no", "935276608", signatories=[{"name": {}}])
        self.assertIn("Validation failed", str(cm.exception))
        self.assertIn("authorityType", str(cm.exception))
        self.assertIn("must not be null", str(cm.exception))


@override_config(
    ONBOARDING_DNB_API_URL="https://sandbox-api.bisnode.com/credit-data-companies/v2",
    ONBOARDING_DNB_RTS_API_URL="https://sandbox-api.bisnode.com/nordic-rts/v1",
    ONBOARDING_DNB_TOKEN_URL="https://login.bisnode.com/as/token.oauth2",
    ONBOARDING_DNB_CLIENT_ID="test-client",
    ONBOARDING_DNB_CLIENT_SECRET="test-secret",
)
class DnbSwedenBackendTest(TestCase):
    """SE backend uses the Nordic Right to Sign API, not the credit-data API."""

    def setUp(self):
        reset_dnb_client()
        reset_dnb_rts_client()
        self.addCleanup(reset_dnb_client)
        self.addCleanup(reset_dnb_rts_client)
        self.backend = DnbSwedenBackend()

        patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.check_rts"
        )
        self.mock_check_rts = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_check_rts.return_value = dnb_rts_sweden_response()

        # Credit-data enrichment is invoked on authorization success.
        # Default to the canonical response so existing happy-path tests
        # don't have to opt in.
        enrich_patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.get_company"
        )
        self.mock_get_company = enrich_patcher.start()
        self.addCleanup(enrich_patcher.stop)
        self.mock_get_company.return_value = dnb_credit_data_sweden_response()

    def _build_request(self, personnummer: str) -> ValidationRequest:
        return ValidationRequest(
            country="SE",
            person_identifier=personnummer,
            legal_person_identifier=DNB_SE_REGISTRATION_NUMBER,
            legal_name=DNB_SE_COMPANY_NAME,
        )

    def test_authorized_user_is_verified(self):
        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertTrue(result.is_valid)
        self.assertIn("Styrelseledamot", result.user_roles)
        self.assertEqual(result.company_data["name"], DNB_SE_COMPANY_NAME)
        self.assertEqual(result.method_used, "dnb_se")
        # Must hit the RTS endpoint with the user's personnummer in
        # signatories[].ssn and signTogether=ANY.
        self.mock_check_rts.assert_called_once_with(
            "se",
            DNB_SE_REGISTRATION_NUMBER,
            signatories=[{"ssn": DNB_SE_AUTHORIZED_PERSONNUMMER}],
            sign_together="ANY",
            authority_type="DEFAULT",
        )

    def test_unknown_user_is_not_listed(self):
        # Person isn't in any of signatories[]/coSignatories[]/nonSignatories[].
        # Distinct from NOT_AUTHORIZED so the UI can hint at wrong DOB / name.
        self.mock_check_rts.return_value = dnb_rts_sweden_response(in_signatories=False)

        result = self.backend.validate_company(
            self._build_request(DNB_SE_UNKNOWN_PERSONNUMMER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.PERSON_NOT_LISTED)
        self.assertEqual(result.user_roles, [])
        # Company data is still populated so staff can review
        self.assertEqual(result.company_data["name"], DNB_SE_COMPANY_NAME)

    def test_user_in_non_signatories_is_not_authorized(self):
        # Person exists in the company records but is explicitly not a
        # signatory — surface the role so staff review has context.
        self.mock_check_rts.return_value = dnb_rts_sweden_response(
            in_signatories=False, in_non_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("Styrelseledamot", result.user_roles)

    def test_user_in_co_signatories_is_not_authorized(self):
        # coSignatories require a co-signer; we treat as not-authorized
        # so staff can confirm the multi-signer arrangement.
        self.mock_check_rts.return_value = dnb_rts_sweden_response(
            in_signatories=False, in_co_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("Styrelseledamot", result.user_roles)

    def test_company_not_found(self):
        self.mock_check_rts.side_effect = DnbError("not found", not_found=True)

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.COMPANY_NOT_FOUND)

    def test_api_error(self):
        self.mock_check_rts.side_effect = DnbError("500 Internal Server Error")

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.API_ERROR)

    def test_matches_correct_signatory_among_many(self):
        # Multiple signatories returned — only one matches the requested SSN.
        response = dnb_rts_sweden_response()
        response["signatories"].insert(
            0,
            {
                "type": "PERSON",
                "name": {"firstName": "Wrong", "lastName": "Person"},
                "nationalIdentificationNumber": "990909-9999",
                "roles": [{"code": "OB", "description": "Observer"}],
                "signingRights": [{"ruleId": "r-2", "signatoryGroupId": "r-g-2"}],
            },
        )
        self.mock_check_rts.return_value = response

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertTrue(result.is_valid)
        self.assertIn("Styrelseledamot", result.user_roles)
        self.assertNotIn("Observer", result.user_roles)

    def test_10_digit_request_matches_12_digit_signatory(self):
        # User submits 10-digit "810103-2384"; D&B returned 12-digit
        # "198101032384". Both normalize to the same last-10 digits.
        result = self.backend.validate_company(self._build_request("810103-2384"))

        self.assertTrue(result.is_valid)
        self.assertIn("Styrelseledamot", result.user_roles)

    def test_12_digit_request_matches_10_digit_signatory(self):
        # Inverse: user submits 12-digit, D&B returned 10-digit.
        response = dnb_rts_sweden_response(person_pnr="810103-2384")
        self.mock_check_rts.return_value = response

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertTrue(result.is_valid)

    def test_rts_response_populates_extra_company_fields(self):
        # Fields available from the RTS payload itself (no extra call) should
        # land in company_data regardless of whether enrichment runs.
        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertEqual(result.company_data["duns_number"], "123456789")
        self.assertEqual(result.company_data["legal_form"], "AB")
        self.assertEqual(result.company_data["status"], "ACTIVE")
        self.assertEqual(
            result.company_data["signing_authority"], "Firman tecknas av styrelsen"
        )

    def test_authorized_user_gets_credit_data_enrichment(self):
        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertTrue(result.is_valid)
        self.assertEqual(result.company_data["address"], "Storgatan 1, Stockholm")
        self.assertEqual(result.company_data["postal"], "11122")
        self.assertEqual(result.company_data["registration_date"], "1995-01-15")
        self.assertEqual(result.company_data["vat_number"], "SE556002136101")
        self.mock_get_company.assert_called_once_with("se", DNB_SE_REGISTRATION_NUMBER)

    def test_unauthorized_user_skips_credit_data_enrichment(self):
        self.mock_check_rts.return_value = dnb_rts_sweden_response(in_signatories=False)

        result = self.backend.validate_company(
            self._build_request(DNB_SE_UNKNOWN_PERSONNUMMER)
        )

        self.assertFalse(result.is_valid)
        self.assertNotIn("address", result.company_data)
        self.assertNotIn("postal", result.company_data)
        self.mock_get_company.assert_not_called()

    def test_enrichment_failure_does_not_fail_validation(self):
        # The user was already authorized via RTS — a downstream credit-data
        # outage must not flip the result to NOT_AUTHORIZED.
        self.mock_get_company.side_effect = DnbError("500 Internal Server Error")

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertTrue(result.is_valid)
        self.assertEqual(result.company_data["name"], DNB_SE_COMPANY_NAME)
        self.assertNotIn("address", result.company_data)
        self.assertNotIn("postal", result.company_data)

    def test_enrichment_company_not_found_does_not_fail_validation(self):
        # RTS confirmed authorization; if credit-data 404s the company we
        # still trust RTS rather than escalating.
        self.mock_get_company.side_effect = DnbError("not found", not_found=True)

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertTrue(result.is_valid)
        self.assertNotIn("address", result.company_data)

    def test_enrichment_with_no_address_block(self):
        self.mock_get_company.return_value = dnb_credit_data_sweden_response(
            include_address=False
        )

        result = self.backend.validate_company(
            self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
        )

        self.assertTrue(result.is_valid)
        self.assertNotIn("address", result.company_data)
        self.assertNotIn("postal", result.company_data)
        # Non-address enrichment fields still come through
        self.assertEqual(result.company_data["registration_date"], "1995-01-15")

    @mock.patch(
        "waldur_core.onboarding.backends.dnb.base.get_dnb_rts_client",
        side_effect=ValueError("Dun & Bradstreet RTS API URL is not configured"),
    )
    def test_configuration_error_propagates_as_value_error(self, _):
        # Regression guard: validate_company must let ValueError bubble up
        # so OnboardingValidator maps it to CONFIGURATION_ERROR rather than
        # a misleading API_ERROR / NOT_AUTHORIZED result.
        with self.assertRaises(ValueError):
            self.backend.validate_company(
                self._build_request(DNB_SE_AUTHORIZED_PERSONNUMMER)
            )


@override_config(
    ONBOARDING_DNB_API_URL="https://sandbox-api.bisnode.com/credit-data-companies/v2",
    ONBOARDING_DNB_RTS_API_URL="https://sandbox-api.bisnode.com/nordic-rts/v1",
    ONBOARDING_DNB_TOKEN_URL="https://login.bisnode.com/as/token.oauth2",
    ONBOARDING_DNB_CLIENT_ID="test-client",
    ONBOARDING_DNB_CLIENT_SECRET="test-secret",
)
class DnbNorwayBackendTest(TestCase):
    """NO backend uses the Nordic Right to Sign API, not the credit-data API."""

    def setUp(self):
        reset_dnb_client()
        reset_dnb_rts_client()
        self.addCleanup(reset_dnb_client)
        self.addCleanup(reset_dnb_rts_client)
        self.backend = DnbNorwayBackend()

        patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.check_rts"
        )
        self.mock_check_rts = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_check_rts.return_value = dnb_rts_norway_response()

        # Credit-data enrichment is invoked on authorization success.
        # Default to the canonical response so existing happy-path tests
        # don't have to opt in.
        enrich_patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.get_company"
        )
        self.mock_get_company = enrich_patcher.start()
        self.addCleanup(enrich_patcher.stop)
        self.mock_get_company.return_value = dnb_credit_data_norway_response()

    def _build_request(self, person: dict) -> ValidationRequest:
        return ValidationRequest(
            country="NO",
            person_identifier=person,
            legal_person_identifier=DNB_NO_REGISTRATION_NUMBER,
            legal_name=DNB_NO_COMPANY_NAME,
        )

    def test_authorized_user_is_verified(self):
        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        self.assertIn("Boardmember", result.user_roles)
        self.assertEqual(result.company_data["name"], DNB_NO_COMPANY_NAME)
        self.assertEqual(result.method_used, "dnb_no")
        # Must hit the RTS endpoint with the user's name + birthDate in
        # signatories[] and signTogether=ANY.
        self.mock_check_rts.assert_called_once_with(
            "no",
            DNB_NO_REGISTRATION_NUMBER,
            signatories=[
                {
                    "name": {"firstName": "Ola", "lastName": "Nordmann"},
                    "birthDate": "1980-05-17",
                }
            ],
            sign_together="ANY",
            authority_type="DEFAULT",
        )

    def test_unknown_user_is_not_listed(self):
        # Person isn't in any of signatories[]/coSignatories[]/nonSignatories[].
        # Distinct from NOT_AUTHORIZED so the UI can hint at wrong DOB / name.
        self.mock_check_rts.return_value = dnb_rts_norway_response(in_signatories=False)

        result = self.backend.validate_company(
            self._build_request(DNB_NO_UNKNOWN_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.PERSON_NOT_LISTED)
        self.assertEqual(result.user_roles, [])
        # Company data is still populated so staff can review.
        self.assertEqual(result.company_data["name"], DNB_NO_COMPANY_NAME)

    def test_user_in_non_signatories_is_not_authorized(self):
        # Person exists in the company records but is explicitly not a
        # signatory — surface the role so staff review has context.
        self.mock_check_rts.return_value = dnb_rts_norway_response(
            in_signatories=False, in_non_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("Boardmember", result.user_roles)

    def test_user_in_co_signatories_is_not_authorized(self):
        # coSignatories require a co-signer; we treat as not-authorized
        # so staff can confirm the multi-signer arrangement.
        self.mock_check_rts.return_value = dnb_rts_norway_response(
            in_signatories=False, in_co_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("Boardmember", result.user_roles)

    def test_company_not_found(self):
        self.mock_check_rts.side_effect = DnbError("not found", not_found=True)

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.COMPANY_NOT_FOUND)

    def test_api_error(self):
        self.mock_check_rts.side_effect = DnbError("500 Internal Server Error")

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.API_ERROR)

    def test_matches_correct_signatory_among_many(self):
        # Multiple signatories returned — only the one matching the requested
        # name+birthDate authorizes. Other entries (different person) must
        # not leak their roles into user_roles.
        response = dnb_rts_norway_response()
        response["signatories"].insert(
            0,
            {
                "type": "PERSON",
                "name": {
                    "firstName": "Kari",
                    "lastName": "Hansen",
                    "fullName": "Kari Hansen",
                },
                "birthDate": "1975-02-02",
                "roles": [{"code": "OB", "description": "Observer"}],
                "signingRights": [{"ruleId": "r-2", "signatoryGroupId": "r-g-2"}],
            },
        )
        self.mock_check_rts.return_value = response

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        self.assertIn("Boardmember", result.user_roles)
        self.assertNotIn("Observer", result.user_roles)

    def test_name_match_is_case_insensitive(self):
        # User submits "ola"/"nordmann" lowercase; D&B returns canonical
        # casing. The normalizer lowercases both sides.
        result = self.backend.validate_company(
            self._build_request(
                {
                    "first_name": "ola",
                    "last_name": "NORDMANN",
                    "birth_date": "1980-05-17",
                }
            )
        )

        self.assertTrue(result.is_valid)
        self.assertIn("Boardmember", result.user_roles)

    def test_iso_datetime_birthdate_in_response_still_matches(self):
        # D&B sometimes returns "1980-05-17T00:00:00Z" instead of "1980-05-17";
        # the date normalizer strips the time component.
        response = dnb_rts_norway_response(birth_date="1980-05-17T00:00:00Z")
        self.mock_check_rts.return_value = response

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)

    def test_rts_response_populates_extra_company_fields(self):
        # Fields available from the RTS payload itself (no extra call) should
        # land in company_data regardless of whether enrichment runs.
        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertEqual(result.company_data["duns_number"], "987654321")
        self.assertEqual(result.company_data["legal_form"], "AS")
        self.assertEqual(result.company_data["status"], "ACTIVE")
        self.assertEqual(
            result.company_data["signing_authority"], "Selskapet tegnes av styret"
        )

    def test_authorized_user_gets_credit_data_enrichment(self):
        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        # NO Credit Data uses the nested v3 shape — confirm our extractor
        # reads it correctly into the same flat company_data keys SE produces.
        self.assertEqual(result.company_data["address"], "Karl Johans gate 1, Oslo")
        self.assertEqual(result.company_data["postal"], "0154")
        self.assertEqual(result.company_data["registration_date"], "1995-01-15")
        self.assertEqual(result.company_data["number_of_employees"], 25)
        # NO doesn't publish a VAT number (only a `registeredInVat` boolean),
        # so this key must NOT appear in the enrichment output.
        self.assertNotIn("vat_number", result.company_data)
        self.mock_get_company.assert_called_once_with("no", DNB_NO_REGISTRATION_NUMBER)

    def test_unauthorized_user_skips_credit_data_enrichment(self):
        self.mock_check_rts.return_value = dnb_rts_norway_response(in_signatories=False)

        result = self.backend.validate_company(
            self._build_request(DNB_NO_UNKNOWN_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertNotIn("address", result.company_data)
        self.assertNotIn("postal", result.company_data)
        self.mock_get_company.assert_not_called()

    def test_enrichment_failure_does_not_fail_validation(self):
        # The user was already authorized via RTS — a downstream credit-data
        # outage must not flip the result to NOT_AUTHORIZED.
        self.mock_get_company.side_effect = DnbError("500 Internal Server Error")

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        self.assertEqual(result.company_data["name"], DNB_NO_COMPANY_NAME)
        self.assertNotIn("address", result.company_data)
        self.assertNotIn("postal", result.company_data)

    def test_enrichment_company_not_found_does_not_fail_validation(self):
        # RTS confirmed authorization; if credit-data 404s the company we
        # still trust RTS rather than escalating.
        self.mock_get_company.side_effect = DnbError("not found", not_found=True)

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        self.assertNotIn("address", result.company_data)

    def test_enrichment_with_no_address_block(self):
        self.mock_get_company.return_value = dnb_credit_data_norway_response(
            include_address=False
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        self.assertNotIn("address", result.company_data)
        self.assertNotIn("postal", result.company_data)
        # Non-address enrichment fields still come through.
        self.assertEqual(result.company_data["registration_date"], "1995-01-15")

    @mock.patch(
        "waldur_core.onboarding.backends.dnb.base.get_dnb_rts_client",
        side_effect=ValueError("Dun & Bradstreet RTS API URL is not configured"),
    )
    def test_configuration_error_propagates_as_value_error(self, _):
        # Regression guard: validate_company must let ValueError bubble up
        # so OnboardingValidator maps it to CONFIGURATION_ERROR rather than
        # a misleading API_ERROR / NOT_AUTHORIZED result.
        with self.assertRaises(ValueError):
            self.backend.validate_company(self._build_request(DNB_NO_AUTHORIZED_PERSON))

    def test_person_not_listed_uses_distinct_error_code(self):
        # Person isn't in any list → PERSON_NOT_LISTED. Distinct from the
        # nonSignatories[]-found-but-NOT_AUTHORIZED case so the UI can
        # suggest the user double-check DOB / name spelling.
        self.mock_check_rts.return_value = dnb_rts_norway_response(in_signatories=False)

        result = self.backend.validate_company(
            self._build_request(DNB_NO_UNKNOWN_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.PERSON_NOT_LISTED)
        self.assertEqual(result.user_roles, [])
        self.assertIn("not found", result.error_message.lower())

    def test_person_in_non_signatories_uses_not_authorized_message(self):
        # Person IS in nonSignatories[] → NOT_AUTHORIZED with the more
        # specific "associated with the company but no signing authority"
        # wording so reviewers see the right escalation hint.
        self.mock_check_rts.return_value = dnb_rts_norway_response(
            in_signatories=False, in_non_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("signing authority", result.error_message.lower())
        self.assertNotIn("not found", result.error_message.lower())

    def test_signing_rules_land_in_company_data(self):
        # signingRules[] is flattened into human-readable strings matching
        # the published Bisnode doc-table vocabulary ("ALONE (MD = 1)",
        # "JOINTLY (BOARDMEMBERS = ALL)").
        self.mock_check_rts.return_value = dnb_rts_norway_response(
            signing_rules=[
                {
                    "code": "ALONE",
                    "signatoryGroups": [
                        {
                            "groupType": "MANAGING_DIRECTOR",
                            "quantity": {"type": "VALUE", "value": 1},
                        }
                    ],
                },
                {
                    "code": "JOINTLY",
                    "signatoryGroups": [
                        {
                            "groupType": "BOARDMEMBERS",
                            "quantity": {"type": "ALL", "value": 0},
                        }
                    ],
                },
            ],
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertEqual(
            result.company_data["signing_rules"],
            ["ALONE (MANAGING_DIRECTOR = 1)", "JOINTLY (BOARDMEMBERS = ALL)"],
        )

    def test_signing_issues_and_infos_land_in_company_data(self):
        # signingIssues[] should surface to company_data so staff see red
        # flags like COMPANY_INACTIVE without grovelling through raw_response.
        self.mock_check_rts.return_value = dnb_rts_norway_response(
            in_signatories=False,
            signing_issues=[
                {
                    "code": "COMPANY_INACTIVE",
                    "signingAuthorityDescription": "Company is no longer active",
                }
            ],
            signing_infos=[{"code": "NOT_DEFINED"}],
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_UNKNOWN_PERSON)
        )

        self.assertEqual(
            result.company_data["signing_issues"],
            ["COMPANY_INACTIVE: Company is no longer active"],
        )
        self.assertEqual(result.company_data["signing_infos"], ["NOT_DEFINED"])

    def test_authority_type_and_period_land_in_company_data(self):
        # authorityType and signingAuthorityDescriptionPeriod surface to
        # company_data so reviewers can correlate DEFAULT vs PROCURATION
        # and see when the listed authority became effective.
        self.mock_check_rts.return_value = dnb_rts_norway_response(
            authority_type="PROCURATION",
            period_start="2025-09-11",
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )

        self.assertEqual(result.company_data["authority_type"], "PROCURATION")
        self.assertEqual(
            result.company_data["signing_authority_period_start"], "2025-09-11"
        )

    def test_partial_interpretation_level_surfaces_in_company_data(self):
        # PARTIAL means D&B couldn't fully parse the company's signing
        # structure — staff should see it as advisory, so we promote it
        # to company_data. COMPLETE is the silent default and stays absent.
        self.mock_check_rts.return_value = dnb_rts_norway_response(
            interpretation_level="PARTIAL",
            in_signatories=False,
        )

        result = self.backend.validate_company(
            self._build_request(DNB_NO_UNKNOWN_PERSON)
        )

        self.assertEqual(result.company_data["interpretation_level"], "PARTIAL")

        # And for the COMPLETE case the key stays absent — no noise for the
        # 99% happy path.
        self.mock_check_rts.return_value = dnb_rts_norway_response()
        result2 = self.backend.validate_company(
            self._build_request(DNB_NO_AUTHORIZED_PERSON)
        )
        self.assertNotIn("interpretation_level", result2.company_data)


@override_config(
    ONBOARDING_DNB_API_URL="https://sandbox-api.bisnode.com/credit-data-companies/v2",
    ONBOARDING_DNB_RTS_API_URL="https://sandbox-api.bisnode.com/nordic-rts/v1",
    ONBOARDING_DNB_TOKEN_URL="https://login.bisnode.com/as/token.oauth2",
    ONBOARDING_DNB_CLIENT_ID="test-client",
    ONBOARDING_DNB_CLIENT_SECRET="test-secret",
)
class DnbDenmarkBackendTest(TestCase):
    """DK uses the company-level Nordic Right to Sign endpoint.

    DK's company-signatories endpoint requires an id or name+address per
    signatory (which onboarding doesn't collect), so DK queries the
    company-level endpoint with just the registration number and name-matches
    the user against the returned signatory lists. An unambiguous match in
    signatories[] is trusted directly (RTS is the signing-authority source);
    credit-data is enrichment only and non-fatal. Same-name collisions escalate
    via the ambiguity guard.
    """

    def setUp(self):
        reset_dnb_client()
        reset_dnb_rts_client()
        self.addCleanup(reset_dnb_client)
        self.addCleanup(reset_dnb_rts_client)
        self.backend = DnbDenmarkBackend()

        patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.check_company_rts"
        )
        self.mock_check_company_rts = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_check_company_rts.return_value = dnb_rts_denmark_response()

        # Credit Info (COMPANY_INFORMATION) is fetched on the success path to
        # enrich company_data; it is non-fatal.
        enrich_patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.get_company"
        )
        self.mock_get_company = enrich_patcher.start()
        self.addCleanup(enrich_patcher.stop)
        self.mock_get_company.return_value = dnb_credit_info_denmark_response()

    def _build_request(self, person: dict) -> ValidationRequest:
        return ValidationRequest(
            country="DK",
            person_identifier=person,
            legal_person_identifier=DNB_DK_REGISTRATION_NUMBER,
            legal_name=DNB_DK_COMPANY_NAME,
        )

    def test_unambiguous_signatory_is_verified(self):
        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertTrue(result.is_valid)
        self.assertIn("Boardmember", result.user_roles)
        self.assertEqual(result.company_data["name"], DNB_DK_COMPANY_NAME)
        self.assertEqual(result.method_used, "dnb_dk")
        # DK uses the company-level endpoint: just the registration number, no
        # per-person payload; authorityType is omitted (None) for DK.
        self.mock_check_company_rts.assert_called_once_with(
            "dk",
            DNB_DK_REGISTRATION_NUMBER,
            authority_type=None,
        )
        # Enrichment pulls COMPANY_INFORMATION only.
        self.mock_get_company.assert_called_once_with(
            "dk",
            DNB_DK_REGISTRATION_NUMBER,
            segments=["COMPANY_INFORMATION"],
        )

    def test_signatory_match_tolerates_middle_name(self):
        # The registered signatory carries a middle name ("Lars Bo Jensen");
        # first+last still match the requested person.
        self.mock_check_company_rts.return_value = dnb_rts_denmark_response(
            first_name="Lars Bo", last_name="Jensen"
        )

        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertTrue(result.is_valid)

    def test_ambiguous_name_escalates(self):
        # Two distinct people share the queried name in signatories[]; we can't
        # tell which one the applicant is, so escalate instead of guessing.
        response = dnb_rts_denmark_response()
        response["signatories"].insert(
            0,
            {
                "type": "PERSON",
                "name": "Lars Jensen",
                "cvrId": "4000000099",
                "roles": [{"code": "LE", "description": "Boardmember"}],
                "signingRights": [{"ruleId": "r-9", "signatoryGroupId": "r-g-9"}],
            },
        )
        self.mock_check_company_rts.return_value = response

        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.AMBIGUOUS_MATCH)
        # Ambiguity is decided before enrichment, so no credit-data call.
        self.mock_get_company.assert_not_called()

    def test_unknown_user_is_not_listed(self):
        self.mock_check_company_rts.return_value = dnb_rts_denmark_response(
            in_signatories=False
        )

        result = self.backend.validate_company(
            self._build_request(DNB_DK_UNKNOWN_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.PERSON_NOT_LISTED)
        self.assertEqual(result.user_roles, [])
        self.assertEqual(result.company_data["name"], DNB_DK_COMPANY_NAME)
        self.mock_get_company.assert_not_called()

    def test_user_in_non_signatories_is_not_authorized(self):
        self.mock_check_company_rts.return_value = dnb_rts_denmark_response(
            in_signatories=False, in_non_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("Boardmember", result.user_roles)

    def test_user_in_co_signatories_is_not_authorized(self):
        self.mock_check_company_rts.return_value = dnb_rts_denmark_response(
            in_signatories=False, in_co_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)

    def test_authorized_user_gets_credit_data_enrichment(self):
        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertTrue(result.is_valid)
        # DK Credit Info uses the nested shape with DK-specific paths: VAT under
        # identifiers.vatNumber and the date as foundationDate.
        self.assertEqual(result.company_data["address"], "Gyngemose Parkvej 50, Søborg")
        self.assertEqual(result.company_data["postal"], "2860")
        self.assertEqual(result.company_data["vat_number"], "DK12345678")
        self.assertEqual(result.company_data["registration_date"], "2001-03-14")
        self.assertEqual(result.company_data["number_of_employees"], 12)

    def test_enrichment_failure_does_not_fail_validation(self):
        # RTS already authorized the user — a credit-data outage or a missing
        # DK credit-data record (the sandbox has none) must not flip the result.
        self.mock_get_company.side_effect = DnbError("not found", not_found=True)

        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertTrue(result.is_valid)
        self.assertEqual(result.company_data["name"], DNB_DK_COMPANY_NAME)
        self.assertNotIn("address", result.company_data)

    def test_company_not_found(self):
        self.mock_check_company_rts.side_effect = DnbError("not found", not_found=True)

        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.COMPANY_NOT_FOUND)

    def test_api_error(self):
        self.mock_check_company_rts.side_effect = DnbError("500 Internal Server Error")

        result = self.backend.validate_company(
            self._build_request(DNB_DK_PERSON_IDENTIFIER)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.API_ERROR)

    @mock.patch(
        "waldur_core.onboarding.backends.dnb.base.get_dnb_rts_client",
        side_effect=ValueError("Dun & Bradstreet RTS API URL is not configured"),
    )
    def test_configuration_error_propagates_as_value_error(self, _):
        with self.assertRaises(ValueError):
            self.backend.validate_company(self._build_request(DNB_DK_PERSON_IDENTIFIER))


class DnbBackendRegistrationTest(TestCase):
    def test_all_four_backends_are_registered(self):
        from waldur_core.onboarding.backends import backend_registry

        for method in ["dnb_se", "dnb_no", "dnb_dk", "dnb_fi"]:
            backend = backend_registry.find_backend_by_method(method)
            self.assertIsNotNone(
                backend, f"Backend for method {method!r} not registered"
            )
            self.assertEqual(backend.get_validation_method(), method)


@override_config(
    ONBOARDING_DNB_API_URL="https://sandbox-api.bisnode.com/credit-data-companies/v2",
    ONBOARDING_DNB_TOKEN_URL="https://login.bisnode.com/as/token.oauth2",
    ONBOARDING_DNB_CLIENT_ID="test-client",
    ONBOARDING_DNB_CLIENT_SECRET="test-secret",
)
class DnbFinlandBackendTest(TestCase):
    """FI backend uses the Nordic Right to Sign API (name + birthDate), like NO."""

    def setUp(self):
        reset_dnb_client()
        reset_dnb_rts_client()
        self.addCleanup(reset_dnb_client)
        self.addCleanup(reset_dnb_rts_client)
        self.backend = DnbFinlandBackend()

        patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.check_rts"
        )
        self.mock_check_rts = patcher.start()
        self.addCleanup(patcher.stop)
        self.mock_check_rts.return_value = dnb_rts_finland_response()

        # Credit-data enrichment is invoked on authorization success.
        enrich_patcher = mock.patch(
            "waldur_core.onboarding.backends.dnb.client.DnbClient.get_company"
        )
        self.mock_get_company = enrich_patcher.start()
        self.addCleanup(enrich_patcher.stop)
        self.mock_get_company.return_value = dnb_credit_data_finland_response()

    def _build_request(self, person: dict) -> ValidationRequest:
        return ValidationRequest(
            country="FI",
            person_identifier=person,
            legal_person_identifier=DNB_FI_REGISTRATION_NUMBER,
            legal_name=DNB_FI_COMPANY_NAME,
        )

    def test_authorized_user_is_verified(self):
        result = self.backend.validate_company(
            self._build_request(DNB_FI_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        self.assertIn("Boardmember", result.user_roles)
        self.assertEqual(result.company_data["name"], DNB_FI_COMPANY_NAME)
        self.assertEqual(result.method_used, "dnb_fi")
        # Must hit the RTS endpoint with the user's name + birthDate in
        # signatories[] and signTogether=ANY.
        self.mock_check_rts.assert_called_once_with(
            "fi",
            DNB_FI_REGISTRATION_NUMBER,
            signatories=[
                {
                    "name": {"firstName": "Matti", "lastName": "Virtanen"},
                    "birthDate": "1979-09-19",
                }
            ],
            sign_together="ANY",
            authority_type="DEFAULT",
        )

    def test_unknown_user_is_not_listed(self):
        # Person isn't in any of signatories[]/coSignatories[]/nonSignatories[].
        # Distinct from NOT_AUTHORIZED so the UI can hint at wrong DOB / name.
        self.mock_check_rts.return_value = dnb_rts_finland_response(
            in_signatories=False
        )

        result = self.backend.validate_company(
            self._build_request(DNB_FI_UNKNOWN_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.PERSON_NOT_LISTED)
        self.assertEqual(result.user_roles, [])
        # Company data is still populated so staff can review.
        self.assertEqual(result.company_data["name"], DNB_FI_COMPANY_NAME)

    def test_user_in_non_signatories_is_not_authorized(self):
        self.mock_check_rts.return_value = dnb_rts_finland_response(
            in_signatories=False, in_non_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_FI_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("Boardmember", result.user_roles)

    def test_user_in_co_signatories_is_not_authorized(self):
        # coSignatories require a co-signer; we treat as not-authorized
        # so staff can confirm the multi-signer arrangement.
        self.mock_check_rts.return_value = dnb_rts_finland_response(
            in_signatories=False, in_co_signatories=True
        )

        result = self.backend.validate_company(
            self._build_request(DNB_FI_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.NOT_AUTHORIZED)
        self.assertIn("Boardmember", result.user_roles)

    def test_company_not_found(self):
        self.mock_check_rts.side_effect = DnbError("not found", not_found=True)

        result = self.backend.validate_company(
            self._build_request(DNB_FI_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.COMPANY_NOT_FOUND)

    def test_api_error(self):
        self.mock_check_rts.side_effect = DnbError("500 Internal Server Error")

        result = self.backend.validate_company(
            self._build_request(DNB_FI_AUTHORIZED_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.assertEqual(result.error_code, ErrorCode.API_ERROR)

    def test_authorized_user_gets_credit_data_enrichment(self):
        result = self.backend.validate_company(
            self._build_request(DNB_FI_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        # Nested v3 credit-data fields land on company_data.
        self.assertEqual(result.company_data["address"], "Mannerheimintie 1, Helsinki")
        self.assertEqual(result.company_data["postal"], "00100")
        self.assertEqual(result.company_data["vat_number"], "FI23456789")
        self.assertEqual(result.company_data["number_of_employees"], 18)
        self.mock_get_company.assert_called_once_with("fi", DNB_FI_REGISTRATION_NUMBER)

    def test_unauthorized_user_skips_credit_data_enrichment(self):
        self.mock_check_rts.return_value = dnb_rts_finland_response(
            in_signatories=False
        )

        result = self.backend.validate_company(
            self._build_request(DNB_FI_UNKNOWN_PERSON)
        )

        self.assertFalse(result.is_valid)
        self.mock_get_company.assert_not_called()

    def test_enrichment_failure_does_not_fail_validation(self):
        # RTS already authorized; a credit-data outage must not flip the result.
        self.mock_get_company.side_effect = DnbError("503 Service Unavailable")

        result = self.backend.validate_company(
            self._build_request(DNB_FI_AUTHORIZED_PERSON)
        )

        self.assertTrue(result.is_valid)
        self.assertEqual(result.company_data["name"], DNB_FI_COMPANY_NAME)
        # Enrichment-only fields are absent, but the result still verifies.
        self.assertNotIn("vat_number", result.company_data)

    @mock.patch(
        "waldur_core.onboarding.backends.dnb.client.DnbClient.check_rts",
        side_effect=ValueError("credentials not configured"),
    )
    def test_configuration_error_propagates_as_value_error(self, _):
        with self.assertRaises(ValueError):
            self.backend.validate_company(self._build_request(DNB_FI_AUTHORIZED_PERSON))


class DnbSwedenCustomerCreationTest(TestCase):
    """End-to-end check: VAT number from D&B enrichment lands on the Customer."""

    def test_vat_number_from_verified_data_populates_customer_vat_code(self):
        # No checklists created — automatic-validation flow with no required
        # intent questions, so create_customer_if_verified() proceeds cleanly.
        from waldur_core.onboarding import enums as onboarding_enums

        from . import factories

        verification = factories.OnboardingVerificationFactory(
            country="SE",
            status=onboarding_enums.VerificationStatus.VERIFIED,
            legal_person_identifier=DNB_SE_REGISTRATION_NUMBER,
            legal_name=DNB_SE_COMPANY_NAME,
            validation_method="dnb_se",
            verified_company_data={
                "name": DNB_SE_COMPANY_NAME,
                "legal_person_identifier": DNB_SE_REGISTRATION_NUMBER,
                "registry": "D&B Sweden",
                "address": "Storgatan 1, Stockholm",
                "postal": "11122",
                "vat_number": "SE556002136101",
            },
        )

        customer = verification.create_customer_if_verified()

        self.assertEqual(customer.vat_code, "SE556002136101")
        self.assertEqual(customer.address, "Storgatan 1, Stockholm")
        self.assertEqual(customer.postal, "11122")
        self.assertEqual(customer.registration_code, DNB_SE_REGISTRATION_NUMBER)


class DnbNorwayCustomerCreationTest(TestCase):
    """End-to-end check: VAT number from D&B enrichment lands on the Customer."""

    def test_vat_number_from_verified_data_populates_customer_vat_code(self):
        # No checklists created — automatic-validation flow with no required
        # intent questions, so create_customer_if_verified() proceeds cleanly.
        from waldur_core.onboarding import enums as onboarding_enums

        from . import factories

        verification = factories.OnboardingVerificationFactory(
            country="NO",
            status=onboarding_enums.VerificationStatus.VERIFIED,
            legal_person_identifier=DNB_NO_REGISTRATION_NUMBER,
            legal_name=DNB_NO_COMPANY_NAME,
            validation_method="dnb_no",
            verified_company_data={
                "name": DNB_NO_COMPANY_NAME,
                "legal_person_identifier": DNB_NO_REGISTRATION_NUMBER,
                "registry": "D&B Norway",
                "address": "Karl Johans gate 1, Oslo",
                "postal": "0154",
                "vat_number": "NO987654321MVA",
            },
        )

        customer = verification.create_customer_if_verified()

        self.assertEqual(customer.vat_code, "NO987654321MVA")
        self.assertEqual(customer.address, "Karl Johans gate 1, Oslo")
        self.assertEqual(customer.postal, "0154")
        self.assertEqual(customer.registration_code, DNB_NO_REGISTRATION_NUMBER)


class DnbNorwayValidatorWiringTest(TestCase):
    """End-to-end check on the validator: form-provided first/last/birth_date
    must reach DnbNorwayBackend.validate_company even when the user profile
    has no birth_date. Regression guard for the WirtschaftsCompass-only
    conditional in OnboardingValidator that silently discarded D&B NO form
    input and sent empty strings to D&B (→ "signatories[0].birthDate must
    not be null").
    """

    def test_form_birth_date_reaches_backend_when_profile_is_sparse(self):
        from waldur_core.onboarding.backends.base import (
            ErrorCode as BackendErrorCode,
        )
        from waldur_core.onboarding.backends.base import (
            ValidationResult as BackendValidationResult,
        )
        from waldur_core.onboarding.validators import onboarding_validator

        from . import factories

        # Test user with no birth_date set on the profile — mirrors the real
        # scenario where the wizard rendered the identification form because
        # `hasRequiredFields()` returned false.
        verification = factories.OnboardingVerificationFactory(
            country="NO",
            validation_method="dnb_no",
            legal_person_identifier=DNB_NO_REGISTRATION_NUMBER,
            legal_name=DNB_NO_COMPANY_NAME,
        )
        # Belt-and-braces: make absolutely sure birth_date is empty on the
        # user the validator will see.
        verification.user.first_name = ""
        verification.user.last_name = ""
        verification.user.birth_date = None
        verification.user.save()

        captured: dict = {}

        def _capture(self, request):
            captured["person_identifier"] = request.person_identifier
            return BackendValidationResult(
                is_valid=False,
                method_used="dnb_no",
                company_data={},
                user_roles=[],
                raw_response={},
                error_code=BackendErrorCode.NOT_AUTHORIZED,
                error_message="captured",
            )

        with mock.patch(
            "waldur_core.onboarding.backends.dnb.dnb_norway.DnbNorwayBackend.validate_company",
            _capture,
        ):
            onboarding_validator.validate_company(
                user=verification.user,
                validation_method="dnb_no",
                legal_person_identifier=verification.legal_person_identifier,
                legal_name=verification.legal_name,
                existing_verification=verification,
                person_identifier="",
                first_name="Fire",
                last_name="Testperson",
                birth_date="1960-06-09",
            )

        self.assertEqual(
            captured.get("person_identifier"),
            {
                "first_name": "Fire",
                "last_name": "Testperson",
                "birth_date": "1960-06-09",
            },
        )


class OnboardingVerificationRawResponseVisibilityTest(TestCase):
    """
    `raw_response` holds the unredacted RTS payload (other signatories'
    national IDs / birth dates). It must reach staff only — never the
    regular user who initiated the verification.
    """

    def _fields_for(self, *, is_authenticated, is_staff):
        request = mock.Mock()
        request.user = mock.Mock(is_authenticated=is_authenticated, is_staff=is_staff)
        serializer = OnboardingVerificationSerializer(context={"request": request})
        return serializer.fields

    def test_raw_response_hidden_from_regular_user(self):
        self.assertNotIn(
            "raw_response",
            self._fields_for(is_authenticated=True, is_staff=False),
        )

    def test_raw_response_hidden_from_anonymous_request(self):
        self.assertNotIn(
            "raw_response",
            self._fields_for(is_authenticated=False, is_staff=False),
        )

    def test_raw_response_visible_to_staff(self):
        self.assertIn(
            "raw_response",
            self._fields_for(is_authenticated=True, is_staff=True),
        )

    def test_raw_response_present_in_generated_schema(self):
        # drf-spectacular sets swagger_fake_view; the schema must still
        # advertise every possible field so the SDK shape is unchanged.
        view = mock.Mock(swagger_fake_view=True)
        serializer = OnboardingVerificationSerializer(context={"view": view})
        self.assertIn("raw_response", serializer.fields)
