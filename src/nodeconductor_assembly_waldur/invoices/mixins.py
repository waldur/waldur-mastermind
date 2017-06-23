from django.db import models


class ProductCodeMixin(models.Model):
    class Meta(object):
        abstract = True

    # technical code used by accounting software
    product_code = models.CharField(max_length=30, blank=True)
    # article code is used for encoding product category in accounting software
    article_code = models.CharField(max_length=30, blank=True)
