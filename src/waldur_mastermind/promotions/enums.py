from django.db import models


class DiscountType(models.TextChoices):
    DISCOUNT = "discount", "Discount"
    SPECIAL_PRICE = "special_price", "Special price"


class CampaignState(models.IntegerChoices):
    DRAFT = 1, "Draft"
    ACTIVE = 2, "Active"
    TERMINATED = 3, "Terminated"
