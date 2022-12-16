from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_mastermind.promotions import models


def check_resources(campaign):
    if campaign.state != models.Campaign.States.DRAFT:
        raise ValidationError(_('You can delete draft campaigns only.'))
