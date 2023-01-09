# Quotas application

## Overview

Quotas is Django application that provides generic implementation of quotas tracking functionality:

1. Store and query resource limits and usages for project, customer or any other model.
2. Aggregate quota usage in object hierarchies.
3. Aggregate historical data for charting and analysis.
4. Prevent user from consuming an entire system's resources by
  raising alerts when quota threshold has been reached.

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

## Parents for object with quotas

Object with quotas can have quota-parents. If usage in child was increased - it will be increased in parent too.
Method ``get_quota_parents`` have to be overridden to return list of quota-parents if object has any of them.
Only first level of ancestors has be added as parents, for example if membership is child of project and project
is child if customer - memberships ``get_quota_parents`` has to return only project, not customer.
It is not necessary for parents to have the same quotas as children, but logically they should have at least one
common quota.

## Check is quota exceeded

To check is one separate quota exceeded - use ``is_exceeded`` method of quota. It can receive usage delta or
threshold and check is quota exceeds considering delta and/or threshold.

To check is any of object or his ancestors quotas exceeded - use ``validate_quota_change`` method of object with quotas.
This method receive dictionary of quotas usage deltas and returns errors if one or more quotas of object or his
quota-ancestors exceeded.

## Get sum of quotas

``QuotasModelMixin`` provides ``get_sum_of_quotas_as_dict`` methods which calculates sum of each quotas for given
scopes.

## Allow user to edit quotas

Will be implemented soon.

## Add quotas to quota scope serializer

``QuotaSerializer`` can be used as quotas serializer in quotas scope controller.

## Sort objects by quotas with django_filters.FilterSet

Inherit your ``FilterSet`` from ``QuotaFilterMixin`` and follow next steps to enable ordering by quotas.

Usage:

1. Add ``quotas__limit`` and ``-quotas__limit`` to filter meta ``order_by`` attribute
  if you want order by quotas limits and ``quotas__usage``, ``-quota__usage`` if you want to order by quota usage.

2. Add `quotas__<limit or usage>__<quota_name>` to meta `order_by` attribute if you want to allow user to order `<quota_name>`. For example, `quotas__limit__ram` will enable ordering by `ram` quota.

Ordering can be done only by one quota at a time.

## QuotaInline for admin models

``quotas.admin`` contains generic inline model``QuotaInline``, which can be used as inline model for any quota
scope.

## Global count quotas for models

Global count quota - quota without scope that stores information about count of all model instances.
To create new global quota - add field `GLOBAL_COUNT_QUOTA_NAME = '<quota name>'` to model.
(Please use prefix `nc_global` for global quotas names)

## Workflow for quota allocation

In order to prevent bugs when multiple simultaneous requests are performed, the following workflow is used.

1) As soon as we know what quota will be used we increase its usage.
  It is performed in serializers' save or update method.
  If quota usage becomes over limit, validation error is raised.
  Consider for example InstanceFlavorChangeSerializer in OpenStack plugin.

2) If backend API call for resource provision fails, frontend quota usage is not modified.
  Instead it is assumed that quota pulling is triggered either by user or by cron.

3) Quota usage is decreased only when backend API call for resource deletion succeeds.
  Consider for example delete_volume backend method in OpenStack plugin.
