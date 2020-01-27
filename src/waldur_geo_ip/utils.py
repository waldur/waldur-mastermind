import collections
import logging

import requests
from django.conf import settings

from . import exceptions

logger = logging.getLogger(__name__)


Coordinates = collections.namedtuple('Coordinates', ('latitude', 'longitude'))


def get_coordinates_by_ip(ip_address):
    """
    Return coordinates by IP or hostname.
    :param ip_address: IP or hostname
    """
    if not settings.IPSTACK_ACCESS_KEY:
        raise exceptions.GeoIpException("IPSTACK_ACCESS_KEY is empty.")

    url = 'http://api.ipstack.com/{}?access_key={}&output=json&legacy=1'.format(
        ip_address,
        settings.IPSTACK_ACCESS_KEY)  # We don't use https, because current plan does not support HTTPS Encryption

    try:
        response = requests.get(url)
    except requests.exceptions.RequestException as e:
        raise exceptions.GeoIpException("Request to geoip API %s failed: %s" % (url, e))

    if response.ok:
        data = response.json()
        latitude = data.get('latitude')
        longitude = data.get('longitude')

        if latitude and longitude:
            return Coordinates(latitude=latitude,
                               longitude=longitude)

    params = (url, response.status_code, response.text)
    raise exceptions.GeoIpException("Request to geoip API %s failed: %s %s" % params)


def detect_coordinates(instance):
    try:
        coordinates = instance.detect_coordinates()
    except exceptions.GeoIpException as e:
        logger.warning('Unable to detect coordinates for %s: %s.', instance, e)
        return

    if coordinates:
        instance.latitude = coordinates.latitude
        instance.longitude = coordinates.longitude
        instance.save(update_fields=['latitude', 'longitude'])
