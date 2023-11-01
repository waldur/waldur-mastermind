# Quotas application

## Overview

Quotas is Django application that provides generic implementation of quotas tracking functionality:

1. Store and query resource limits and usages for project, customer or any other model.
2. Aggregate quota usage in object hierarchies.
3. Aggregate historical data for charting and analysis.

## Define quota fields

Quotas are typically implemented on a per-model basis.
Model with quotas should inherit ``QuotaModelMixin`` and define `Quotas` class.
`Quotas` class consists of set of quota fields. Consider the following example:

```python
  # in models.py

  from waldur_core.quotas import models as quotas_models, fields as quotas_fields

  class Tenant(quotas_models.QuotaModelMixin, models.Model):

      class Quotas(quotas_models.QuotaModelMixin.Quotas):
          vcpu = quotas_fields.QuotaField(default_limit=20, is_backend=True)
          ram = quotas_fields.QuotaField(default_limit=51200, is_backend=True)
          storage = quotas_fields.QuotaField(default_limit=1024000, is_backend=True)
```

As you can see, Tenant model defines quota fields for number of virtual CPU cores, amount of RAM and storage.

## Change object quotas usage and limit

To edit objects quotas use:

* ``set_quota_limit`` - replace old quota limit with new one
* ``set_quota_usage`` - replace old quota usage with new one
* ``add_quota_usage`` - add value to quota usage

Do not edit quotas manually, because this will break quotas in objects ancestors.

Please note that aggregated usage is not stored in the database. Instead usage deltas are saved. The main reason behind it is to avoid deadlocks when multiple requests are trying to update the same quota for customer or project simultaneously.

## Check if quota exceeded

To check if any of object quotas exceeded, use ``validate_quota_change`` method of object with quotas.
This method receive dictionary of quotas usage deltas and returns errors if one or more quotas of object exceeded.

## Sort objects by quotas with django_filters.FilterSet

Inherit your ``FilterSet`` from ``QuotaFilterMixin`` and follow next steps to enable ordering by quotas.

Add ``quotas__limit`` and ``-quotas__limit`` to filter meta ``order_by`` attribute if you want to order by quotas limits. Ordering can be done only by one quota at a time.

## Workflow for quota allocation

In order to prevent bugs when multiple simultaneous requests are performed, the following workflow is used.

1) As soon as we know what quota will be used we increase its usage.
  It is performed in serializers' save or update method.
  If quota usage becomes over limit, validation error is raised.
  Consider for example `InstanceFlavorChangeSerializer` in OpenStack plugin.

2) If backend API call for resource provision fails, frontend quota usage is not modified.
  Instead it is assumed that quota pulling is triggered either by user or by cron.

3) Quota usage is decreased only when backend API call for resource deletion succeeds.
  Consider for example `delete_volume` backend method in OpenStack plugin.
