import logging
import requests

from django.contrib.auth import get_user_model, authenticate
from django.core.management.base import BaseCommand
from django.db import connection
from django.db.utils import OperationalError
from elasticsearch.exceptions import ElasticsearchException
from redis import exceptions as redis_exceptions
from rest_framework import status
from rest_framework.authtoken.models import Token
from rest_framework.schemas import EndpointInspector

from waldur_core.logging.elasticsearch_client import ElasticsearchClient, ElasticsearchClientError
from waldur_core.server.celery import app as celery_app

User = get_user_model()


class Command(BaseCommand):
    help = "Check status of Waldur MasterMind configured services"

    def add_arguments(self, parser):
        parser.add_argument(
            '--check-api-endpoints-at',
            dest='base_url', default=None,
            help='Runs API endpoints check at specified base URL (i.e. http://example.com). '
                 'If this argument is not provided, check will be skipped.',
        )

    def handle(self, *args, **options):
        success_status = self.style.SUCCESS(' [OK]')
        error_status = self.style.ERROR(' [ERROR]')
        output_messages = {
            'database': ' - Database %(vendor)s connection',
            'workers': ' - Task runners (Celery workers)',
            'redis': ' - Queue and cache server (Redis) connection',
            'elasticsearch': ' - Event store (Elasticsearch) connection',
        }
        padding = len(max(output_messages.values(), key=len))
        # If services checks didn't pass, skip API endpoints check
        skip_endpoints = False

        # Rise logging level to prevent redundant log messages
        logging.disable(logging.CRITICAL)
        self.stdout.write('Checking Waldur MasterMind services...')

        # Check database connectivity
        db_vendor = connection.vendor.capitalize().replace('sql', 'SQL').replace('Sql', 'SQL')
        self.stdout.write((output_messages['database'] % {'vendor': db_vendor}).ljust(padding), ending='')
        try:
            connection.cursor()
        except OperationalError:
            skip_endpoints = True
            self.stdout.write(error_status)
        else:
            self.stdout.write(success_status)

        # Check celery and redis
        celery_inspect = celery_app.control.inspect()
        celery_results = {
            'workers': success_status,
            'redis': success_status,
        }
        try:
            stats = celery_inspect.stats()
            if not stats:
                skip_endpoints = True
                celery_results['workers'] = error_status
        except redis_exceptions.RedisError:
            skip_endpoints = True
            celery_results['redis'] = error_status
            celery_results['workers'] = error_status
        finally:
            self.stdout.write(output_messages['workers'].ljust(padding) + celery_results['workers'])
            self.stdout.write(output_messages['redis'].ljust(padding) + celery_results['redis'])

        # Check ElasticSearch
        self.stdout.write(output_messages['elasticsearch'].ljust(padding), ending='')
        try:
            es_client = ElasticsearchClient()
            if es_client.client.ping():
                self.stdout.write(success_status)
            else:
                skip_endpoints = True
                self.stdout.write(error_status)
        except (ElasticsearchException, ElasticsearchClientError):
            skip_endpoints = True
            self.stdout.write(error_status)

        if skip_endpoints:
            self.stderr.write('API endpoints check skipped due to erred services')
            exit(1)
        elif options['base_url'] is None:
            self.stdout.write('API endpoints check skipped')
        else:
            self._check_api_endpoints(options['base_url'])

        # return logging level back
        logging.disable(logging.NOTSET)

    def _check_api_endpoints(self, base_url):
        self.stdout.write('\nChecking Waldur MasterMind API endpoints...')
        inspector = EndpointInspector()
        endpoints = inspector.get_api_endpoints()
        user, _ = User.objects.get_or_create(username='waldur_status_checker', is_staff=True)
        authenticate(username='waldur_status_checker')
        token = Token.objects.get(user=user)

        for endpoint in endpoints:
            path, method, view = endpoint
            if method != 'GET' or '{pk}' in path or '{uuid}' in path:
                continue

            url = base_url + path
            self.stdout.write(' Checking %s endpoint...' % url, ending='')
            try:
                response = requests.get(url, headers={'Authorization': 'Token %s' % token.key})
            except requests.RequestException:
                self.stdout.write(self.style.ERROR(' [ERROR]'))
            else:
                if response.status_code != status.HTTP_200_OK:
                    self.stdout.write(self.style.ERROR(' [%d]' % response.status_code))
                else:
                    self.stdout.write(self.style.SUCCESS(' [200]'))

        # clean up
        user.delete()
