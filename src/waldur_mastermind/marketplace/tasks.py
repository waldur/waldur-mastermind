from __future__ import unicode_literals

from celery import shared_task

from . import utils, models


@shared_task(name='marketplace.create_screenshot_thumbnail')
def create_screenshot_thumbnail(uuid):
    screenshot = models.Screenshots.objects.get(uuid=uuid)
    utils.create_screenshot_thumbnail(screenshot)
