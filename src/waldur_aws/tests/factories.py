import factory
from django.urls import reverse
from factory import fuzzy
from libcloud.compute.types import NodeState

from waldur_aws import models
from waldur_core.structure.tests import factories as structure_factories


class RegionFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Region

    name = factory.Sequence(lambda n: 'region%s' % n)
    backend_id = factory.sequence(lambda n: 'id-%s' % n)

    @classmethod
    def get_url(cls, region=None):
        if region is None:
            region = RegionFactory()
        return 'http://testserver' + reverse(
            'aws-region-detail', kwargs={'uuid': region.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('aws-region-list')


class ImageFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Image

    name = factory.Sequence(lambda n: 'image%s' % n)
    backend_id = factory.Sequence(lambda n: 'image-id%s' % n)
    region = factory.SubFactory(RegionFactory)

    @classmethod
    def get_url(cls, image=None):
        if image is None:
            image = ImageFactory()
        return 'http://testserver' + reverse(
            'aws-image-detail', kwargs={'uuid': image.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('aws-image-list')


class SizeFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Size

    name = factory.Sequence(lambda n: 'size%s' % n)
    backend_id = factory.sequence(lambda n: 'id-%s' % n)

    cores = fuzzy.FuzzyInteger(1, 8, step=2)
    ram = fuzzy.FuzzyInteger(1024, 10240, step=1024)
    disk = fuzzy.FuzzyInteger(1024, 102400, step=1024)
    price = fuzzy.FuzzyDecimal(0.5, 5, precision=2)

    @classmethod
    def get_url(cls, size=None):
        if size is None:
            size = SizeFactory()
        return 'http://testserver' + reverse(
            'aws-size-detail', kwargs={'uuid': size.uuid.hex}
        )

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('aws-size-list')


class InstanceFactory(factory.DjangoModelFactory):
    class Meta:
        model = models.Instance

    name = factory.Sequence(lambda n: 'instance%s' % n)
    backend_id = factory.Sequence(lambda n: 'instance-id%s' % n)
    service_settings = factory.SubFactory(
        structure_factories.ServiceSettingsFactory, type='Amazon'
    )
    region = factory.SubFactory(RegionFactory)

    state = models.Instance.States.OK
    runtime_state = NodeState.STOPPED
    cores = fuzzy.FuzzyInteger(1, 8, step=2)
    ram = fuzzy.FuzzyInteger(1024, 10240, step=1024)
    disk = fuzzy.FuzzyInteger(1024, 102400, step=1024)

    @classmethod
    def get_url(cls, instance=None, action=None):
        if instance is None:
            instance = InstanceFactory()
        url = 'http://testserver' + reverse(
            'aws-instance-detail', kwargs={'uuid': instance.uuid.hex}
        )
        return url if action is None else url + action + '/'

    @classmethod
    def get_list_url(cls):
        return 'http://testserver' + reverse('aws-instance-list')
