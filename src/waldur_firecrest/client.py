import threading
from http.cookiejar import DefaultCookiePolicy
from urllib.parse import urlsplit

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DEFAULT_TIMEOUT = 30

# Reads are retried when FirecREST cannot be reached or reports that it, or
# the cluster behind it, is briefly unavailable. Writes never are: a submit
# whose response was lost may still have queued the job. Read timeouts are
# not retried either, since each attempt already waits DEFAULT_TIMEOUT.
RETRY = Retry(
    total=2,
    connect=2,
    read=0,
    status=2,
    backoff_factor=0.5,
    status_forcelist=(503, 504),
    allowed_methods=frozenset({"GET"}),
    respect_retry_after_header=True,
    raise_on_status=False,
)

# FirecREST v2 maps its internal errors to these statuses
# (lib/handlers/api_response_handler.py); each gets a remedy the user can act on.
STATUS_EXPLANATIONS = {
    401: "FirecREST did not accept your identity provider token",
    403: "The cluster refused this operation for your account",
    413: "The output is too large for FirecREST to return",
    424: "FirecREST could not log in to the cluster as you. Your cluster account "
    "may not be ready yet, or SSH access to the login node has not been granted",
    502: "FirecREST could not reach the cluster or its SSH credential service",
    503: "FirecREST or the cluster is temporarily unavailable",
    504: "The cluster did not answer FirecREST in time",
}

_sessions = threading.local()


def get_session(api_url) -> requests.Session:
    """A pooled session per FirecREST instance, one per thread.

    A workspace page makes several FirecREST calls; reusing the connection
    saves a TLS handshake on each. ``requests.Session`` is not thread-safe,
    so every worker thread keeps its own. The session serves every user, so
    it keeps no cookies: one a gateway sets for one user's call would
    otherwise go out with the next user's.
    """
    sessions = getattr(_sessions, "by_origin", None)
    if sessions is None:
        sessions = _sessions.by_origin = {}
    parts = urlsplit(api_url)
    origin = f"{parts.scheme}://{parts.netloc}"
    session = sessions.get(origin)
    if session is None:
        session = requests.Session()
        session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        adapter = HTTPAdapter(max_retries=RETRY)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        sessions[origin] = session
    return session


class FirecrestException(Exception):
    """A failed FirecREST call.

    ``sign_in_provider`` is set when FirecREST rejected the token, so the UI
    can offer a fresh sign-in through the identity provider that issued it.
    """

    def __init__(self, message, status_code=None, sign_in_provider=None):
        super().__init__(message)
        self.status_code = status_code
        self.sign_in_provider = sign_in_provider


class FirecrestClient:
    """
    Client for the FirecREST v2 API, acting with the access token of one user.

    FirecREST resolves the POSIX user from the token itself, so the client
    never names the user: every call runs as whoever the token belongs to.
    https://eth-cscs.github.io/firecrest-v2/
    """

    def __init__(
        self, api_url, access_token, timeout=DEFAULT_TIMEOUT, sign_in_provider=None
    ):
        self.api_url = api_url.rstrip("/")
        self.timeout = timeout
        self.headers = {"Authorization": f"Bearer {access_token}"}
        self.sign_in_provider = sign_in_provider
        self.session = get_session(self.api_url)

    def _request(self, method, path, **kwargs):
        try:
            response = self.session.request(
                method,
                self.api_url + path,
                headers=self.headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.exceptions.Timeout:
            raise FirecrestException(
                f"FirecREST did not answer within {self.timeout} seconds.",
                status_code=504,
            )
        except requests.exceptions.RequestException as e:
            raise FirecrestException(f"Unable to reach FirecREST: {e}")
        if not response.ok:
            raise FirecrestException(
                self._error_message(response),
                status_code=response.status_code,
                sign_in_provider=self.sign_in_provider
                if response.status_code == 401
                else None,
            )
        if not response.content:
            return {}
        return response.json()

    @staticmethod
    def _error_message(response):
        try:
            data = response.json()
        except ValueError:
            detail = response.reason
        else:
            detail = (
                data.get("message") or data.get("detail") or data
                if isinstance(data, dict)
                else data
            )
        explanation = STATUS_EXPLANATIONS.get(response.status_code)
        if explanation:
            return f"{explanation} (FirecREST {response.status_code}: {detail})."
        return f"FirecREST returned {response.status_code}: {detail}"

    # Status

    def list_systems(self):
        return self._request("get", "/status/systems")["systems"]

    def list_partitions(self, system):
        return self._request("get", f"/status/{system}/partitions")["partitions"]

    def list_nodes(self, system):
        return self._request("get", f"/status/{system}/nodes")["nodes"]

    def list_reservations(self, system):
        return self._request("get", f"/status/{system}/reservations")["reservations"]

    def get_userinfo(self, system):
        return self._request("get", f"/status/{system}/userinfo")

    # Compute

    def list_jobs(self, system, account=None, time_window=None):
        params = {}
        if account:
            params["account"] = account
        if time_window:
            params["time_window"] = time_window
        return self._request("get", f"/compute/{system}/jobs", params=params)["jobs"]

    def get_job(self, system, job_id):
        jobs = self._request("get", f"/compute/{system}/jobs/{job_id}")["jobs"]
        if not jobs:
            raise FirecrestException(f"Job {job_id} is not found.", status_code=404)
        return jobs[0]

    def get_job_metadata(self, system, job_id):
        metadata = self._request("get", f"/compute/{system}/jobs/{job_id}/metadata")[
            "jobs"
        ]
        if not metadata:
            raise FirecrestException(f"Job {job_id} is not found.", status_code=404)
        return metadata[0]

    def submit_job(self, system, job):
        """Submit a job description (FirecREST JobDescriptionModel); returns the job id."""
        response = self._request("post", f"/compute/{system}/jobs", json={"job": job})
        return str(response["jobId"])

    def cancel_job(self, system, job_id):
        self._request("delete", f"/compute/{system}/jobs/{job_id}")

    # Filesystem

    def list_directory(self, system, path, show_hidden=False):
        params = {"path": path, "showHidden": show_hidden}
        return (
            self._request("get", f"/filesystem/{system}/ops/ls", params=params)[
                "output"
            ]
            or []
        )

    def tail_file(self, system, path, lines=100):
        params = {"path": path, "lines": lines}
        output = self._request("get", f"/filesystem/{system}/ops/tail", params=params)[
            "output"
        ]
        return output or {}

    def view_file(self, system, path, size=None, offset=None):
        params = {"path": path}
        if size is not None:
            params["size"] = size
        if offset is not None:
            params["offset"] = offset
        return (
            self._request("get", f"/filesystem/{system}/ops/view", params=params)[
                "output"
            ]
            or ""
        )

    def upload_file(self, system, path, file_name, content):
        """Upload a small file (up to FirecREST's max_ops_file_size) into directory *path*."""
        self._request(
            "post",
            f"/filesystem/{system}/ops/upload",
            params={"path": path},
            files={"file": (file_name, content)},
        )
