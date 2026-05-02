from unittest import mock

import pytest
from keystoneauth1 import exceptions as keystoneauth_exceptions
from keystoneauth1.identity import v3

from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.session import (
    PlacementClient,
    create_session,
    get_nova_client,
)


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


@mock.patch("waldur_openstack.session.keystone_session.Session")
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
        public_response = mock.MagicMock()
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
