import logging
import urlparse

from django.conf import settings as django_settings
from django.utils import timezone
import lxml.etree  # nosec
import requests


logger = logging.getLogger(__name__)


class ClientError(Exception):
    pass


class ResponseParseError(ClientError):
    pass


class ResponseStatusError(ClientError):
    pass


class RequestError(ClientError):
    def __init__(self, message, response):
        super(RequestError, self).__init__(message)
        self.response = response


class UnknownStatusError(ResponseParseError):
    pass


class Response(object):
    ns_namespace = 'http://uri.etsi.org/TS102204/v1.1.2#'

    def __init__(self, content):
        etree = lxml.etree.fromstring(content)  # nosec
        self.init_response_attributes(etree)

    def init_response_attributes(self, etree):
        """ Define response attributes based on valimo request content """
        raise NotImplementedError


class Request(object):
    url = NotImplemented
    template = NotImplemented
    response_class = NotImplemented
    settings = getattr(django_settings, 'WALDUR_AUTH_VALIMO', {})

    @classmethod
    def execute(cls, **kwargs):
        url = cls._get_url()
        headers = {
            'content-type': 'text/xml',
            'SOAPAction': url,
        }
        data = cls.template.strip().format(
            AP_ID=cls.settings['AP_ID'],
            AP_PWD=cls.settings['AP_PWD'],
            Instant=cls._format_datetime(timezone.now()),
            DNSName=cls.settings['DNSName'],
            **kwargs
        )
        cert = (cls.settings['cert_path'], cls.settings['key_path'])
        # TODO: add verification
        logger.debug('Executing POST request to %s with data:\n %s \nheaders: %s', url, data, headers)
        response = requests.post(url, data=data, headers=headers, cert=cert, verify=cls.settings['verify_ssl'])
        if response.ok:
            return cls.response_class(response.content)
        else:
            message = 'Failed to execute POST request against %s endpoint. Response [%s]: %s' % (
                url, response.status_code, response.content)
            raise RequestError(message, response)

    @classmethod
    def _format_datetime(cls, d):
        return d.strftime('%Y-%m-%dT%H:%M:%S.000Z')

    @classmethod
    def _format_transaction_id(cls, transaction_id):
        return ('_' + transaction_id)[:32]  # such formation is required by server.

    @classmethod
    def _get_url(cls):
        return urlparse.urljoin(cls.settings['URL'], cls.url)


class SignatureResponse(Response):

    def init_response_attributes(self, etree):
        try:
            self.backend_transaction_id = etree.xpath('//MSS_SignatureResp')[0].attrib['MSSP_TransID']
            self.status = etree.xpath('//ns6:StatusCode', namespaces={'ns6': self.ns_namespace})[0].attrib['Value']
        except (IndexError, KeyError, lxml.etree.XMLSchemaError) as e:
            raise ResponseParseError('Cannot parse signature response: %s. Response content: %s' % (
                e, lxml.etree.tostring(etree)))


class SignatureRequest(Request):
    url = '/MSSP/services/MSS_Signature'
    template = """
        <?xml version="1.0" encoding="UTF-8"?>
        <soapenv:Envelope xmlns:soapenv="http://www.w3.org/2003/05/soap-envelope"
                          xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                          xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <soapenv:Body>
                <MSS_Signature xmlns="">
                <MSS_SignatureReq MajorVersion="1" MessagingMode="{MessagingMode}" MinorVersion="1" TimeOut="300">
                    <ns1:AP_Info AP_ID="{AP_ID}" AP_PWD="{AP_PWD}" AP_TransID="{AP_TransID}"
                                 Instant="{Instant}" xmlns:ns1="http://uri.etsi.org/TS102204/v1.1.2#"/>
                    <ns2:MSSP_Info xmlns:ns2="http://uri.etsi.org/TS102204/v1.1.2#">
                        <ns2:MSSP_ID>
                            <ns2:DNSName>{DNSName}</ns2:DNSName>
                        </ns2:MSSP_ID>
                    </ns2:MSSP_Info>
                    <ns3:MobileUser xmlns:ns3="http://uri.etsi.org/TS102204/v1.1.2#">
                        <ns3:MSISDN>{MSISDN}</ns3:MSISDN>
                    </ns3:MobileUser>
                    <ns4:DataToBeSigned Encoding="UTF-8" MimeType="text/plain" xmlns:ns4="http://uri.etsi.org/TS102204/v1.1.2#">
                        {DataToBeSigned}
                    </ns4:DataToBeSigned>
                    <ns5:SignatureProfile xmlns:ns5="http://uri.etsi.org/TS102204/v1.1.2#">
                        <ns5:mssURI>{SignatureProfile}</ns5:mssURI>
                    </ns5:SignatureProfile>
                    <ns6:MSS_Format xmlns:ns6="http://uri.etsi.org/TS102204/v1.1.2#">
                        <ns6:mssURI>http://uri.etsi.org/TS102204/v1.1.2#PKCS7</ns6:mssURI>
                    </ns6:MSS_Format>
                </MSS_SignatureReq>
                </MSS_Signature>
            </soapenv:Body>
        </soapenv:Envelope>
    """
    response_class = SignatureResponse

    @classmethod
    def execute(cls, transaction_id, phone, message):
        kwargs = {
            'MessagingMode': 'asynchClientServer',
            'AP_TransID': cls._format_transaction_id(transaction_id),
            'MSISDN': phone,
            'DataToBeSigned': '%s %s' % (cls.settings['message_prefix'], message),
            'SignatureProfile': cls.settings['SignatureProfile']
        }
        return super(SignatureRequest, cls).execute(**kwargs)


class Statuses(object):
    OK = 'OK'
    PROCESSING = 'Processing'
    ERRED = 'Erred'

    @classmethod
    def map(cls, status_code):
        if status_code == '502':
            return cls.OK
        elif status_code == '504':
            return cls.PROCESSING
        else:
            raise UnknownStatusError('Received unsupported status in response: %s' % status_code)


class StatusResponse(Response):

    def init_response_attributes(self, etree):
        try:
            status_code = etree.xpath('//ns5:StatusCode', namespaces={'ns5': self.ns_namespace})[0].attrib['Value']
        except (IndexError, KeyError, lxml.etree.XMLSchemaError) as e:
            raise ResponseParseError('Cannot parse status response: %s. Response content: %s' % (
                e, lxml.etree.tostring(etree)))
        self.status = Statuses.map(status_code)

        try:
            civil_number_tag = etree.xpath('//ns4:UserIdentifier', namespaces={'ns4': self.ns_namespace})[0]
        except IndexError:
            # civil number tag does not exist - this is possible if request is still processing
            return
        else:
            try:
                self.civil_number = civil_number_tag.text.split('=')[1]
            except IndexError:
                raise ResponseParseError('Cannot get civil_number from tag text: %s' % civil_number_tag.text)


class ErredStatusResponse(Response):
    soapenv_namespace = 'http://www.w3.org/2003/05/soap-envelope'

    def init_response_attributes(self, etree):
        self.status = Statuses.ERRED
        try:
            self.details = etree.xpath('//soapenv:Text', namespaces={'soapenv': self.soapenv_namespace})[0].text
        except (IndexError, lxml.etree.XMLSchemaError) as e:
            raise ResponseParseError('Cannot parse error status response: %s. Response content: %s' % (
                e, lxml.etree.tostring(etree)))


class StatusRequest(Request):
    url = '/MSSP/services/MSS_StatusPort'
    template = """
        <?xml version="1.0" encoding="UTF-8"?>
        <soapenv:Envelope xmlns:soapenv="http://www.w3.org/2003/05/soap-envelope"
                              xmlns:xsd="http://www.w3.org/2001/XMLSchema"
                              xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">
            <soapenv:Body>
            <MSS_StatusQuery xmlns="">
            <MSS_StatusReq MajorVersion="1" MinorVersion="1">
                <ns1:AP_Info AP_ID="{AP_ID}" AP_PWD="{AP_PWD}" AP_TransID="{AP_TransID}"
                                 Instant="{Instant}" xmlns:ns1="http://uri.etsi.org/TS102204/v1.1.2#"/>
                <ns2:MSSP_Info xmlns:ns2="http://uri.etsi.org/TS102204/v1.1.2#">
                    <ns2:MSSP_ID>
                        <ns2:DNSName>{DNSName}</ns2:DNSName>
                    </ns2:MSSP_ID>
                </ns2:MSSP_Info>
                <ns3:MSSP_TransID xmlns:ns3="http://uri.etsi.org/TS102204/v1.1.2#">{MSSP_TransID}</ns3:MSSP_TransID>
            </MSS_StatusReq>
            </MSS_StatusQuery>
            </soapenv:Body>
        </soapenv:Envelope>
    """
    response_class = StatusResponse

    @classmethod
    def execute(cls, transaction_id, backend_transaction_id):
        kwargs = {
            'AP_TransID': cls._format_transaction_id(transaction_id),
            'MSSP_TransID': backend_transaction_id,
        }
        try:
            return super(StatusRequest, cls).execute(**kwargs)
        except RequestError as e:
            # If request was timed out or user canceled login - Valimo would return response with status 500
            return ErredStatusResponse(e.response.content)
