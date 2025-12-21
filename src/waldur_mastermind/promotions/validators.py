from django.utils.translation import gettext_lazy as _
from rest_framework.exceptions import ValidationError

from waldur_mastermind.promotions.enums import CampaignState


def check_resources(campaign):
    if campaign.state != CampaignState.DRAFT:
        raise ValidationError(_("You can delete draft campaigns only."))
