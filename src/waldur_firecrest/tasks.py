import logging

from celery import shared_task

from waldur_auth_social.models import OAuthToken
from waldur_core.structure.models import ProjectPermission
from waldur_firecrest.client import FirecrestException
from waldur_mastermind.marketplace.models import OfferingUser
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME

from . import utils

logger = logging.getLogger(__name__)


@shared_task('waldur_firecrest.pull_jobs')
def pull_jobs():
    for offering_user in OfferingUser.objects.filter(offering__type=PLUGIN_NAME):
        try:
            oauth_token = OAuthToken.objects.get(
                provider='keycloak', user=offering_user.user
            )
        except OAuthToken.DoesNotExist:
            logger.debug('OAuth token for user %s is not found', offering_user.user)
            continue

        token = oauth_token.access_token
        if not token:
            logger.debug('Access token for user %s is not found', offering_user.user)
            continue

        service_settings = offering_user.offering.scope

        if not service_settings:
            logger.debug('Offering %s does not have scope', offering_user.offering)
            continue

        api_url = service_settings.options.get('firecrest_api_url')
        if not api_url:
            logger.debug(
                'Service settings %s does not have Firecrest API URL', service_settings
            )
            continue

        project = ProjectPermission.objects.filter(
            user=offering_user.user, is_active=True
        ).first()
        if not project:
            logger.debug('User %s does not have access to any project', project)
            continue

        try:
            utils.pull_jobs(api_url, token, service_settings, project)
        except FirecrestException:
            logger.exception('Unable to pull SLURM jobs using API %s', api_url)
