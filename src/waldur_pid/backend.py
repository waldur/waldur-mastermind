import logging

import requests
from django.conf import settings

from waldur_core.structure import ServiceBackend

from . import exceptions

logger = logging.getLogger(__name__)


class DataciteBackend(ServiceBackend):
    def __init__(self):
        self.settings = settings.WALDUR_PID['DATACITE']

    def _datacite_auth_request(self, request_verb, data, url=None):
        headers = {
            'Content-Type': 'application/vnd.api+json',
        }

        if not self.settings['API_URL']:
            raise exceptions.DataciteException('API_URL is not defined.')

        response = request_verb(
            url if url else self.settings['API_URL'],
            headers=headers,
            json=data,
            auth=(self.settings['REPOSITORY_ID'], self.settings['PASSWORD']),
        )

        return response

    def post(self, data, url=None):
        return self._datacite_auth_request(requests.post, data, url)

    def put(self, data, url=None):
        return self._datacite_auth_request(requests.put, data, url)

    def get(self, doi):
        headers = {
            'accept': 'application/vnd.api+json',
        }

        url = self.settings['API_URL']
        if not url:
            raise exceptions.DataciteException('API_URL is not defined.')

        url = f"{url}/{doi}"

        response = requests.get(url=url, headers=headers,)
        return response

    def create_doi(self, instance):
        data = {
            'data': {
                'type': 'dois',
                'attributes': {
                    'prefix': self.settings['PREFIX'],
                    'event': 'publish',
                    'creators': [{'name': instance.get_datacite_creators_name()}],
                    'titles': [{'title': instance.get_datacite_title()}],
                    'descriptions': [
                        {'description': instance.get_datacite_description()}
                    ],
                    'publisher': self.settings['PUBLISHER'],
                    'publicationYear': instance.get_datacite_publication_year(),
                    'types': {'resourceTypeGeneral': 'Service'},
                    'url': instance.get_datacite_url(),
                    'schemaVersion': 'http://datacite.org/schema/kernel-4',
                },
            }
        }
        response = self.post(data)

        if response.status_code == 201:
            instance.datacite_doi = response.json()['data']['id']
            instance.save()
        else:
            logger.error(
                'Creating Datacite DOI for %s has failed. Status code: %s, message: %s.'
                % (instance, response.status_code, response.text)
            )

    def link_doi_with_collection(self, instance):
        collection_doi = self.settings['COLLECTION_DOI']
        if not collection_doi:
            raise exceptions.DataciteException(
                'COLLECTION_DOI is not defined in settings, cannot proceed with linking'
            )
        if not instance.datacite_doi:
            raise exceptions.DataciteException(
                'Instance does not have a registered DOI, cannot proceed with linking'
            )

        data = {
            'data': {
                'attributes': {
                    'relatedIdentifiers': [
                        {
                            'relatedIdentifierType': 'DOI',
                            'relationType': 'IsPartOf',
                            'relatedIdentifier': f'{instance.datacite_doi}',
                            'resourceTypeGeneral': 'Service',
                        }
                    ]
                }
            }
        }

        response = self.put(data, f"{self.settings['API_URL']}/{collection_doi}")

        if response.status_code != 200:
            logger.error(
                'Linking Datacite DOI of %s with %s has failed. Status code: %s, message: %s.'
                % (instance, collection_doi, response.status_code, response.text)
            )

    def get_datacite_data(self, doi):
        logger.debug('Looking up DOI %s' % doi)
        response = self.get(doi)

        if response.status_code == 200:
            return response.json()['data']
        else:
            logger.error(
                'Receiving Datacite data for %s has failed. Status code: %s, message: %s.'
                % (doi, response.status_code, response.text)
            )
