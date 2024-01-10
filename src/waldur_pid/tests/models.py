from .. import mixins


class Offering(mixins.DataciteMixin):
    def get_datacite_title(self):
        return "test offering"

    def get_datacite_creators_name(self):
        return "Test company"

    def get_datacite_description(self):
        return "Description of test offering"

    def get_datacite_publication_year(self):
        return "2020"

    def get_datacite_url(self):
        return "http://example.com/offerings/test"
