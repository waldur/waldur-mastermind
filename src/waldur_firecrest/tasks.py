import logging

from celery import shared_task

from waldur_auth_social.models import OAuthToken
from waldur_core.core.utils import deserialize_instance
from waldur_core.structure.managers import get_connected_projects
from waldur_core.structure.models import Project
from waldur_firecrest.client import FirecrestException
from waldur_firecrest.models import Job
from waldur_mastermind.marketplace.models import OfferingUser
from waldur_mastermind.marketplace_slurm import PLUGIN_NAME

from . import utils

logger = logging.getLogger(__name__)


@shared_task(name='waldur_firecrest.pull_jobs')
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

        project_id = get_connected_projects(offering_user.user).first()
        if not project_id:
            logger.debug(
                'User %s does not have access to any project', offering_user.user
            )
            continue

        project = Project.objects.get(id=project_id)

        try:
            utils.pull_jobs(api_url, token, service_settings, project)
        except FirecrestException:
            logger.exception('Unable to pull SLURM jobs using API %s', api_url)


@shared_task(name='waldur_firecrest.submit_job')
def submit_job(serialized_job):
    job = deserialize_instance(serialized_job)
    try:
        oauth_token = OAuthToken.objects.get(provider='keycloak', user=job.user)
    except OAuthToken.DoesNotExist:
        logger.debug('OAuth token for user %s is not found', job.user)
        job.state = Job.States.ERRED
        job.error_message = 'OAuth token is not found'
        job.save()
        return

    token = oauth_token.access_token
    if not token:
        logger.debug('Access token for user %s is not found', job.user)
        job.state = Job.States.ERRED
        job.error_message = 'Access token is not found'
        job.save()
        return

    api_url = job.service_settings.options.get('firecrest_api_url')
    if not api_url:
        logger.debug(
            'Service settings %s does not have Firecrest API URL', job.service_settings
        )
        job.state = Job.States.ERRED
        job.error_message = 'Service does not have Firecrest API URL'
        job.save()
        return

    try:
        utils.submit_job(api_url, token, job)
    except FirecrestException as e:
        job.state = Job.States.ERRED
        job.error_message = str(e)
        job.save()
