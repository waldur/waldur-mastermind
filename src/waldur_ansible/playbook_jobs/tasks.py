import errno
import logging
from shutil import rmtree

from celery import shared_task

logger = logging.getLogger(__name__)


@shared_task(name='waldur_ansible.tasks.delete_playbook_workspace')
def delete_playbook_workspace(workspace_path):
    logger.debug('Deleting playbook workspace %s.', workspace_path)
    try:
        rmtree(workspace_path)
    except OSError as e:
        if e.errno == errno.ENOENT:
            logger.info('Playbook workspace %s does not exist.', workspace_path)
        else:
            logger.warning('Failed to delete playbook workspace %s.', workspace_path)
            raise
    else:
        logger.info('Playbook workspace %s has been deleted.', workspace_path)
