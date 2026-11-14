from celery import shared_task
from django.core.management import call_command

from waldur_mastermind.mcp import models


@shared_task(name="waldur_mastermind.mcp.clear_expired_oauth_tokens")
def clear_expired_oauth_tokens():
    call_command("cleartokens")
    models.ConnectedApp.objects.ended().delete()


@shared_task(name="waldur_mastermind.mcp.clear_expired_cimd_applications")
def clear_expired_cimd_applications():
    call_command("clearcimdapplications")
