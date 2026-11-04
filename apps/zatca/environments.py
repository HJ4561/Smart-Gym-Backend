"""
The three ZATCA environments, in one place: switching from Sandbox to Simulation to Production is a
setting (and a new device), never a code change.

  sandbox     developer portal — fixed OTP 123345, documents are only checked, nothing is legal
  simulation  the real flow with a real VAT number and an OTP from the Fatoora simulation portal
  production  legally real invoices
"""
GATEWAY = 'https://gw-fatoora.zatca.gov.sa/e-invoicing'

ENVIRONMENTS = {
    'sandbox': {
        'label': 'Sandbox (developer portal)',
        'base_url': f'{GATEWAY}/developer-portal',
        'template': 'TSTZATCA-Code-Signing',
        'legal': False,
    },
    'simulation': {
        'label': 'Simulation',
        'base_url': f'{GATEWAY}/simulation',
        'template': 'PREZATCA-Code-Signing',
        'legal': False,
    },
    'production': {
        'label': 'Production',
        'base_url': f'{GATEWAY}/core',
        'template': 'ZATCA-Code-Signing',
        'legal': True,
    },
}
CHOICES = [(k, v['label']) for k, v in ENVIRONMENTS.items()]

SANDBOX_OTP = '123345'
SANDBOX_VAT = '399999999900003'      # ZATCA's test VAT number; real numbers are refused in Sandbox


def get(env):
    return ENVIRONMENTS[env]
