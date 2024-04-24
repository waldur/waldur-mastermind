import collections
import json
import os
import re
import unicodedata

from jira import JIRA, JIRAError, utils
from jira.resources import Attachment, Customer, Issue, RequestType, ServiceDesk, User
from jira.utils import json_loads
from requests import Request
from rest_framework import status

PADDING = 3
CHARS_LIMIT = 255


def _get_filename(path):
    # JIRA does not support composite symbols from Latin-1 charset.
    # Hence we need to use NFD normalization which translates
    # each character into its decomposed form.
    path = unicodedata.normalize("NFD", path)
    limit = CHARS_LIMIT - PADDING
    fname = os.path.basename(path)
    filename = fname.split(".")[0]
    filename_extension = fname.split(".")[1:]
    count = (
        len(".".join(filename_extension).encode("utf-8")) + 1
        if filename_extension
        else 0
    )
    char_limit = 0

    for char in filename:
        count += len(char.encode("utf-8"))
        if count > limit:
            break
        else:
            char_limit += 1

    if not char_limit:
        raise JIRAError("Attachment filename is very long.")

    tmp = [filename[:char_limit]]
    tmp.extend(filename_extension)
    filename = ".".join(tmp)
    return filename


def _upload_file(manager, issue, upload_file, filename):
    # This method will fix original method jira.JIRA.add_attachment (jira/client.py line 591)
    url = manager._get_url("issue/" + str(issue) + "/attachments")
    files = {
        "file": (filename, upload_file),
    }
    headers = {
        "X-Atlassian-Token": "no-check",
    }
    req = Request("POST", url, headers=headers, files=files, auth=manager._session.auth)
    prepped = req.prepare()
    prepped.body = re.sub(
        b"filename=.*", b'filename="%s"\r' % filename.encode("utf-8"), prepped.body
    )
    r = manager._session.send(prepped)

    js = utils.json_loads(r)

    if not js or not isinstance(js, collections.abc.Iterable):
        raise JIRAError("Unable to parse JSON: %s" % js)

    attachment = Attachment(manager._options, manager._session, js[0])

    if attachment.size == 0:
        raise JIRAError(f"Added empty attachment?!: r: {r}\nattachment: {attachment}")

    return attachment


def add_attachment(manager, issue, file):
    """
    Replace jira's method 'add_attachment' while don't well fixed this issue
    https://github.com/shazow/urllib3/issues/303
    And we need to set filename limit equaled 252 chars.
    :param manager: [jira.JIRA instance]
    :param issue: [jira.JIRA.resources.Issue instance]
    :param path: [string]
    :return: [jira.JIRA.resources.Attachment instance]
    """
    filename = _get_filename(file.name)
    return _upload_file(manager, issue, file.file.read(), filename)


def service_desk(manager, id_or_key):
    """In Jira v8.7.1 / SD 4.7.1 a Service Desk ID must be an integer.
    We use a hackish workaround to make it work till Atlassian resolves bug
    https://jira.atlassian.com/browse/JSDSERVER-4877.
    """
    try:
        return manager.service_desk(id_or_key)
    except JIRAError as e:
        if "java.lang.NumberFormatException" in e.text:
            service_desks = [
                sd for sd in manager.service_desks() if sd.projectKey == id_or_key
            ]
            if len(service_desks):
                return service_desks[0]
            else:
                msg = f"The Service Desk with ID {id_or_key} does not exist."
                raise JIRAError(text=msg, status_code=404)
        else:
            raise e


def request_types(manager, service_desk, project_key=None, strange_setting=None):
    """We need to use this function because in the old Jira version issueTypeId field does not exist."""
    types = manager.request_types(service_desk)

    if len(types) and not hasattr(types[0], "issueTypeId"):
        if hasattr(service_desk, "id"):
            service_desk = service_desk.id

        url = (
            manager._options["server"]
            + f"/rest/servicedesk/{strange_setting}/servicedesk/{project_key.lower()}/groups/{service_desk}/request-types"
        )
        headers = {"X-ExperimentalApi": "opt-in"}
        r_json = json_loads(manager._session.get(url, headers=headers))
        types = [
            RequestType(manager._options, manager._session, raw_type_json)
            for raw_type_json in r_json
        ]
        list(map(lambda t: setattr(t, "issueTypeId", t.issueType), types))

    return types


def request_type_fields(manager, service_desk, request_type_id):
    url = (
        manager._options["server"]
        + f"/rest/servicedeskapi/servicedesk/{service_desk.id}/requesttype/{request_type_id}/field"
    )
    headers = {"X-ExperimentalApi": "opt-in"}
    r_json = json_loads(manager._session.get(url, headers=headers))
    return r_json.get("requestTypeFields")


def search_users(
    self, query, startAt=0, maxResults=50, includeActive=True, includeInactive=False
):
    """Get a list of user Resources that match the specified search string. Use query instead of
    username field for lookups.
    """
    params = {
        "query": query,
        "includeActive": includeActive,
        "includeInactive": includeInactive,
    }
    try:
        return self._fetch_pages(User, None, "user/search", startAt, maxResults, params)
    except JIRAError as e:
        if e.text == "The username query parameter was not provided":
            params = {
                "username": query,
                "includeActive": includeActive,
                "includeInactive": includeInactive,
            }
            return self._fetch_pages(
                User, None, "user/search", startAt, maxResults, params
            )
        raise e


def create_customer_request(
    self, fields=None, prefetch=True, use_old_api=False, **fieldargs
):
    """The code for this function is almost completely copied from
    function create_customer_request of the JIRA library"""
    data = fields

    p = data["serviceDeskId"]
    service_desk = None

    if isinstance(p, str) or isinstance(p, int):
        service_desk = self.service_desk(p)
    elif isinstance(p, ServiceDesk):
        service_desk = p

    data["serviceDeskId"] = service_desk.id

    p = data["requestTypeId"]
    if isinstance(p, int):
        data["requestTypeId"] = p
    elif isinstance(p, str):
        data["requestTypeId"] = self.request_type_by_name(service_desk, p).id

    requestParticipants = data.pop("requestParticipants", None)

    url = self._options["server"] + "/rest/servicedeskapi/request"
    headers = {"X-ExperimentalApi": "opt-in"}
    r = self._session.post(url, headers=headers, data=json.dumps(data))

    raw_issue_json = json_loads(r)
    if "issueKey" not in raw_issue_json:
        raise JIRAError(r.status_code, request=r)

    if requestParticipants:
        url = (
            self._options["server"]
            + "/rest/servicedeskapi/request/%s/participant" % raw_issue_json["issueKey"]
        )
        headers = {"X-ExperimentalApi": "opt-in"}

        if use_old_api:
            data = {"usernames": requestParticipants}
        else:
            data = {"accountIds": requestParticipants}

        r = self._session.post(url, headers=headers, json=data)

    if r.status_code != status.HTTP_200_OK:
        raise JIRAError(r.status_code, request=r)

    if prefetch:
        return self.issue(raw_issue_json["issueKey"])
    else:
        return Issue(self._options, self._session, raw=raw_issue_json)


def create_customer(self, email, displayName):
    """Create a new customer and return an issue Resource for it."""
    url = self._options["server"] + "/rest/servicedeskapi/customer"
    headers = {"X-ExperimentalApi": "opt-in"}
    r = self._session.post(
        url,
        headers=headers,
        data=json.dumps(
            {
                "email": email,
                "fullName": displayName,  # different property for the server one
            }
        ),
    )

    raw_customer_json = json_loads(r)

    if r.status_code != 201:
        raise JIRAError(r.status_code, request=r)
    return Customer(self._options, self._session, raw=raw_customer_json)


JIRA.waldur_add_attachment = add_attachment
JIRA.waldur_service_desk = service_desk
JIRA.waldur_request_types = request_types
JIRA.waldur_request_type_fields = request_type_fields
JIRA.waldur_search_users = search_users
JIRA.waldur_create_customer_request = create_customer_request
JIRA.waldur_create_customer = create_customer
