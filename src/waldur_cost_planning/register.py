class Register(object):
    _optimizers = {}
    _serializers = {}

    @classmethod
    def register_optimizer(cls, service_settings_type, optimizer):
        cls._optimizers[service_settings_type] = optimizer

    @classmethod
    def register_serializer(cls, service_settings_type, serializer):
        cls._serializers[service_settings_type] = serializer

    @classmethod
    def get_optimizer(cls, service_settings_type):
        return cls._optimizers.get(service_settings_type)

    @classmethod
    def get_serilizer(cls, service_settings_type):
        return cls._serializers.get(service_settings_type)
