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

from nodeconductor.logging.elasticsearch_client import ElasticsearchClient
from nodeconductor.server.celery import app as celery_app

User = get_user_model()


class Command(BaseCommand):
    help = "Check status of Waldur MasterMind configured services"

    def add_arguments(self, parser):
        parser.add_argument(
            '--scheme',
            dest='scheme', default='http',
            choices=['http', 'https'],
            help='Used for API endpoints status check. Default is http.',
        )
        parser.add_argument(
            '--domain',
            dest='domain', default='localhost',
            help='Used for API endpoints status check. Default is localhost.',
        )
        parser.add_argument(
            '--port',
            dest='port', default='',
            help='Used for API endpoints status check. Default is 80.',
        )
        parser.add_argument(
            '--skip-endpoints',
            action='store_true', default=False, dest='skip_endpoints',
            help='API endpoints will not be checked with this flag.',
        )

    def handle(self, *args, **options):
        success_status = self.style.SUCCESS(' [OK]')
        error_status = self.style.ERROR(' [ERROR]')
        output_messages = {
            'database': ' - Database connection',
            'workers': ' - Celery worker(s)',
            'redis': ' - Redis broker',
            'elasticsearch': ' - ElasticSearch backend',
        }
        padding = len(max(output_messages.values(), key=len))
        # If services checks didn't pass, skip API endpoints check
        skip_endpoints = False

        # Rise logging level to prevent redundant log messages
        logging.disable(logging.CRITICAL)
        self.stdout.write('Checking Waldur MasterMind services...')

        # Check database connectivity
        self.stdout.write(output_messages['database'].ljust(padding), ending='')
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
        self.stdout.write(output_messages['elasticsearch'].rjust(padding), ending='')
        es_client = ElasticsearchClient()
        try:
            if es_client.client.ping():
                self.stdout.write(success_status)
            else:
                skip_endpoints = True
                self.stdout.write(error_status)
        except ElasticsearchException:
            skip_endpoints = True
            self.stdout.write(error_status)

        if skip_endpoints:
            self.stderr.write('API endpoints check skipped due to erred services')
        elif options['skip_endpoints']:
            self.stdout.write('API endpoints check skipped')
        else:
            host = '{scheme}://{domain}{port}'.format(
                scheme=options['scheme'],
                domain=options['domain'],
                port=':' + options['port'] if options['port'] else ''
            )
            self._check_api_endpoints(host)

        # return logging level back
        logging.disable(logging.NOTSET)

    def _check_api_endpoints(self, host):
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

            url = host + path
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
