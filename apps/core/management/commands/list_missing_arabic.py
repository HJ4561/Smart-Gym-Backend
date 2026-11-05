"""List every record whose English text has no Arabic yet (python manage.py list_missing_arabic)."""
from django.core.management.base import BaseCommand

from apps.core.missing_arabic import missing_arabic


class Command(BaseCommand):
    help = 'List products, courses, trainers, website texts… that have English text but no Arabic yet.'

    def add_arguments(self, parser):
        parser.add_argument('--counts', action='store_true', help='Only the number missing per kind.')
        parser.add_argument('--only', help='One kind, e.g. products, courses, site_texts.')

    def handle(self, *args, counts=False, only=None, **options):
        groups = missing_arabic()
        if only:
            groups = [g for g in groups if g['key'] == only]
            if not groups:
                self.stderr.write(f'Unknown kind "{only}". Kinds: ' + ', '.join(g['key'] for g in missing_arabic()))
                return
        total = sum(g['count'] for g in groups)
        for g in groups:
            if not g['count'] and not only:
                continue
            self.stdout.write(self.style.MIGRATE_HEADING(f"{g['label']} ({g['key']}): {g['count']}"))
            if counts:
                continue
            for item in g['items']:
                where = item['link'] or item['edit_url']
                self.stdout.write(f"  #{item['id']}  {item['name']}  -  missing: {', '.join(item['fields'])}  [{where}]")
        if total:
            self.stdout.write(self.style.WARNING(f'{total} record(s) still need Arabic.'))
        else:
            self.stdout.write(self.style.SUCCESS('Everything has Arabic.'))
