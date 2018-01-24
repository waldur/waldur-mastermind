import requests

from django.conf import settings


def get_file_content_from_url(url):
    response = requests.get(url, stream=True, verify=settings.WALDUR_SUPPORT['ATTACHMENT_CERTIFICATE_VERIFY'])
    if not response.ok:
        raise requests.RequestException()

    return response.raw
