import collections
import logging

from django.conf import settings
import requests

UserDetails = collections.namedtuple('UserDetails', ('full_name', 'native_name', 'organization', 'job_title'))


class BCCException(Exception):
    default_code = 400
    default_detail = 'BCC authentication client error'

    def __init__(self, detail=None, code=None):
        self.detail = detail or self.default_detail
        self.code = code or self.default_code

    def __str__(self):
        return self.detail


def get_user_details(nid, vno):
    """
    Fetch user details from PayFixation API using configured credentials.
    :param nid: NID
    :type nid: string
    :param vno: Voucher No
    :type vno: string
    :return: user details
    :rtype: UserDetails
    """
    conf = settings.WALDUR_AUTH_BCC
    url = conf['BASE_API_URL']
    params = {
        'username': conf['USERNAME'],
        'password': conf['PASSWORD'],
        'nid': nid,
        'vno': vno,
        'type': 'bcc',
    }

    try:
        response = requests.get(url, params=params)
    except requests.RequestException as e:
        logging.warning('Unable to get user details from PayFixation API. Exception: %s', e)
        raise BCCException(detail=e.message)

    if response.status_code != 200:
        raise BCCException(code=response.status_code)

    try:
        data = response.json()
    except ValueError:
        raise BCCException('Unable to parse JSON.')

    error = data.get('error')
    if error:
        raise BCCException(detail=error)

    name = data['nameen']
    if not name:
        raise BCCException(detail='Invalid input parameters.')

    return UserDetails(
        full_name=data['nameen'],
        native_name=data['namebn'],
        job_title=data['desig'],
        organization=data['office'],
    )
