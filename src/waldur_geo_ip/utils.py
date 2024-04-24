import collections
import logging

import requests
from django.conf import settings

from . import exceptions

logger = logging.getLogger(__name__)


Coordinates = collections.namedtuple("Coordinates", ("latitude", "longitude"))


def get_response(ip_address):
    if not settings.IPSTACK_ACCESS_KEY:
        raise exceptions.GeoIpException("IPSTACK_ACCESS_KEY is empty.")

    url = f"http://api.ipstack.com/{ip_address}?access_key={settings.IPSTACK_ACCESS_KEY}&output=json&legacy=1"  # We don't use https, because current plan does not support HTTPS Encryption

    try:
        response = requests.get(url)
    except requests.exceptions.RequestException as e:
        raise exceptions.GeoIpException(f"Request to geoip API {url} failed: {e}")

    if response.ok:
        return response.json()

    params = (url, response.status_code, response.text)
    raise exceptions.GeoIpException(
        "Request to geoip API {} failed: {} {}".format(*params)
    )


def get_coordinates_by_ip(ip_address):
    """
    Return coordinates by IP or hostname.
    :param ip_address: IP or hostname
    """
    data = get_response(ip_address)
    latitude = data.get("latitude")
    longitude = data.get("longitude")
    return Coordinates(latitude=latitude, longitude=longitude)


def get_country_by_ip(ip_address):
    """
    Return country by IP or hostname.
    :param ip_address: IP or hostname
    """
    data = get_response(ip_address)
    return data.get("country_name")


def detect_coordinates(instance):
    try:
        coordinates = instance.detect_coordinates()
    except exceptions.GeoIpException as e:
        logger.warning("Unable to detect coordinates for %s: %s.", instance, e)
        return

    if coordinates:
        instance.latitude = coordinates.latitude
        instance.longitude = coordinates.longitude
        instance.save(update_fields=["latitude", "longitude"])
