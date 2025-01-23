from waldur_mastermind.marketplace import processors


class CreateAllocationProcessor(processors.BasicCreateResourceProcessor):
    def process_order(self, user):
        # waldur-site-agent is responsible for order approval and processing
        pass


class DeleteAllocationProcessor(processors.BasicDeleteResourceProcessor):
    def send_request(self, user, resource):
        # waldur-site-agent is responsible for order approval and processing
        return False


class UpdateAllocationLimitsProcessor(processors.BasicUpdateResourceProcessor):
    def send_request(self, user):
        # waldur-site-agent is responsible for order approval and processing
        return False
