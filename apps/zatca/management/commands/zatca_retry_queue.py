"""Resend ZATCA documents that are waiting (run every 5 minutes from cron / Task Scheduler)."""
from django.core.management.base import BaseCommand

from apps.zatca import service


class Command(BaseCommand):
    help = 'Report pending ZATCA e-invoices whose retry time has come.'

    def add_arguments(self, parser):
        parser.add_argument('--limit', type=int, default=100)

    def handle(self, *args, **opts):
        out = service.retry_due(limit=opts['limit'])
        self.stdout.write(f"Sent {out['sent']}: {out['reported']} reported, {out['waiting']} waiting, "
                          f"{out['rejected']} rejected.")
