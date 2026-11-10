"""The Celery task that runs a demo preset load.

It lives here rather than in marketplace/tasks.py, which the preset loader
imports through invoices.tasks; marketplace's AppConfig.ready() imports this
module so the worker registers the task.
"""

import logging

from celery import shared_task
from celery.exceptions import SoftTimeLimitExceeded
from django.utils import timezone

from waldur_mastermind.marketplace import models
from waldur_mastermind.marketplace.demo_presets import jobs as demo_preset_jobs
from waldur_mastermind.marketplace.demo_presets.manifest import DemoPresetManager

logger = logging.getLogger(__name__)


@shared_task(
    name="waldur_mastermind.marketplace.load_demo_preset",
    soft_time_limit=demo_preset_jobs.LOAD_TIME_LIMIT - 60,
    time_limit=demo_preset_jobs.LOAD_TIME_LIMIT,
)
def load_demo_preset(job_uuid):
    """Run a queued demo preset load and record how it ended."""
    States = models.DemoPresetLoad.States
    claimed = models.DemoPresetLoad.objects.filter(
        uuid=job_uuid, state=States.PENDING
    ).update(state=States.RUNNING, started_at=timezone.now())
    if not claimed:
        logger.info("Demo preset load %s is not pending, skipping", job_uuid)
        return

    job = models.DemoPresetLoad.objects.get(uuid=job_uuid)
    state, message, output = States.ERRED, "", job.output
    try:
        result = DemoPresetManager.load_preset(name=job.preset, **job.options)
        output = job.output + result.get("output", "")
        if result["success"]:
            state = States.DONE
        else:
            message = result["message"]
    except SoftTimeLimitExceeded:
        message = "The load did not finish in time."
    except Exception as e:
        logger.exception("Demo preset load %s failed", job_uuid)
        message = str(e)
    finally:
        models.DemoPresetLoad.objects.filter(uuid=job_uuid).update(
            state=state,
            error_message=message,
            output=output,
            finished_at=timezone.now(),
        )
