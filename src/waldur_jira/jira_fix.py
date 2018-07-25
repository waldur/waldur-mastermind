import collections
import os
import re

from jira import JIRA, JIRAError, utils
from jira.resources import Attachment
from requests import Request

PADDING = 3
CHARS_LIMIT = 255


def _get_filename(path):
    limit = CHARS_LIMIT - PADDING
    fname = os.path.basename(path)
    filename = fname.split('.')[0]
    filename_extension = fname.split('.')[1:]
    count = len('.'.join(filename_extension).encode('utf-8')) + 1 if filename_extension else 0
    char_limit = 0

    for char in filename:
        count += len(char.encode('utf-8'))
        if count > limit:
            break
        else:
            char_limit += 1

    if not char_limit:
        raise JIRAError('Attachment filename is very long.')

    tmp = [filename[:char_limit]]
    tmp.extend(filename_extension)
    filename = '.'.join(tmp)
    return filename


def _upload_file(manager, issue, upload_file, filename):
    # This method will fix original method jira.JIRA.add_attachment (jira/client.py line 591)
    url = manager._get_url('issue/' + str(issue) + '/attachments')
    files = {'file': (filename, upload_file), }
    headers = {'X-Atlassian-Token': 'nocheck', }
    req = Request('POST', url, headers=headers, files=files, auth=manager._session.auth)
    prepped = req.prepare()
    prepped.body = re.sub(b'filename\*=.*', b'filename="%s"\r' % filename, prepped.body)
    r = manager._session.send(prepped)

    js = utils.json_loads(r)

    if not js or not isinstance(js, collections.Iterable):
        raise JIRAError("Unable to parse JSON: %s" % js)

    attachment = Attachment(manager._options, manager._session, js[0])

    if attachment.size == 0:
        raise JIRAError("Added empty attachment?!: r: %s\nattachment: %s" % (r, attachment))

    return attachment


def add_attachment(manager, issue, path):
    """
    Replace jira's method 'add_attachment' while don't well fixed this issue
    https://github.com/shazow/urllib3/issues/303
    And we need to set filename limit equaled 252 chars.
    :param manager: [jira.JIRA instance]
    :param issue: [jira.JIRA.resources.Issue instance]
    :param path: [string]
    :return: [jira.JIRA.resources.Attachment instance]
    """
    filename = _get_filename(path)

    with open(path, 'rb') as f:
        return _upload_file(manager, issue, f, filename)


JIRA.waldur_add_attachment = add_attachment
