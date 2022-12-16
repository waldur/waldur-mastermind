from waldur_mastermind.promotions import views


def register_in(router):
    router.register(
        r'promotions-campaigns',
        views.CampaignViewSet,
        basename='promotions-campaign',
    )
