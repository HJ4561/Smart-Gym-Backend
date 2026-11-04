"""
Register a ZATCA device in one go — the same steps as the go-live wizard:
new key + CSR → OTP → compliance certificate → 3 compliance checks → production certificate → active.

    python manage.py zatca_onboard --environment sandbox --otp 123345 [--b2b]
"""
from django.core.management.base import BaseCommand, CommandError

from apps.zatca import environments, service


class Command(BaseCommand):
    help = 'Onboard a new ZATCA device (EGS unit) and make it the active one.'

    def add_arguments(self, parser):
        parser.add_argument('--environment', default='sandbox', choices=list(environments.ENVIRONMENTS))
        parser.add_argument('--otp', required=True, help='6-digit OTP from the Fatoora portal (Sandbox: 123345)')
        parser.add_argument('--b2b', action='store_true', help='Also issue company (standard) tax invoices.')
        parser.add_argument('--yes-production', action='store_true',
                            help='Required for production: those invoices are legally real.')

    def handle(self, *args, **opts):
        env = opts['environment']
        if environments.get(env)['legal'] and not opts['yes_production']:
            raise CommandError('Production invoices are legally real — pass --yes-production to continue.')
        try:
            device = service.start_device(env, b2b=opts['b2b'])
            self.stdout.write(f'Device {device.common_name} created for {env}.')
            service.request_compliance(device, opts['otp'])
            self.stdout.write('Compliance certificate issued.')
            for doc_type, r in service.run_compliance_checks(device).items():
                self.stdout.write(f"  {doc_type}: {r['status']}{' — warnings: ' + '; '.join(r['warnings']) if r['warnings'] else ''}")
            service.request_production(device)
            self.stdout.write(f'Production certificate issued (expires {device.certificate_expires_at:%Y-%m-%d}).')
            service.activate(device)
        except service.OnboardingError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(self.style.SUCCESS(f'{device.common_name} is active — paid sales are now e-invoiced.'))
