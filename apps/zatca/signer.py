"""
Signing, in pure Python (no Java SDK):

  1. invoice hash — the document without UBLExtensions, cac:Signature and the QR reference,
     canonicalized (C14N), SHA-256, base64 of the raw digest;
  2. signature   — ECDSA-SHA256 over the hash bytes with the device's private key;
  3. XAdES block — the certificate, its digest, the signing time, and the digest of those signed
     properties (ZATCA's digests of the certificate and the signed properties are base64 of the
     *hex* SHA-256, unlike the invoice hash);
  4. QR          — TLV tags 1–9, base64: seller, VAT no., time, amount due, VAT, hash, signature,
     public key, and the certificate's own signature (required on simplified invoices).
"""
import base64
import hashlib
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from lxml import etree

from .xml_builder import QR, UBL_EXTENSIONS

NS = {
    'ext': 'urn:oasis:names:specification:ubl:schema:xsd:CommonExtensionComponents-2',
    'cac': 'urn:oasis:names:specification:ubl:schema:xsd:CommonAggregateComponents-2',
    'cbc': 'urn:oasis:names:specification:ubl:schema:xsd:CommonBasicComponents-2',
}
LEFT_OUT = ('//ext:UBLExtensions', '//cac:Signature', "//cac:AdditionalDocumentReference[cbc:ID='QR']")


# ---------------------------------------------------------------- certificate
@dataclass
class Certificate:
    body: str                 # base64 DER, as ZATCA returns it (no PEM header)
    cert: x509.Certificate

    @classmethod
    def from_token(cls, binary_security_token):
        """ZATCA's binarySecurityToken is the base64 of the certificate's base64 body."""
        body = base64.b64decode(binary_security_token).decode().strip()
        return cls.from_body(body)

    @classmethod
    def from_body(cls, body):
        body = ''.join(body.replace('-----BEGIN CERTIFICATE-----', '').replace('-----END CERTIFICATE-----', '').split())
        return cls(body, x509.load_der_x509_certificate(base64.b64decode(body)))

    @property
    def issuer_name(self):
        return ', '.join(rdn.rfc4514_string() for rdn in reversed(self.cert.issuer.rdns))

    @property
    def serial_number(self):
        return str(self.cert.serial_number)

    @property
    def not_after(self):
        return self.cert.not_valid_after_utc

    @property
    def public_key_der(self):
        return self.cert.public_key().public_bytes(serialization.Encoding.DER,
                                                   serialization.PublicFormat.SubjectPublicKeyInfo)

    @property
    def digest(self):
        return base64.b64encode(hashlib.sha256(self.body.encode()).hexdigest().encode()).decode()


# ---------------------------------------------------------------- hash
def _remove_keeping_text(el):
    """Drop an element but keep the text after it, as the XPath transforms do."""
    parent, tail = el.getparent(), el.tail or ''
    prev = el.getprevious()
    if tail:
        if prev is not None:
            prev.tail = (prev.tail or '') + tail
        else:
            parent.text = (parent.text or '') + tail
    parent.remove(el)


def invoice_hash(xml_text):
    """Base64 SHA-256 of the canonical document without the signature, its extension and the QR."""
    root = etree.fromstring(xml_text.encode('utf-8'), etree.XMLParser(remove_blank_text=False, resolve_entities=False))
    for path in LEFT_OUT:
        for el in root.xpath(path, namespaces=NS):
            _remove_keeping_text(el)
    canonical = etree.tostring(root, method='c14n', exclusive=False, with_comments=False)
    return base64.b64encode(hashlib.sha256(canonical).digest()).decode()


# ---------------------------------------------------------------- QR
def _tlv(tag, value):
    raw = value if isinstance(value, bytes) else str(value).encode('utf-8')
    if len(raw) > 255:
        raise ValueError(f'QR tag {tag} is too long ({len(raw)} bytes).')
    return bytes([tag, len(raw)]) + raw


def qr_code(*, seller_name, vat_number, timestamp, total, vat, hash_b64, signature_b64, certificate):
    data = b''.join([
        _tlv(1, seller_name), _tlv(2, vat_number), _tlv(3, timestamp), _tlv(4, total), _tlv(5, vat),
        _tlv(6, hash_b64), _tlv(7, signature_b64), _tlv(8, certificate.public_key_der),
        _tlv(9, certificate.cert.signature),
    ])
    return base64.b64encode(data).decode()


def read_qr(b64):
    """{tag: value} — text tags decoded, binary tags (8, 9) left as bytes."""
    raw, out, i = base64.b64decode(b64), {}, 0
    while i < len(raw):
        tag, length = raw[i], raw[i + 1]
        value = raw[i + 2:i + 2 + length]
        out[tag] = value if tag in (8, 9) else value.decode('utf-8')
        i += 2 + length
    return out


# ---------------------------------------------------------------- XAdES
def _signed_properties(signing_time, certificate, *, for_hash):
    """The SignedProperties element; for the digest it carries its own namespace declarations."""
    x = ' xmlns:xades="http://uri.etsi.org/01903/v1.3.2#"' if for_hash else ''
    d = ' xmlns:ds="http://www.w3.org/2000/09/xmldsig#"' if for_hash else ''
    return (
        f'<xades:SignedProperties{x} Id="xadesSignedProperties">\n'
        f'                                    <xades:SignedSignatureProperties>\n'
        f'                                        <xades:SigningTime>{signing_time}</xades:SigningTime>\n'
        f'                                        <xades:SigningCertificate>\n'
        f'                                            <xades:Cert>\n'
        f'                                                <xades:CertDigest>\n'
        f'                                                    <ds:DigestMethod{d} Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>\n'
        f'                                                    <ds:DigestValue{d}>{certificate.digest}</ds:DigestValue>\n'
        f'                                                </xades:CertDigest>\n'
        f'                                                <xades:IssuerSerial>\n'
        f'                                                    <ds:X509IssuerName{d}>{certificate.issuer_name}</ds:X509IssuerName>\n'
        f'                                                    <ds:X509SerialNumber{d}>{certificate.serial_number}</ds:X509SerialNumber>\n'
        f'                                                </xades:IssuerSerial>\n'
        f'                                            </xades:Cert>\n'
        f'                                        </xades:SigningCertificate>\n'
        f'                                    </xades:SignedSignatureProperties>\n'
        f'                                </xades:SignedProperties>'
    )


def _ubl_extensions(hash_b64, signature_b64, signing_time, certificate):
    props_digest = base64.b64encode(hashlib.sha256(
        _signed_properties(signing_time, certificate, for_hash=True).encode()).hexdigest().encode()).decode()
    return f"""
    <ext:UBLExtensions>
        <ext:UBLExtension>
            <ext:ExtensionURI>urn:oasis:names:specification:ubl:dsig:enveloped:xades</ext:ExtensionURI>
            <ext:ExtensionContent>
                <sig:UBLDocumentSignatures xmlns:sig="urn:oasis:names:specification:ubl:schema:xsd:CommonSignatureComponents-2" xmlns:sac="urn:oasis:names:specification:ubl:schema:xsd:SignatureAggregateComponents-2" xmlns:sbc="urn:oasis:names:specification:ubl:schema:xsd:SignatureBasicComponents-2">
                    <sac:SignatureInformation>
                        <cbc:ID>urn:oasis:names:specification:ubl:signature:1</cbc:ID>
                        <sbc:ReferencedSignatureID>urn:oasis:names:specification:ubl:signature:Invoice</sbc:ReferencedSignatureID>
                        <ds:Signature xmlns:ds="http://www.w3.org/2000/09/xmldsig#" Id="signature">
                            <ds:SignedInfo>
                                <ds:CanonicalizationMethod Algorithm="http://www.w3.org/2006/12/xml-c14n11"/>
                                <ds:SignatureMethod Algorithm="http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256"/>
                                <ds:Reference Id="invoiceSignedData" URI="">
                                    <ds:Transforms>
                                        <ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116">
                                            <ds:XPath>not(//ancestor-or-self::ext:UBLExtensions)</ds:XPath>
                                        </ds:Transform>
                                        <ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116">
                                            <ds:XPath>not(//ancestor-or-self::cac:Signature)</ds:XPath>
                                        </ds:Transform>
                                        <ds:Transform Algorithm="http://www.w3.org/TR/1999/REC-xpath-19991116">
                                            <ds:XPath>not(//ancestor-or-self::cac:AdditionalDocumentReference[cbc:ID='QR'])</ds:XPath>
                                        </ds:Transform>
                                        <ds:Transform Algorithm="http://www.w3.org/2006/12/xml-c14n11"/>
                                    </ds:Transforms>
                                    <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>
                                    <ds:DigestValue>{hash_b64}</ds:DigestValue>
                                </ds:Reference>
                                <ds:Reference Type="http://www.w3.org/2000/09/xmldsig#SignatureProperties" URI="#xadesSignedProperties">
                                    <ds:DigestMethod Algorithm="http://www.w3.org/2001/04/xmlenc#sha256"/>
                                    <ds:DigestValue>{props_digest}</ds:DigestValue>
                                </ds:Reference>
                            </ds:SignedInfo>
                            <ds:SignatureValue>{signature_b64}</ds:SignatureValue>
                            <ds:KeyInfo>
                                <ds:X509Data>
                                    <ds:X509Certificate>{certificate.body}</ds:X509Certificate>
                                </ds:X509Data>
                            </ds:KeyInfo>
                            <ds:Object>
                                <xades:QualifyingProperties xmlns:xades="http://uri.etsi.org/01903/v1.3.2#" Target="signature">
                                {_signed_properties(signing_time, certificate, for_hash=False)}
                                </xades:QualifyingProperties>
                            </ds:Object>
                        </ds:Signature>
                    </sac:SignatureInformation>
                </sig:UBLDocumentSignatures>
            </ext:ExtensionContent>
        </ext:UBLExtension>
    </ext:UBLExtensions>"""


def _qr_reference(qr_b64):
    return f"""
    <cac:AdditionalDocumentReference>
        <cbc:ID>QR</cbc:ID>
        <cac:Attachment>
            <cbc:EmbeddedDocumentBinaryObject mimeCode="text/plain">{qr_b64}</cbc:EmbeddedDocumentBinaryObject>
        </cac:Attachment>
    </cac:AdditionalDocumentReference>"""


# ---------------------------------------------------------------- sign
@dataclass
class Signed:
    xml: str
    invoice_hash: str
    signature: str
    qr: str


def sign(unsigned_xml, *, private_key, certificate, seller_name, vat_number, timestamp, total, vat, signing_time):
    """
    `unsigned_xml` comes from xml_builder.build() (with its two markers). `timestamp` is the QR
    time (IssueDate + 'T' + IssueTime); `total` and `vat` are SAR strings ("115.00").
    """
    if UBL_EXTENSIONS not in unsigned_xml or QR not in unsigned_xml:
        raise ValueError('The XML has no signature/QR markers — build it with xml_builder.build().')
    # Hash the document in its final shape: the left-out parts are placeholders here, so the
    # text around them (and so the hash) is exactly what the signed document will have.
    shaped = unsigned_xml.replace(UBL_EXTENSIONS, '\n    <ext:UBLExtensions/>').replace(
        QR, "\n    <cac:AdditionalDocumentReference><cbc:ID>QR</cbc:ID></cac:AdditionalDocumentReference>")
    hash_b64 = invoice_hash(shaped)
    signature_b64 = base64.b64encode(
        private_key.sign(base64.b64decode(hash_b64), ec.ECDSA(hashes.SHA256()))).decode()
    qr = qr_code(seller_name=seller_name, vat_number=vat_number, timestamp=timestamp, total=total, vat=vat,
                 hash_b64=hash_b64, signature_b64=signature_b64, certificate=certificate)
    xml = (unsigned_xml
           .replace(UBL_EXTENSIONS, _ubl_extensions(hash_b64, signature_b64, signing_time, certificate))
           .replace(QR, _qr_reference(qr)))
    return Signed(xml=xml, invoice_hash=hash_b64, signature=signature_b64, qr=qr)
