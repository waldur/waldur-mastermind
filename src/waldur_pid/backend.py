import logging
import requests
import json

from django.conf import settings

from waldur_core.structure import ServiceBackend

logger = logging.getLogger(__name__)


class DataciteBackend(ServiceBackend):
    def __init__(self):
        self.settings = settings.WALDUR_PID['DATACITE']

    def post(self, data):
        headers = {
            'Content-Type': 'application/vnd.api+json',
        }
        response = requests.post(
            'https://api.test.datacite.org/dois',
            headers=headers,
            data=json.dumps(data),
            auth=(self.settings['REPOSITORY_ID'], self.settings['PASSWORD'])
        )

        return response

    def create_doi(self, instance):
        data = {
            'data': {
                'type': 'dois',
                'attributes': {
                    'doi': self.settings['PREFIX']
                }
            }
        }
        response = self.post(data)
        if response.status_code != 201:
            logger.error('Create doi for %s is fail. Status code: %s, message: %s.' % (
                instance,
                response.status_code,
                response.text
            ))
        else:
            response_data = json.loads(response.text)
            instance.datacite_doi = response_data['data']['id']
            instance.save()
