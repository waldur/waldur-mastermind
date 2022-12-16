from django.db import models as django_models
from django_fsm import FSMIntegerField, transition
from model_utils.models import TimeStampedModel

from waldur_core.core.models import DescribableMixin
from waldur_core.logging.models import UuidMixin
from waldur_mastermind.marketplace import models as marketplace_models


class DiscountType(django_models.CharField):
    DISCOUNT = 'discount'
    SPECIAL_PRICE = 'special_price'

    CHOICES = (
        (DISCOUNT, 'Discount'),
        (SPECIAL_PRICE, 'Special price'),
    )

    def __init__(self, *args, **kwargs):
        kwargs['max_length'] = 30
        kwargs['choices'] = self.CHOICES
        super().__init__(*args, **kwargs)


class Campaign(UuidMixin, DescribableMixin):
    class States:
        DRAFT = 1
        ACTIVE = 2
        TERMINATED = 3

        CHOICES = (
            (DRAFT, 'Draft'),
            (ACTIVE, 'Active'),
            (TERMINATED, 'Terminated'),
        )

    start_date = django_models.DateField(
        help_text='Starting from this date, the campaign is active.',
    )
    end_date = django_models.DateField(
        help_text='The last day the campaign is active.',
    )
    coupon = django_models.CharField(
        blank=True,
        default='',
        max_length=255,
        help_text='If coupon is empty, campaign is available to all users.',
    )
    discount_type = DiscountType()
    discount = django_models.IntegerField()
    offerings = django_models.ManyToManyField(
        marketplace_models.Offering, related_name='+'
    )
    required_offerings = django_models.ManyToManyField(
        marketplace_models.Offering, related_name='+'
    )
    stock = django_models.PositiveIntegerField(blank=True, null=True)
    months = django_models.PositiveIntegerField(
        default=1,
        help_text='How many months in a row should the related '
        'service (when activated) get special deal '
        '(0 for indefinitely until active)',
    )
    auto_apply = django_models.BooleanField(default=True, blank=True)
    state = FSMIntegerField(default=States.DRAFT, choices=States.CHOICES)
    service_provider = django_models.ForeignKey(
        marketplace_models.ServiceProvider, on_delete=django_models.CASCADE
    )

    class Permissions:
        customer_path = 'service_provider__customer'

    @classmethod
    def get_url_name(cls):
        return 'promotions-campaign'

    @transition(field=state, source=States.DRAFT, target=States.ACTIVE)
    def activate(self):
        pass

    @transition(field=state, source='*', target=States.TERMINATED)
    def terminate(self):
        pass


class DiscountedResource(TimeStampedModel):
    campaign = django_models.ForeignKey(Campaign, on_delete=django_models.CASCADE)
    resource = django_models.ForeignKey(
        'marketplace.Resource', on_delete=django_models.CASCADE
    )
