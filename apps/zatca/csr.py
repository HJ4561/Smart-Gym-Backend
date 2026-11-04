"""
The device's key pair and certificate signing request (CSR), in ZATCA's format.

The key is ECDSA on secp256k1. The CSR carries the seller in its subject and, in a directory-name
subjectAltName, the device serial, the VAT number, the invoice types it issues, its address and
its business category — plus the certificate template name for the environment.
"""
import base64

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID, ObjectIdentifier

from . import environments

TEMPLATE_OID = ObjectIdentifier('1.3.6.1.4.1.311.20.2')
REGISTERED_ADDRESS = ObjectIdentifier('2.5.4.26')

# Invoice types a device issues (4 flags): standard (B2B), simplified (B2C), reserved, reserved.
SIMPLIFIED_ONLY = '0100'


def new_private_key():
    return ec.generate_private_key(ec.SECP256K1())


def private_key_pem(key):
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


def load_private_key(pem):
    return serialization.load_pem_private_key(pem.encode(), password=None)


def _printable_string(text):
    raw = text.encode('ascii')
    if len(raw) > 127:
        raise ValueError('Template name too long.')
    return bytes([0x13, len(raw)]) + raw


def build_csr(key, *, environment, common_name, serial, organization, organization_unit, vat_number,
              invoice_types=SIMPLIFIED_ONLY, address, business_category):
    """Returns the CSR as PEM text."""
    subject = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, 'SA'),
        x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, organization_unit),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, organization),
        x509.NameAttribute(NameOID.COMMON_NAME, common_name),
    ])
    alt = x509.DirectoryName(x509.Name([
        x509.RelativeDistinguishedName([x509.NameAttribute(NameOID.SURNAME, serial)]),
        x509.RelativeDistinguishedName([x509.NameAttribute(NameOID.USER_ID, vat_number)]),
        x509.RelativeDistinguishedName([x509.NameAttribute(NameOID.TITLE, invoice_types)]),
        x509.RelativeDistinguishedName([x509.NameAttribute(REGISTERED_ADDRESS, address)]),
        x509.RelativeDistinguishedName([x509.NameAttribute(NameOID.BUSINESS_CATEGORY, business_category)]),
    ]))
    template = environments.get(environment)['template']
    csr = (x509.CertificateSigningRequestBuilder()
           .subject_name(subject)
           .add_extension(x509.UnrecognizedExtension(TEMPLATE_OID, _printable_string(template)), critical=False)
           .add_extension(x509.SubjectAlternativeName([alt]), critical=False)
           .sign(key, hashes.SHA256()))
    return csr.public_bytes(serialization.Encoding.PEM).decode()


def csr_for_api(csr_pem):
    """ZATCA wants the whole PEM text base64-encoded."""
    return base64.b64encode(csr_pem.encode()).decode()
