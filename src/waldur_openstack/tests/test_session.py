import logging
from unittest import mock

import pytest
from constance.test.unittest import override_config
from keystoneauth1 import exceptions as keystoneauth_exceptions
from keystoneauth1 import session as keystone_session
from keystoneauth1.identity import v3
from keystoneauth1.session import TCPKeepAliveAdapter

from waldur_core.core.utils import QuietSession
from waldur_core.structure.backend import current_backend_action
from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.session import (
    PlacementClient,
    TimedSession,
    create_session,
    get_nova_client,
    recover_cached_session,
)

CALLS_LOGGER = "waldur_openstack.calls"


@pytest.fixture
def password_credentials():
    return {
        "auth_url": "https://keystone.example.com:5000/v3",
        "username": "admin",
        "password": "secret",
        "user_domain_name": "Default",
        "project_domain_name": "Default",
        "project_name": "admin",
    }


@mock.patch("waldur_openstack.session.TimedSession")
class TestCreateSessionAuthType:
    def test_default_auth_type_uses_password(
        self, mock_session_class, password_credentials
    ):
        mock_session_class.return_value.get_auth_headers.return_value = {
            "X-Auth-Token": "token"
        }

        create_session(password_credentials)

        auth_arg = mock_session_class.call_args[1]["auth"]
        assert isinstance(auth_arg, v3.Password)

    def test_explicit_password_auth_type(
        self, mock_session_class, password_credentials
    ):
        mock_session_class.return_value.get_auth_headers.return_value = {
            "X-Auth-Token": "token"
        }
        password_credentials["auth_type"] = "password"

        create_session(password_credentials)

        auth_arg = mock_session_class.call_args[1]["auth"]
        assert isinstance(auth_arg, v3.Password)

    def test_application_credential_auth_type(
        self, mock_session_class, password_credentials
    ):
        mock_session_class.return_value.get_auth_headers.return_value = {
            "X-Auth-Token": "token"
        }
        password_credentials["auth_type"] = "v3applicationcredential"

        create_session(password_credentials)

        auth_arg = mock_session_class.call_args[1]["auth"]
        assert isinstance(auth_arg, v3.ApplicationCredential)

    def test_application_credential_maps_username_to_id(
        self, mock_session_class, password_credentials
    ):
        mock_session_class.return_value.get_auth_headers.return_value = {
            "X-Auth-Token": "token"
        }
        password_credentials["auth_type"] = "v3applicationcredential"
        password_credentials["username"] = "my-app-cred-id"
        password_credentials["password"] = "my-app-cred-secret"

        create_session(password_credentials)

        auth_arg = mock_session_class.call_args[1]["auth"]
        auth_method = auth_arg.auth_methods[0]
        assert auth_method.application_credential_id == "my-app-cred-id"
        assert auth_method.application_credential_secret == "my-app-cred-secret"

    def test_invalid_auth_type_raises_error(
        self, mock_session_class, password_credentials
    ):
        password_credentials["auth_type"] = "invalid"

        with pytest.raises(OpenStackBackendError, match="Unsupported auth_type"):
            create_session(password_credentials)

    def test_auth_type_not_passed_to_password_plugin(
        self, mock_session_class, password_credentials
    ):
        """auth_type must be popped from credentials before passing to v3.Password."""
        mock_session_class.return_value.get_auth_headers.return_value = {
            "X-Auth-Token": "token"
        }
        password_credentials["auth_type"] = "password"

        create_session(password_credentials)

        auth_arg = mock_session_class.call_args[1]["auth"]
        assert isinstance(auth_arg, v3.Password)


@mock.patch("waldur_openstack.session.nova_client.Client")
def test_get_nova_client_pins_microversion_2_87(mock_nova_client_class):
    # Pinning is load-bearing: 2.87 enables volume-backed rescue, while 2.88
    # removes capacity fields from /os-hypervisors/* that pull_hypervisors and
    # pull_service_settings_quotas consume. Negotiating "latest" would silently
    # zero hypervisor capacity on any cloud advertising 2.88+.
    session = mock.MagicMock()
    get_nova_client(session)
    assert mock_nova_client_class.call_args.kwargs["version"] == "2.87"


class TestPlacementClient:
    def _make_session(self, response=None, raise_=None):
        session = mock.MagicMock()
        if raise_ is not None:
            session.get.side_effect = raise_
        else:
            response = response or mock.MagicMock()
            response.json.return_value = response.json.return_value or {}
            session.get.return_value = response
        return session

    def test_falls_back_from_public_to_internal_interface(self):
        """Some clouds register Placement only on the internal interface."""
        internal_response = mock.MagicMock()
        internal_response.json.return_value = {"resource_providers": [{"uuid": "x"}]}
        session = mock.MagicMock()
        # First call (public) raises EndpointNotFound, second (internal) succeeds.
        session.get.side_effect = [
            keystoneauth_exceptions.EndpointNotFound("no public placement"),
            internal_response,
        ]

        client = PlacementClient(session)
        result = client.list_resource_providers()

        assert result == [{"uuid": "x"}]
        assert session.get.call_count == 2
        assert (
            session.get.call_args_list[0].kwargs["endpoint_filter"]["interface"]
            == "public"
        )
        assert (
            session.get.call_args_list[1].kwargs["endpoint_filter"]["interface"]
            == "internal"
        )

    def test_raises_when_placement_missing_in_both_interfaces(self):
        """If Placement isn't registered at all, raise rather than return [].
        Returning empty would silently zero hypervisor capacity in the DB."""
        session = mock.MagicMock()
        session.get.side_effect = keystoneauth_exceptions.EndpointNotFound("missing")

        client = PlacementClient(session)
        with pytest.raises(OpenStackBackendError, match="Placement service"):
            client.list_resource_providers()

    def test_member_of_query_param_is_set_for_aggregate_filter(self):
        response = mock.MagicMock()
        response.json.return_value = {"resource_providers": []}
        session = mock.MagicMock()
        session.get.return_value = response

        client = PlacementClient(session)
        client.list_resource_providers(member_of=["agg-1", "agg-2"])

        params = session.get.call_args.kwargs["params"]
        assert params == {"member_of": "in:agg-1,agg-2"}

    def test_get_inventories_returns_inventories_dict(self):
        response = mock.MagicMock()
        response.json.return_value = {
            "inventories": {"VCPU": {"total": 40, "reserved": 0}}
        }
        session = mock.MagicMock()
        session.get.return_value = response

        client = PlacementClient(session)
        result = client.get_inventories("rp-uuid")

        assert result == {"VCPU": {"total": 40, "reserved": 0}}
        assert "/resource_providers/rp-uuid/inventories" in session.get.call_args.args

    def test_get_traits_returns_traits_list(self):
        response = mock.MagicMock()
        response.json.return_value = {
            "traits": ["HW_CPU_X86_AVX2", "STORAGE_DISK_SSD", "CUSTOM_GOLD"],
            "resource_provider_generation": 7,
        }
        session = mock.MagicMock()
        session.get.return_value = response

        client = PlacementClient(session)
        result = client.get_traits("rp-uuid")

        assert result == ["HW_CPU_X86_AVX2", "STORAGE_DISK_SSD", "CUSTOM_GOLD"]
        assert "/resource_providers/rp-uuid/traits" in session.get.call_args.args

    def test_get_traits_returns_empty_list_when_absent(self):
        response = mock.MagicMock()
        response.json.return_value = {}
        session = mock.MagicMock()
        session.get.return_value = response

        client = PlacementClient(session)
        assert client.get_traits("rp-uuid") == []

    def test_pinned_microversion_header_is_sent(self):
        # Without this header Placement defaults to microversion 1.0 and
        # silently drops parent_provider_uuid/traits/aggregates that downstream
        # code needs. Lab probe (May 2026) confirmed the fields appear only
        # when the header is present.
        response = mock.MagicMock()
        response.json.return_value = {"resource_providers": []}
        session = mock.MagicMock()
        session.get.return_value = response

        client = PlacementClient(session)
        client.list_resource_providers()

        headers = session.get.call_args.kwargs["headers"]
        assert headers["OpenStack-API-Version"] == "placement 1.36"


def _make_response(status_code=200, url=None):
    resp = mock.MagicMock()
    resp.status_code = status_code
    resp.url = url
    return resp


def _calls_records(caplog):
    return [r for r in caplog.records if r.name == CALLS_LOGGER]


@pytest.fixture
def patched_parent_request():
    """Patch keystoneauth1.session.Session.request so super().request() is mocked."""
    with mock.patch.object(keystone_session.Session, "request") as m:
        yield m


@pytest.fixture
def deterministic_clock():
    """Patch time.perf_counter in session module to a controllable iterator."""
    with mock.patch("waldur_openstack.session.time.perf_counter") as m:
        yield m


@pytest.fixture(autouse=True)
def reset_backend_action():
    token = current_backend_action.set(None)
    try:
        yield
    finally:
        current_backend_action.reset(token)


@pytest.mark.django_db
class TestTimedSession:
    """Direct tests for TimedSession.request logging branches.

    super().request is patched at the parent class so no real HTTP is involved;
    time.perf_counter is patched so threshold gating is deterministic.
    """

    def _set_elapsed_ms(self, clock_mock, ms):
        clock_mock.side_effect = [1000.0, 1000.0 + ms / 1000.0]

    @override_config(OPENSTACK_LOG_CALLS_ENABLED=False)
    def test_disabled_constance_suppresses_success_log(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        patched_parent_request.return_value = _make_response(
            200, "https://nova/v2.1/servers"
        )
        self._set_elapsed_ms(deterministic_clock, 250)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            session.request("/v2.1/servers", "GET")

        assert _calls_records(caplog) == []

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=True, OPENSTACK_LOG_CALLS_THRESHOLD_MS=100
    )
    def test_enabled_above_threshold_logs_success(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        patched_parent_request.return_value = _make_response(
            200, "https://nova/v2.1/servers"
        )
        self._set_elapsed_ms(deterministic_clock, 250)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            session.request("/v2.1/servers", "GET")

        records = _calls_records(caplog)
        assert len(records) == 1
        record = records[0]
        assert record.openstack_status == 200
        assert record.openstack_method == "GET"
        assert record.openstack_duration_ms == pytest.approx(250.0, abs=1.0)
        assert record.openstack_error is None

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=True, OPENSTACK_LOG_CALLS_THRESHOLD_MS=500
    )
    def test_enabled_below_threshold_suppresses_success_log(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        patched_parent_request.return_value = _make_response(
            200, "https://nova/v2.1/servers"
        )
        self._set_elapsed_ms(deterministic_clock, 100)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            session.request("/v2.1/servers", "GET")

        assert _calls_records(caplog) == []

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=False, OPENSTACK_LOG_CALLS_THRESHOLD_MS=10_000
    )
    def test_4xx_logs_even_when_disabled(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        # SDK clients (nova/cinder/...) pass raise_exc=False, so 4xx returns
        # as a status code rather than as a raised exception. Errors must log
        # regardless of ENABLED or THRESHOLD.
        patched_parent_request.return_value = _make_response(
            404, "https://nova/v2.1/servers/missing"
        )
        self._set_elapsed_ms(deterministic_clock, 5)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            session.request("/v2.1/servers/missing", "GET")

        records = _calls_records(caplog)
        assert len(records) == 1
        assert records[0].openstack_status == 404
        assert records[0].openstack_error is None

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=False, OPENSTACK_LOG_CALLS_THRESHOLD_MS=10_000
    )
    def test_5xx_logs_even_when_disabled(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        patched_parent_request.return_value = _make_response(
            503, "https://nova/v2.1/servers"
        )
        self._set_elapsed_ms(deterministic_clock, 5)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            session.request("/v2.1/servers", "GET")

        records = _calls_records(caplog)
        assert len(records) == 1
        assert records[0].openstack_status == 503

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=False, OPENSTACK_LOG_CALLS_THRESHOLD_MS=10_000
    )
    def test_exception_logs_even_when_disabled(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        patched_parent_request.side_effect = keystoneauth_exceptions.NotFound(
            "missing", http_status=404
        )
        self._set_elapsed_ms(deterministic_clock, 5)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            with pytest.raises(keystoneauth_exceptions.NotFound):
                session.request("/v2.1/servers/missing", "GET")

        records = _calls_records(caplog)
        assert len(records) == 1
        assert records[0].openstack_error == "NotFound"
        assert records[0].openstack_status == 404

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=True, OPENSTACK_LOG_CALLS_THRESHOLD_MS=0
    )
    def test_query_string_is_stripped_from_logged_url(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        patched_parent_request.return_value = _make_response(
            200,
            "https://nova:8774/v2.1/servers?marker=abc&fields=name",
        )
        self._set_elapsed_ms(deterministic_clock, 5)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            session.request("/v2.1/servers", "GET")

        records = _calls_records(caplog)
        assert len(records) == 1
        assert records[0].openstack_url == "https://nova:8774/v2.1/servers"
        assert "?" not in records[0].getMessage()

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=True, OPENSTACK_LOG_CALLS_THRESHOLD_MS=0
    )
    def test_relative_url_falls_back_to_caller_arg_with_query_stripped(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        # No response.url available (e.g. transport error path); the caller's
        # relative URL is used and its query string is still stripped.
        patched_parent_request.return_value = _make_response(200, url=None)
        self._set_elapsed_ms(deterministic_clock, 5)
        session = TimedSession()

        with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
            session.request("/v2.1/servers?marker=abc", "GET")

        records = _calls_records(caplog)
        assert len(records) == 1
        assert records[0].openstack_url == "/v2.1/servers"

    def test_constance_failure_does_not_break_call(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        # Constance can fail during boot (DB unavailable, migration mid-flight).
        # The HTTP call must still succeed, and the success path stays quiet —
        # no log line if we can't even tell whether logging is enabled.
        response = _make_response(200, "https://nova/v2.1/servers")
        patched_parent_request.return_value = response
        self._set_elapsed_ms(deterministic_clock, 5)

        broken_config = mock.MagicMock()
        type(broken_config).OPENSTACK_LOG_CALLS_ENABLED = mock.PropertyMock(
            side_effect=RuntimeError("db unavailable")
        )

        session = TimedSession()
        with mock.patch("waldur_openstack.session.config", broken_config):
            with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
                result = session.request("/v2.1/servers", "GET")

        assert result is response
        assert _calls_records(caplog) == []

    @override_config(
        OPENSTACK_LOG_CALLS_ENABLED=True, OPENSTACK_LOG_CALLS_THRESHOLD_MS=0
    )
    def test_action_contextvar_is_captured(
        self, patched_parent_request, deterministic_clock, caplog
    ):
        patched_parent_request.return_value = _make_response(
            200, "https://neutron/v2.0/routers"
        )
        self._set_elapsed_ms(deterministic_clock, 5)
        session = TimedSession()

        token = current_backend_action.set("delete tenant routers")
        try:
            with caplog.at_level(logging.INFO, logger=CALLS_LOGGER):
                session.request("/v2.0/routers", "DELETE")
        finally:
            current_backend_action.reset(token)

        records = _calls_records(caplog)
        assert len(records) == 1
        assert records[0].openstack_action == "delete tenant routers"
        assert "action=delete tenant routers" in records[0].getMessage()


class TestSessionTlsWarnings:
    """Both session-construction paths must build a QuietSession transport when
    TLS verification is off, so InsecureRequestWarning is suppressed structurally
    rather than per call site (issue #66). The warm-token path
    (recover_cached_session) previously supplied no transport and floods the logs.
    """

    def _assert_quiet_transport(self, session):
        transport = session.session
        assert isinstance(transport, QuietSession)
        # The TCPKeepAliveAdapter mounts (LP#1323862) that keystoneauth applies
        # to its own default transport must be re-applied to ours.
        assert transport.adapters
        assert all(
            isinstance(adapter, TCPKeepAliveAdapter)
            for adapter in transport.adapters.values()
        )
        # We built the transport, so keystoneauth's del-time close must own it
        # (LP#1838704) or the socket leaks.
        assert session._session is transport

    @mock.patch.object(
        keystone_session.Session,
        "get_auth_headers",
        return_value={"X-Auth-Token": "token"},
    )
    def test_create_session_uses_quiet_transport_when_unverified(
        self, _mock_headers, password_credentials
    ):
        session = create_session(password_credentials, verify_ssl=False)
        self._assert_quiet_transport(session)

    @mock.patch.object(
        keystone_session.Session,
        "get_auth_headers",
        return_value={"X-Auth-Token": "token"},
    )
    def test_create_session_plain_transport_when_verified(
        self, _mock_headers, password_credentials
    ):
        session = create_session(password_credentials, verify_ssl=True)
        assert not isinstance(session.session, QuietSession)

    @mock.patch.object(
        keystone_session.Session,
        "get_auth_headers",
        return_value={"X-Auth-Token": "token"},
    )
    def test_create_session_plain_transport_when_verified_with_ca_path(
        self, _mock_headers, password_credentials
    ):
        # get_verify_ssl returns a CA file path (str) for the certificate
        # branch; a truthy non-bool verify must still not build a QuietSession.
        session = create_session(password_credentials, verify_ssl="/path/ca.pem")
        assert not isinstance(session.session, QuietSession)

    @mock.patch("waldur_openstack.session.v3.Token")
    def test_recover_cached_session_uses_quiet_transport_when_unverified(
        self, _mock_token
    ):
        session = recover_cached_session(
            {"auth_state": "state", "token": "token"}, verify_ssl=False
        )
        self._assert_quiet_transport(session)

    @mock.patch("waldur_openstack.session.v3.Token")
    def test_recover_cached_session_plain_transport_when_verified(self, _mock_token):
        session = recover_cached_session(
            {"auth_state": "state", "token": "token"}, verify_ssl=True
        )
        assert not isinstance(session.session, QuietSession)
