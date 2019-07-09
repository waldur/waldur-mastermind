from waldur_mastermind.marketplace import processors


class BookingCreateProcessor(processors.CreateResourceProcessor):
    def get_serializer_class(self):
        pass

    def get_viewset(self):
        pass

    def get_post_data(self):
        pass

    def get_scope_from_response(self, response):
        pass


class BookingDeleteProcessor(processors.CreateResourceProcessor):
    def get_viewset(self):
        pass

    def get_post_data(self):
        pass

    def get_scope_from_response(self, response):
        pass

    def get_serializer_class(self):
        pass
