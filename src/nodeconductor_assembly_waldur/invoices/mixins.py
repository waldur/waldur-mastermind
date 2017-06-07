from django.db import models


class ProductCodeMixin(models.Model):
    class Meta(object):
        abstract = True

    product_code = models.CharField(max_length=30, blank=True)
