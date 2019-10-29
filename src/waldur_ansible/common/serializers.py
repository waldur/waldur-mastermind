from rest_framework import serializers

from waldur_core.core import serializers as core_serializers


class ApplicationSerializerRegistry:
    """
    Holds application-related model-serializer mappings
    """
    APPLICATION_SERIALIZERS = dict()

    @staticmethod
    def get_registered_app_serializer(model_class):
        return ApplicationSerializerRegistry.APPLICATION_SERIALIZERS[model_class]

    @staticmethod
    def register(model_class, application_serializer):
        ApplicationSerializerRegistry.APPLICATION_SERIALIZERS[model_class] = application_serializer


class ApplicationSerializerMetaclass(serializers.SerializerMetaclass):
    """ Registers model-serializer mapper for various types of application serializers
    """

    def __new__(cls, name, bases, args):
        serializer = super(ApplicationSerializerMetaclass, cls).__new__(cls, name, bases, args)
        ApplicationSerializerRegistry.register(args['Meta'].model, serializer)
        return serializer


class BaseApplicationSerializer(core_serializers.AugmentedSerializerMixin,
                                serializers.HyperlinkedModelSerializer,
                                metaclass=ApplicationSerializerMetaclass):
    class Meta:
        model = NotImplemented


class SummaryApplicationSerializer(core_serializers.BaseSummarySerializer):
    @classmethod
    def get_serializer(cls, model):
        return ApplicationSerializerRegistry.get_registered_app_serializer(model)
