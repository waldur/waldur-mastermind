import logging

from celery import shared_task
from django.conf import settings

from waldur_keycloak.models import ProjectGroup
from waldur_rancher.models import Cluster, ClusterRole

logger = logging.getLogger(__name__)


@shared_task(name='waldur_keycloak_rancher.sync_groups')
def sync_groups():
    if not settings.WALDUR_KEYCLOAK['ENABLED']:
        logger.debug('Skipping Keycloak synchronization because plugin is disabled.')
        return

    for project_group in ProjectGroup.objects.all():
        project = project_group.project
        for cluster in Cluster.objects.filter(project=project):
            backend = cluster.get_backend()
            try:
                backend.get_or_create_cluster_group_role(
                    f'keycloakoidc_group://{project.name}',
                    cluster.backend_id,
                    ClusterRole.CLUSTER_MEMBER,
                )
            except Exception:
                logger.warning(
                    'Unable to create cluster group for project %s and cluster %s',
                    project,
                    cluster,
                )
