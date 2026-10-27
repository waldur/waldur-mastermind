from celery import shared_task

from . import utils


@shared_task(name="waldur_mastermind.feedback.expire_requests")
def expire_requests():
    utils.expire_requests()
