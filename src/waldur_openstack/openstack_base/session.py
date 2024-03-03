import base64
import hashlib
import logging
import os.path
import tempfile

from cinderclient import exceptions as cinder_exceptions
from cinderclient.v3 import client as cinder_client
from django.core.cache import cache
from glanceclient import exc as glance_exceptions
from glanceclient.v2 import client as glance_client
from keystoneauth1 import exceptions as keystoneauth_exceptions
from keystoneauth1 import session as keystone_session
from keystoneauth1.identity import v3
from keystoneclient import exceptions as keystone_exceptions
from keystoneclient.v3 import client as keystone_client
from neutronclient.client import exceptions as neutron_exceptions
from neutronclient.v2_0 import client as neutron_client
from novaclient import client as nova_client
from novaclient import exceptions as nova_exceptions
from novaclient.v2 import client as nova2_client

from waldur_core.core.utils import QuietSession
from waldur_openstack.openstack_base.exceptions import (
    OpenStackAuthorizationFailed,
    OpenStackBackendError,
)

logger = logging.getLogger(__name__)


SESSION_LIFETIME = 10 * 60 * 60


def get_cached_session_key(credentials: dict[str, str]):
    # copy from https://github.com/openstack/keystoneauth/blob/master/keystoneauth1/identity/base.py#L642
    hasher = hashlib.sha256()

    for k, v in sorted(credentials.items()):
        if v is not None:
            if isinstance(k, str):
                k = k.encode("utf-8")
            if isinstance(v, str):
                v = v.encode("utf-8")

            hasher.update(k)
            hasher.update(v)

    key = base64.b64encode(hasher.digest()).decode("utf-8")

    return "OPENSTACK_SESSION_%s" % key


def get_certificate_filename(data):
    if not isinstance(data, bytes):
        data = data.encode("utf-8")
    cert_hash = hashlib.sha256(data).hexdigest()
    return os.path.join(tempfile.gettempdir(), f"waldur-certificate-{cert_hash}.pem")


def get_credentials(settings, tenant_id=None):
    domain_name = settings.domain or "Default"
    credentials = {
        "auth_url": settings.backend_url,
        "username": settings.username,
        "password": settings.password,
        "user_domain_name": domain_name,
    }
    if tenant_id:
        credentials["project_id"] = tenant_id
    else:
        credentials["project_domain_name"] = domain_name
        credentials["project_name"] = settings.get_option("tenant_name")
    return credentials


def recover_cached_session(cached_session: dict[str, str], verify_ssl=False):
    auth_state = cached_session.pop("auth_state")
    auth_method = v3.Token(**cached_session)
    auth_method.set_auth_state(auth_state)
    return keystone_session.Session(auth=auth_method, verify=verify_ssl)


def create_session(credentials: dict[str, str], verify_ssl=False):
    http_session = None
    if not verify_ssl:
        http_session = QuietSession()
        http_session.verify = False

    ks_session = keystone_session.Session(
        auth=v3.Password(**credentials),
        verify=verify_ssl,
        session=http_session,
    )

    try:
        # This will eagerly sign in throwing AuthorizationFailure on bad credentials
        ks_session.get_auth_headers()
    except keystone_exceptions.ClientException as e:
        raise OpenStackAuthorizationFailed(e)

    return ks_session


def get_verify_ssl(settings):
    verify_ssl = settings.get_option("verify_ssl")
    client_cert = settings.get_option("certificate")
    if client_cert:
        file_path = get_certificate_filename(client_cert)
        if not os.path.isfile(file_path):
            with open(file_path, "w") as fh:
                fh.write(client_cert)
        verify_ssl = file_path
    return verify_ssl


def get_cached_session(session: keystone_session.Session):
    cached_session = {}
    for key in (
        "auth_url",
        "project_id",
        "project_name",
        "project_domain_name",
    ):
        cached_session[key] = getattr(session.auth, key)
    cached_session["token"] = session.auth.auth_ref.auth_token
    cached_session["auth_state"] = session.auth.get_auth_state()
    return cached_session


def get_keystone_session(settings, tenant_id=None):
    credentials = get_credentials(settings, tenant_id)
    verify_ssl = get_verify_ssl(settings)

    session_key = get_cached_session_key(credentials)
    cached_session = cache.get(session_key)
    # try to get session from cache
    try:
        logger.debug("Trying to recover OpenStack session.")
        recovered_session = recover_cached_session(cached_session, verify_ssl)
        # validate if cached session is valid
        recovered_session.get_auth_headers()
        return recovered_session
    except (AttributeError, IndexError, keystoneauth_exceptions.ClientException):
        logger.warning("Unable to recover OpenStack session, deleting cache.")
        cache.delete(session_key)
        pass

    # create new token if session is not cached or expired
    ks_session = create_session(credentials, verify_ssl)
    cached_session = get_cached_session(ks_session)

    cache.set(session_key, cached_session, SESSION_LIFETIME)
    return ks_session


def get_keystone_client(session):
    return keystone_client.Client(session=session, interface="public")


def get_nova_client(session: keystone_session.Session) -> "nova2_client.Client":
    try:
        return nova_client.Client(
            version="2.19",
            session=session,
            endpoint_type="publicURL",
        )
    except nova_exceptions.ClientException as e:
        logger.exception("Failed to create nova client: %s", e)
        raise OpenStackBackendError(e)


def get_neutron_client(session: keystone_session.Session):
    try:
        return neutron_client.Client(session=session)
    except neutron_exceptions.NeutronClientException as e:
        logger.exception("Failed to create neutron client: %s", e)
        raise OpenStackBackendError(e)


def get_cinder_client(session: keystone_session.Session):
    try:
        return cinder_client.Client(session=session)
    except cinder_exceptions.ClientException as e:
        logger.exception("Failed to create cinder client: %s", e)
        raise OpenStackBackendError(e)


def get_glance_client(session: keystone_session.Session):
    try:
        return glance_client.Client(session=session)
    except glance_exceptions.ClientException as e:
        logger.exception("Failed to create glance client: %s", e)
        raise OpenStackBackendError(e)
