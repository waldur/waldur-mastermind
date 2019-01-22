from waldur_mastermind.support import models as support_models


class RequestBasedOffering(support_models.Offering):
    """
    It is assumed that each model may have t most single registrator,
    hence in order to use multiple registrators for the same model we need to use
    proxy model class.
    """
    class Meta:
        proxy = True
