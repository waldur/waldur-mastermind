import logging
import time

from waldur_firecrest.client import FirecrestClient

from .models import Job

logger = logging.getLogger(__name__)


def pull_jobs(api_url, token, service_settings, project):
    client = FirecrestClient(api_url, token)
    task_id = client.list_jobs()
    while True:
        task = client.get_task(task_id)
        if task['status'] in ['200', '400']:
            break
        time.sleep(2)

    if task['status'] != '200':
        logger.warning('Firecrest task %s has failed', task_id)
        return

    for job_details in task['data']:
        job, created = Job.objects.update_or_create(
            service_settings=service_settings,
            project=project,
            backend_id=job_details['jobid'],
            defaults={
                'name': job_details['name'],
                'runtime_state': job_details['state'],
                'state': Job.States.OK,
            },
        )
        if created:
            logger.info(
                'SLURM job %s has been pulled from Firecrest to project %s',
                job.backend_id,
                project.id,
            )


def submit_job(api_url, token, job):
    client = FirecrestClient(api_url, token)
    task_id = client.submit_job(job.file.file)

    while True:
        task = client.get_task(task_id)
        if task['status'] in ['200', '400']:
            break
        time.sleep(2)

    if task['status'] != '200':
        job.state = Job.States.ERRED
        job.error_message = task['data']
        job.save()

    job_id = task['data']['jobid']
    job.backend_id = job_id
    job.report = task['data']['result']
    job.state = Job.States.OK
    job.save()
