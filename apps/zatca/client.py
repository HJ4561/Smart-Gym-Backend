"""
ZATCA e-invoicing API (Fatoora gateway). Every call returns a Result; nothing here logs tokens,
secrets, OTPs or invoice contents.

  compliance(csr, otp)                    → compliance certificate (CCSID)
  compliance_check(creds, hash, uuid, xml)
  production_csid(creds, request_id)      → production certificate (PCSID)
  report(creds, hash, uuid, xml)          → simplified (B2C) invoices, within 24 hours
"""
import base64
import json
import logging
import urllib.error
import urllib.request
from dataclasses import dataclass, field

from . import environments

logger = logging.getLogger(__name__)
TIMEOUT = 20
USER_AGENT = 'SmartGym-EGS/1.0'


@dataclass
class Result:
    status: int                  # HTTP status; 0 when ZATCA could not be reached
    data: dict = field(default_factory=dict)
    error: str = ''

    @property
    def ok(self):
        return self.status in (200, 202)

    @property
    def reachable(self):
        return self.status != 0 and self.status < 500

    @property
    def validation(self):
        return self.data.get('validationResults') or {}

    @property
    def warnings(self):
        return [m.get('message', '') for m in self.validation.get('warningMessages') or []]

    @property
    def errors(self):
        msgs = [m.get('message', '') for m in self.validation.get('errorMessages') or []]
        if not msgs and not self.ok:
            msgs = [self.error or self.data.get('message') or str(self.data.get('errors') or '') or f'HTTP {self.status}']
        return msgs


def _basic(token, secret):
    return 'Basic ' + base64.b64encode(f'{token}:{secret}'.encode()).decode()


def _post(environment, path, body, headers=None, timeout=TIMEOUT):
    url = environments.get(environment)['base_url'] + path
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method='POST', headers={
        'Accept': 'application/json', 'Content-Type': 'application/json', 'Accept-Version': 'V2',
        'Accept-Language': 'en', 'User-Agent': USER_AGENT, **(headers or {}),
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            return Result(resp.status, json.loads(raw) if raw else {})
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        logger.warning('ZATCA %s %s → HTTP %s', environment, path, exc.code)
        return Result(exc.code, data if isinstance(data, dict) else {'errors': data}, f'HTTP {exc.code}')
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        logger.warning('ZATCA %s %s unreachable: %s', environment, path, type(exc).__name__)
        return Result(0, {}, 'ZATCA could not be reached — it will be retried.')


def compliance(environment, csr_b64, otp):
    return _post(environment, '/compliance', {'csr': csr_b64}, {'OTP': str(otp)})


def compliance_check(environment, token, secret, invoice_hash, uuid, xml):
    return _post(environment, '/compliance/invoices',
                 {'invoiceHash': invoice_hash, 'uuid': uuid, 'invoice': base64.b64encode(xml.encode()).decode()},
                 {'Authorization': _basic(token, secret)})


def production_csid(environment, token, secret, compliance_request_id):
    return _post(environment, '/production/csids', {'compliance_request_id': str(compliance_request_id)},
                 {'Authorization': _basic(token, secret)})


def report(environment, token, secret, invoice_hash, uuid, xml, timeout=TIMEOUT):
    return _post(environment, '/invoices/reporting/single',
                 {'invoiceHash': invoice_hash, 'uuid': uuid, 'invoice': base64.b64encode(xml.encode()).decode()},
                 {'Authorization': _basic(token, secret), 'Clearance-Status': '0'}, timeout=timeout)
