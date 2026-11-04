"""
UBL 2.1 XML for ZATCA tax invoices, credit notes and debit notes, always in SAR: simplified (B2C,
reported) and — with a buyer — standard (B2B, cleared by ZATCA before handover; it carries the
buyer's legal details and the supply date).

The XML is written as text with fixed indentation: the signer hashes the document as it stands, so
the bytes sent to ZATCA must be exactly the bytes that were hashed. `build()` leaves two markers —
UBL_EXTENSIONS and QR — that the signer fills in; both sit inside parts the hash leaves out.
"""
from dataclasses import dataclass, field
from xml.sax.saxutils import escape

from .vat import Breakdown, to_sar

TYPE_CODES = {'invoice': '388', 'credit': '381', 'debit': '383'}
SIMPLIFIED = '0200000'          # simplified tax invoice, no third-party / nominal / export / summary / self-billed
STANDARD = '0100000'            # standard tax invoice (B2B)

UBL_EXTENSIONS = '<!--UBL_EXTENSIONS-->'
QR = '<!--QR-->'

# The PIH of a device's first document: base64 of the hex SHA-256 of "0".
FIRST_PIH = 'NWZlY2ViNjZmZmM4NmYzOGQ5NTI3ODZjNmQ2OTZjNzljMmRiYzIzOWRkNGU5MWI0NjcyOWQ3M2EyN2ZiNTdlOQ=='

# UNTDID 4461 payment means
CASH, CARD, NOT_DEFINED = '10', '48', '1'


@dataclass
class Seller:
    name: str
    vat_number: str
    crn: str
    street: str
    building_number: str
    district: str
    city: str
    postal_code: str
    additional_number: str = ''


@dataclass
class Buyer:
    """A VAT-registered company buying from the gym (standard tax invoice)."""
    name: str
    vat_number: str
    street: str
    building_number: str
    district: str
    city: str
    postal_code: str
    crn: str = ''
    additional_number: str = ''


@dataclass
class Document:
    doc_type: str                  # invoice | credit | debit
    number: str
    uuid: str
    issued_at: object              # aware datetime, already in gym (Riyadh) time
    icv: int
    pih: str
    seller: Seller
    breakdown: Breakdown
    payment_means: str = CARD
    billing_reference: str = ''    # credit / debit: the original invoice number
    reason: str = ''               # credit / debit: why it was issued
    notes: list = field(default_factory=list)
    buyer: Buyer = None            # set → standard (B2B) tax invoice

    @property
    def standard(self):
        return self.buyer is not None


def _e(text):
    return escape(str(text or ''))


def _sar(halalas):
    return f'{to_sar(halalas):.2f}'


def _tax_category(tag, rate):
    return (f'<cac:{tag}>\n'
            f'                <cbc:ID>S</cbc:ID>\n'
            f'                <cbc:Percent>{rate:.2f}</cbc:Percent>\n'
            f'                <cac:TaxScheme>\n'
            f'                    <cbc:ID>VAT</cbc:ID>\n'
            f'                </cac:TaxScheme>\n'
            f'            </cac:{tag}>')


def _customer(buyer):
    if buyer is None:
        return ''
    ident = (f'\n            <cac:PartyIdentification>\n'
             f'                <cbc:ID schemeID="CRN">{_e(buyer.crn)}</cbc:ID>\n'
             f'            </cac:PartyIdentification>') if buyer.crn else ''
    plot = (f'\n                <cbc:PlotIdentification>{_e(buyer.additional_number)}</cbc:PlotIdentification>'
            if buyer.additional_number else '')
    return f"""
        <cac:Party>{ident}
            <cac:PostalAddress>
                <cbc:StreetName>{_e(buyer.street)}</cbc:StreetName>
                <cbc:BuildingNumber>{_e(buyer.building_number)}</cbc:BuildingNumber>{plot}
                <cbc:CitySubdivisionName>{_e(buyer.district)}</cbc:CitySubdivisionName>
                <cbc:CityName>{_e(buyer.city)}</cbc:CityName>
                <cbc:PostalZone>{_e(buyer.postal_code)}</cbc:PostalZone>
                <cac:Country>
                    <cbc:IdentificationCode>SA</cbc:IdentificationCode>
                </cac:Country>
            </cac:PostalAddress>
            <cac:PartyTaxScheme>
                <cbc:CompanyID>{_e(buyer.vat_number)}</cbc:CompanyID>
                <cac:TaxScheme>
                    <cbc:ID>VAT</cbc:ID>
                </cac:TaxScheme>
            </cac:PartyTaxScheme>
            <cac:PartyLegalEntity>
                <cbc:RegistrationName>{_e(buyer.name)}</cbc:RegistrationName>
            </cac:PartyLegalEntity>
        </cac:Party>"""


def build(doc: Document):
    if doc.doc_type not in TYPE_CODES:
        raise ValueError(f'Unknown document type {doc.doc_type!r}')
    if doc.doc_type != 'invoice' and not (doc.billing_reference and doc.reason):
        raise ValueError('Credit and debit notes need the original invoice number and a reason.')
    b, s = doc.breakdown, doc.seller
    rate = b.rate_percent
    date, time = doc.issued_at.strftime('%Y-%m-%d'), doc.issued_at.strftime('%H:%M:%S')

    billing = ''
    if doc.billing_reference:
        billing = (f'\n    <cac:BillingReference>\n'
                   f'        <cac:InvoiceDocumentReference>\n'
                   f'            <cbc:ID>{_e(doc.billing_reference)}</cbc:ID>\n'
                   f'        </cac:InvoiceDocumentReference>\n'
                   f'    </cac:BillingReference>')
    notes = ''.join(f'\n    <cbc:Note languageID="{lang}">{_e(text)}</cbc:Note>' for lang, text in doc.notes)
    instruction = f'\n        <cbc:InstructionNote>{_e(doc.reason)}</cbc:InstructionNote>' if doc.doc_type != 'invoice' else ''
    plot = (f'\n                <cbc:PlotIdentification>{_e(s.additional_number)}</cbc:PlotIdentification>'
            if s.additional_number else '')
    rounding = (f'\n        <cbc:PayableRoundingAmount currencyID="SAR">{_sar(b.rounding)}</cbc:PayableRoundingAmount>'
                if b.rounding else '')
    customer = _customer(doc.buyer)
    delivery = (f'\n    <cac:Delivery>\n        <cbc:ActualDeliveryDate>{date}</cbc:ActualDeliveryDate>\n    </cac:Delivery>'
                if doc.standard else '')

    lines = []
    for i, line in enumerate(b.lines, start=1):
        lines.append(f"""
    <cac:InvoiceLine>
        <cbc:ID>{i}</cbc:ID>
        <cbc:InvoicedQuantity unitCode="PCE">{line.quantity:.6f}</cbc:InvoicedQuantity>
        <cbc:LineExtensionAmount currencyID="SAR">{_sar(line.net)}</cbc:LineExtensionAmount>
        <cac:TaxTotal>
            <cbc:TaxAmount currencyID="SAR">{_sar(line.vat)}</cbc:TaxAmount>
            <cbc:RoundingAmount currencyID="SAR">{_sar(line.gross)}</cbc:RoundingAmount>
        </cac:TaxTotal>
        <cac:Item>
            <cbc:Name>{_e(line.name)}</cbc:Name>
            {_tax_category('ClassifiedTaxCategory', rate)}
        </cac:Item>
        <cac:Price>
            <cbc:PriceAmount currencyID="SAR">{_sar(line.unit_net)}</cbc:PriceAmount>
        </cac:Price>
    </cac:InvoiceLine>""")

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<Invoice xmlns="urn:oasis:names:specification:ubl:schema:xsd:Invoice-2" xmlns:cac="urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2" xmlns:cbc="urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2" xmlns:ext="urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2">{UBL_EXTENSIONS}
    <cbc:ProfileID>reporting:1.0</cbc:ProfileID>
    <cbc:ID>{_e(doc.number)}</cbc:ID>
    <cbc:UUID>{_e(doc.uuid)}</cbc:UUID>
    <cbc:IssueDate>{date}</cbc:IssueDate>
    <cbc:IssueTime>{time}</cbc:IssueTime>
    <cbc:InvoiceTypeCode name="{STANDARD if doc.standard else SIMPLIFIED}">{TYPE_CODES[doc.doc_type]}</cbc:InvoiceTypeCode>{notes}
    <cbc:DocumentCurrencyCode>SAR</cbc:DocumentCurrencyCode>
    <cbc:TaxCurrencyCode>SAR</cbc:TaxCurrencyCode>{billing}
    <cac:AdditionalDocumentReference>
        <cbc:ID>ICV</cbc:ID>
        <cbc:UUID>{doc.icv}</cbc:UUID>
    </cac:AdditionalDocumentReference>
    <cac:AdditionalDocumentReference>
        <cbc:ID>PIH</cbc:ID>
        <cac:Attachment>
            <cbc:EmbeddedDocumentBinaryObject mimeCode="text/plain">{doc.pih}</cbc:EmbeddedDocumentBinaryObject>
        </cac:Attachment>
    </cac:AdditionalDocumentReference>{QR}
    <cac:Signature>
        <cbc:ID>urn:oasis:names:specification:ubl:signature:Invoice</cbc:ID>
        <cbc:SignatureMethod>urn:oasis:names:specification:ubl:dsig:enveloped:xades</cbc:SignatureMethod>
    </cac:Signature>
    <cac:AccountingSupplierParty>
        <cac:Party>
            <cac:PartyIdentification>
                <cbc:ID schemeID="CRN">{_e(s.crn)}</cbc:ID>
            </cac:PartyIdentification>
            <cac:PostalAddress>
                <cbc:StreetName>{_e(s.street)}</cbc:StreetName>
                <cbc:BuildingNumber>{_e(s.building_number)}</cbc:BuildingNumber>{plot}
                <cbc:CitySubdivisionName>{_e(s.district)}</cbc:CitySubdivisionName>
                <cbc:CityName>{_e(s.city)}</cbc:CityName>
                <cbc:PostalZone>{_e(s.postal_code)}</cbc:PostalZone>
                <cac:Country>
                    <cbc:IdentificationCode>SA</cbc:IdentificationCode>
                </cac:Country>
            </cac:PostalAddress>
            <cac:PartyTaxScheme>
                <cbc:CompanyID>{_e(s.vat_number)}</cbc:CompanyID>
                <cac:TaxScheme>
                    <cbc:ID>VAT</cbc:ID>
                </cac:TaxScheme>
            </cac:PartyTaxScheme>
            <cac:PartyLegalEntity>
                <cbc:RegistrationName>{_e(s.name)}</cbc:RegistrationName>
            </cac:PartyLegalEntity>
        </cac:Party>
    </cac:AccountingSupplierParty>
    <cac:AccountingCustomerParty>{customer}
    </cac:AccountingCustomerParty>{delivery}
    <cac:PaymentMeans>
        <cbc:PaymentMeansCode>{_e(doc.payment_means)}</cbc:PaymentMeansCode>{instruction}
    </cac:PaymentMeans>
    <cac:TaxTotal>
        <cbc:TaxAmount currencyID="SAR">{_sar(b.vat)}</cbc:TaxAmount>
    </cac:TaxTotal>
    <cac:TaxTotal>
        <cbc:TaxAmount currencyID="SAR">{_sar(b.vat)}</cbc:TaxAmount>
        <cac:TaxSubtotal>
            <cbc:TaxableAmount currencyID="SAR">{_sar(b.net)}</cbc:TaxableAmount>
            <cbc:TaxAmount currencyID="SAR">{_sar(b.vat)}</cbc:TaxAmount>
            {_tax_category('TaxCategory', rate)}
        </cac:TaxSubtotal>
    </cac:TaxTotal>
    <cac:LegalMonetaryTotal>
        <cbc:LineExtensionAmount currencyID="SAR">{_sar(b.net)}</cbc:LineExtensionAmount>
        <cbc:TaxExclusiveAmount currencyID="SAR">{_sar(b.net)}</cbc:TaxExclusiveAmount>
        <cbc:TaxInclusiveAmount currencyID="SAR">{_sar(b.inclusive)}</cbc:TaxInclusiveAmount>
        <cbc:AllowanceTotalAmount currencyID="SAR">0.00</cbc:AllowanceTotalAmount>
        <cbc:PrepaidAmount currencyID="SAR">0.00</cbc:PrepaidAmount>{rounding}
        <cbc:PayableAmount currencyID="SAR">{_sar(b.paid)}</cbc:PayableAmount>
    </cac:LegalMonetaryTotal>{''.join(lines)}
</Invoice>"""
