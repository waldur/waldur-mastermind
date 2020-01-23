import logging
import requests

from django.conf import settings

from waldur_core.structure import ServiceBackend

from . import exceptions

logger = logging.getLogger(__name__)


class DataciteBackend(ServiceBackend):
    def __init__(self):
        self.settings = settings.WALDUR_PID['DATACITE']

    def post(self, data):
        headers = {
            'Content-Type': 'application/vnd.api+json',
        }

        if not self.settings['API_URL']:
            raise exceptions.DataciteException('API_URL is not defined.')

        response = requests.post(
            self.settings['API_URL'],
            headers=headers,
            json=data,
            auth=(self.settings['REPOSITORY_ID'], self.settings['PASSWORD'])
        )

        return response

    def create_doi(self, instance):
        data = {
            'data': {
                'type': 'dois',
                'attributes': {
                    'prefix': self.settings['PREFIX'],
                    'event': 'publish',
                    'creators': [{
                        'name': instance.get_datacite_creators_name()
                    }],
                    'titles': [{
                        'title': instance.get_datacite_title()
                    }],
                    'descriptions': [{'description': instance.get_datacite_description()}],
                    'publisher': self.settings['PUBLISHER'],
                    'publicationYear': instance.get_datacite_publication_year(),
                    'types': {
                        'resourceTypeGeneral': 'Service'
                    },
                    'url': instance.get_datacite_url(),
                    'schemaVersion': 'http://datacite.org/schema/kernel-4'
                }
            }
        }
        response = self.post(data)

        if response.status_code == 201:
            instance.datacite_doi = response.json()['data']['id']
            instance.save()
        else:
            logger.error('Creating Datacite DOI for %s has failed. Status code: %s, message: %s.' % (
                instance,
                response.status_code,
                response.text
            ))
