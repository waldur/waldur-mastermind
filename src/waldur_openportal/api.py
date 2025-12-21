import logging
from http import HTTPStatus as status

from django.http import JsonResponse
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import (
    api_view,
    authentication_classes,
    permission_classes,
)

from waldur_core.core import utils as core_utils

from . import enums, models, tasks
from . import op as openportal
from .board import OpenPortalBoard

logger = logging.getLogger(__name__)


@extend_schema(exclude=True)
@api_view(["GET"])
@authentication_classes([])
@permission_classes([])
def fetch_job(request):
    """
    End-point called by the OpenPortal bridge agent to signal to Waldur
    that new a job has arrived and it needs to be fetched.

    This triggers the code to fetch the job and then submit it for
    processing to one of the backend celery workers.

    As argument, you need to pass in the `job_id` query parameter,
    which must match one of the jobs in the OpenPortal bridge queue.

    If a job is not found, it will return an authorisation error
    (403 Forbidden), thereby preventing "unauthorised" access to
    this end-point.

    If the job is found, it will return a 200 OK response.

    The use of the job-id acts as a bit of authorisation, as the
    job-id is random, and is only known to the OpenPortal bridge agent.
    It is a de-facto secret shared between the OpenPortal bridge agent
    and Waldur, and is not known to any other user or system.
    """

    board = OpenPortalBoard()

    job_id = request.query_params.get("job_id")

    if not job_id:
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    # move to serlialiser django
    job_id = str(job_id).lstrip().rstrip()

    if len(job_id) == 0:
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    # fetch this job from the OpenPortal bridge queue
    try:
        job = board.fetch_job(job_id)
        if job is None:
            response = JsonResponse({})
            response.status_code = status.UNAUTHORIZED
            return response
    except Exception as e:
        logger.error(f"Error fetching job {job_id}: {e}")
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    job_id = str(job.id).lstrip().rstrip()

    if len(job_id) == 0:
        logger.error(f"Job {job} has no job_id")
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    if job.state != openportal.Status.pending():
        logger.error(f"Job {job_id} is not in PENDING state, but in {job.state}")
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    # create a Job model object for this job
    job_model, created = models.Job.objects.get_or_create(
        job_id=job_id,
        defaults={
            "job_data": job.to_json(),
            "state": enums.JobState.PENDING,
        },
    )

    if not created:
        logger.warning(f"Job {job_id} already exists in the database... re-running?")

    if job_model.state != enums.JobState.PENDING:
        logger.error(f"Job {job_id} is not in PENDING state, but in {job_model.state}")
        response = JsonResponse({})
        response.status_code = status.UNAUTHORIZED
        return response

    # submit the job for processing
    logger.info(f"Submitting job {job_id} for processing")
    tasks.run_job.delay(core_utils.serialize_instance(job_model))

    response = JsonResponse({})
    response.status_code = status.OK
    return response
