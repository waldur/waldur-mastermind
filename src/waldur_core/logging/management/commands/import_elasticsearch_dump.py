from argparse import FileType
from functools import reduce
import operator
import json
from sys import stdin

from django.core.exceptions import ObjectDoesNotExist
from django.core.management.base import BaseCommand

from waldur_auth_saml2.log import Saml2AuthEventLogger
from waldur_auth_social.log import SocialEventLogger
from waldur_core.core.log import AuthEventLogger
from waldur_core.core.models import User
from waldur_core.logging.models import Event, Feed
from waldur_core.structure import SupportedServices
from waldur_core.structure.models import Customer, Project
from waldur_freeipa.log import FreeIPAEventLogger
from waldur_mastermind.common.utils import parse_datetime
from waldur_mastermind.marketplace.models import Resource

SKIPPED_FIELDS = (
    'importance',
    'importance_code',
    'logger',
    'type',
    '@version',
    'host',
    'levelname',
    'tags',
    'port',
)

USER_LOGGERS = (
    Saml2AuthEventLogger,
    SocialEventLogger,
    AuthEventLogger,
    FreeIPAEventLogger,
)

USER_EVENTS = reduce(operator.add, map(lambda logger: tuple(logger.Meta.event_types), USER_LOGGERS))

RESOURCE_MODELS = SupportedServices.get_resource_models()


def get_scopes(event):
    scopes = []
    user_uuid = event.context.get('user_uuid')
    if user_uuid and event.event_type in USER_EVENTS:
        try:
            user = User.objects.get(uuid=user_uuid)
            scopes.append(user)
        except ObjectDoesNotExist:
            pass

    affected_user_uuid = event.context.get('affected_user_uuid')
    if affected_user_uuid:
        try:
            user = User.objects.get(uuid=affected_user_uuid)
            scopes.append(user)
        except ObjectDoesNotExist:
            pass

    customer_uuid = event.context.get('customer_uuid')
    if customer_uuid:
        try:
            customer = Customer.objects.get(uuid=customer_uuid)
            scopes.append(customer)
        except ObjectDoesNotExist:
            pass

    project_uuid = event.context.get('project_uuid')
    if project_uuid:
        try:
            project = Project.objects.get(uuid=project_uuid)
            scopes.append(project)
        except ObjectDoesNotExist:
            pass

    resource_uuid = event.context.get('resource_uuid')
    if resource_uuid:
        if event.event_type.startswith('marketplace'):
            model = Resource
        else:
            resource_type = event.context.get('resource_type')
            model = RESOURCE_MODELS.get(resource_type)

        if model:
            try:
                resource = model.objects.get(uuid=resource_uuid)
                scopes.append(resource)
            except ObjectDoesNotExist:
                pass

    return scopes


class Command(BaseCommand):
    help = 'Import ElasticSearch events to PostgreSQL database.'

    def add_arguments(self, parser):
        parser.add_argument('input', nargs='?', type=FileType('r'),
                            default=stdin)

    def handle(self, *args, **options):
        counter = 0
        for line in options['input']:
            record = json.loads(line)
            record = record['_source']
            for field in SKIPPED_FIELDS:
                record.pop(field, None)
            created = parse_datetime(record.pop('@timestamp'))
            event_type = record.pop('event_type')
            message = record.pop('message')
            event = Event.objects.create(
                created=created,
                event_type=event_type,
                message=message,
                context=record,
            )
            for scope in get_scopes(event):
                Feed.objects.create(scope=scope, event=event)
            counter += 1
            if counter % 100 == 0:
                self.stdout.write('%s events have been imported' % counter)
        self.stdout.write('%s events have been imported' % counter)
