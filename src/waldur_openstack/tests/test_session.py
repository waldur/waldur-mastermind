from unittest import mock

import pytest
from keystoneauth1.identity import v3

from waldur_openstack.exceptions import OpenStackBackendError
from waldur_openstack.session import create_session, get_nova_client


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
