import logging
import weakref

import requests

from waldur_vmware.exceptions import VMwareError

logger = logging.getLogger(__name__)


def _close_session(session, base_url):
    """Drop the vCenter REST session, then the HTTP connections behind it.

    Called from a weakref finalizer, so it must not raise: by then the session
    may already have expired, or the interpreter may be shutting down. Logging
    out is best effort; closing the pool is not optional and happens either way.
    """
    try:
        session.delete(f"{base_url}/com/vmware/cis/session", timeout=5)
    except Exception:
        logger.debug("Failed to log out of the vCenter REST API.", exc_info=True)
    finally:
        session.close()


class VMwareClient:
    """
    Lightweight VMware vCenter Automation API client for the Content Library.

    Everything else this plugin does goes over vim25 (SOAP) through pyVmomi —
    see :mod:`waldur_vmware.backend`. The Content Library is the exception
    because it has no vim25 equivalent: it is exposed over REST only.

    Note that the `/rest` prefix used here is deprecated in favour of `/api`
    (Broadcom KB 320077). Moving these calls over is a path change and a
    response-envelope change rather than a rewrite, and is tracked separately.

    See also: https://vmware.github.io/vsphere-automation-sdk-rest/vsphere/
    """

    def __init__(self, host, verify_ssl=True):
        """
        Initialize client with connection options.

        :param host: VMware vCenter server IP address / FQDN
        :type host: string
        :param verify_ssl: verify SSL certificates for HTTPS requests
        :type verify_ssl: bool
        """
        self._host = host
        self._base_url = f"https://{self._host}/rest"
        self._session = requests.Session()
        self._session.verify = verify_ssl
        # vCenter keeps a REST session server-side once login() runs. A client
        # built through VMwareBackend is cached and closed by
        # :mod:`waldur_vmware.sessions`; this covers one built outside it, whose
        # session and connection pool would otherwise be released only when
        # vCenter expired them.
        self._finalizer = weakref.finalize(
            self, _close_session, self._session, self._base_url
        )

    def close(self):
        """Log out of vCenter and release the HTTP connection pool.

        Safe to call more than once.
        """
        self._finalizer()

    def _request(self, method, endpoint, json=None, **kwargs):
        url = f"{self._base_url}/{endpoint}"
        if json:
            json = {"spec": json}
        try:
            response = self._session.request(method, url, json=json, **kwargs)
        except requests.RequestException as e:
            raise VMwareError(e)

        status_code = response.status_code
        if status_code in (
            requests.codes.ok,
            requests.codes.created,
            requests.codes.accepted,
            requests.codes.no_content,
        ):
            if response.content:
                data = response.json()
                if isinstance(data, dict) and "value" in data:
                    return data["value"]
                return data
        else:
            raise VMwareError(response.content)

    def _get(self, endpoint, **kwargs):
        return self._request("get", endpoint, **kwargs)

    def _post(self, endpoint, **kwargs):
        return self._request("post", endpoint, **kwargs)

    def login(self, username, password):
        """
        Login to vCenter server using username and password.

        :param username: user to connect
        :type username: string
        :param password: password of the user
        :type password: string
        :raises Unauthorized: raised if credentials are invalid.
        """
        self._post("com/vmware/cis/session", auth=(username, password))
        logger.info(f"Successfully logged in as {username}")

    def is_session_alive(self):
        """Whether vCenter still holds the session this client logged in with.

        A client is cached per process and handed to backends built later, so it
        can outlive its session: vCenter drops a REST session it has not seen
        traffic on for about thirty minutes, and a restart takes all of them.
        Asking the session endpoint about itself answers that in one round trip —
        an expired session gets a 401, which arrives here as a VMwareError. See
        :mod:`waldur_vmware.sessions`.

        The `/rest` binding exposes only create and delete on the session path as
        plain verbs; reading the current session is `?~action=get` on a POST. A
        GET works against vcsim, but a vCenter answering it 405 would make this
        report every session dead and turn the cache into a relogin per minute.
        """
        try:
            return bool(self._post("com/vmware/cis/session?~action=get"))
        except VMwareError:
            logger.debug("The cached REST session did not answer.", exc_info=True)
            return False

    def list_libraries(self):
        return self._get("com/vmware/content/library")

    def list_library_items(self, library_id):
        params = {"library_id": library_id}
        return self._get("com/vmware/content/library/item", params=params)

    def get_library_item(self, library_item_id):
        return self._get(f"com/vmware/content/library/item/id:{library_item_id}")

    def get_template_library_item(self, library_item_id):
        return self._get(f"vcenter/vm-template/library-items/{library_item_id}")

    def list_all_templates(self):
        items = []
        # vCenter answers an empty library set with a null value rather than an
        # empty list, so neither loop can assume it got a sequence.
        for library_id in self.list_libraries() or []:
            for library_item_id in self.list_library_items(library_id) or []:
                library_item = self.get_library_item(library_item_id)
                if library_item["type"] == "vm-template":
                    template = self.get_template_library_item(library_item_id)
                    items.append(
                        {
                            "library_item": library_item,
                            "template": template,
                        }
                    )
        return items

    def deploy_vm_from_template(self, library_item_id, spec):
        """
        Deploys a virtual machine as a copy of the source virtual machine
        template contained in the library item specified by library_item_id.

        :param library_item_id: identifier of the content library item containing the source virtual machine template to be deployed.
        :param spec: deployment specification
        :return: Identifier of the deployed virtual machine.
        :rtype: str
        """
        url = f"vcenter/vm-template/library-items/{library_item_id}?action=deploy"
        return self._post(url, json=spec)
