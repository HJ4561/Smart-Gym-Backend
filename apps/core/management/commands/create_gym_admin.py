"""
Create the first admin of a gym side (the female gym starts with no accounts, so nobody can add one
from its admin portal yet).

    python manage.py create_gym_admin --side female --email admin@example.com --first-name Huda

Asks for the password (typed twice, not shown). The account signs in at <that side's website>/admin/login
and can then add the rest of its team from Team. Refuses an email already used at either gym.
"""
import getpass

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.core.sides import SIDES, using_side


class Command(BaseCommand):
    help = 'Create an admin account on the male or female gym side.'

    def add_arguments(self, parser):
        parser.add_argument('--side', required=True, choices=SIDES)
        parser.add_argument('--email', required=True)
        parser.add_argument('--first-name', required=True)
        parser.add_argument('--last-name', default='')
        parser.add_argument('--role', default='admin', choices=['admin', 'manager'])

    def handle(self, *args, **opts):
        from apps.accounts.models import User
        from apps.core.validators import email_error
        from apps.staff.models import StaffProfile

        email = opts['email'].strip().lower()
        msg = email_error(email)
        if msg:
            raise CommandError(msg)
        if User._base_manager.filter(email__iexact=email).exists():
            raise CommandError('This email already has an account (at one of the two gyms).')
        password = getpass.getpass('Password: ')
        if password != getpass.getpass('Password again: '):
            raise CommandError('The two passwords are different.')
        user = User(email=email, username=email, role=opts['role'], first_name=opts['first_name'].strip(),
                    last_name=opts['last_name'].strip(), side=opts['side'], is_verified=True, is_active=True)
        try:
            validate_password(password, user)
        except ValidationError as e:
            raise CommandError(' '.join(e.messages))
        user.set_password(password)
        with using_side(opts['side']), transaction.atomic():
            user.save()
            StaffProfile.objects.create(user=user, department='executive' if opts['role'] == 'admin' else 'operations')
        self.stdout.write(self.style.SUCCESS(
            f'{opts["role"].title()} {email} created on the {opts["side"]} side. Sign in at that gym\'s website /admin/login.'))
