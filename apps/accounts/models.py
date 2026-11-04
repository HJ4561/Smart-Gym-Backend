from django.contrib.auth.models import AbstractUser
from django.db import models


class User(AbstractUser):
    ROLE_CHOICES = [
        ('member', 'Member'),
        ('trainer', 'Trainer'),
        ('nutritionist', 'Nutritionist'),
        ('receptionist', 'Receptionist'),
        ('manager', 'Branch Manager'),
        ('admin', 'Super Admin'),
    ]

    email = models.EmailField(unique=True)
    phone = models.CharField(max_length=20, blank=True, null=True)
    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='member')
    avatar = models.ImageField(upload_to='avatars/', blank=True, null=True)
    date_of_birth = models.DateField(blank=True, null=True)
    is_verified = models.BooleanField(default=False)
    # Set when an account is given a temporary password (registration approval).
    # While True the API only allows changing the password (see accounts.authentication).
    # db_default so code deployed before this column existed can still insert users.
    must_change_password = models.BooleanField(default=False, db_default=False)
    # The language they last used the portal in (sent with every request) — for text sent outside one.
    language = models.CharField(max_length=2, choices=[('en', 'English'), ('ar', 'Arabic')], default='en',
                                db_default='en')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['username']

    class Meta:
        db_table = 'users'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.email} ({self.get_role_display()})"

    # ⬇️ ADD THIS PROPERTY
    @property
    def full_name(self):
        return f"{self.first_name} {self.last_name}".strip() or self.username

    @property
    def is_staff_member(self):
        return self.role in ['admin', 'manager']

    @property
    def is_member(self):
        return self.role == 'member'

class RateEvent(models.Model):
    """
    One counted event for rate limits that must hold across serverless instances
    (failed logins, website signups). Old rows are pruned as new ones are written.
    """
    kind = models.CharField(max_length=20)
    key = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    class Meta:
        db_table = 'rate_events'
        indexes = [models.Index(fields=['kind', 'key', 'created_at'])]


class TwoFactor(models.Model):
    """
    A person's second sign-in step: an authenticator app (TOTP, RFC 6238) or a code by email.
    Required for staff who sign in (admins, managers, coaches); members can switch it on.
    """
    METHOD_CHOICES = [('totp', 'Authenticator app'), ('email', 'Email code')]

    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='two_factor')
    method = models.CharField(max_length=10, choices=METHOD_CHOICES)
    secret = models.CharField(max_length=64, blank=True)          # base32 TOTP key (method=totp)
    enabled = models.BooleanField(default=False)                   # set once the first code is confirmed
    backup_codes = models.JSONField(default=list, blank=True)      # sha256 hashes of one-time codes
    last_totp_step = models.BigIntegerField(default=0)             # a TOTP code works once
    confirmed_at = models.DateTimeField(null=True, blank=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'two_factor'


class TwoFactorChallenge(models.Model):
    """
    A pending second step: after the password is right (login), or while turning 2FA on (setup).
    The browser holds only the ticket; both ticket and emailed code are stored hashed.
    """
    PURPOSE_CHOICES = [('login', 'Sign-in'), ('setup', 'Set up at sign-in'), ('enable', 'Turn on')]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='two_factor_challenges')
    purpose = models.CharField(max_length=10, choices=PURPOSE_CHOICES)
    ticket_hash = models.CharField(max_length=64, unique=True)
    method = models.CharField(max_length=10, blank=True)
    code_hash = models.CharField(max_length=64, blank=True)       # emailed code
    pending_secret = models.CharField(max_length=64, blank=True)  # TOTP key being set up
    attempts = models.PositiveSmallIntegerField(default=0)
    sends = models.PositiveSmallIntegerField(default=0)
    expires_at = models.DateTimeField()
    used = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'two_factor_challenges'


class TrustedDevice(models.Model):
    """
    "Trust this device for 30 days": after a correct 2-step code the browser keeps a random token;
    signing in again from it skips the code until it expires. Only a hash is stored. All of a
    person's devices are forgotten when their password or 2-step settings change.
    """
    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name='trusted_devices')
    token_hash = models.CharField(max_length=64, unique=True)
    label = models.CharField(max_length=120, blank=True)        # browser / OS, to recognise it
    created_at = models.DateTimeField(auto_now_add=True)
    last_used_at = models.DateTimeField(null=True, blank=True)
    expires_at = models.DateTimeField()

    class Meta:
        db_table = 'trusted_devices'
        ordering = ['-created_at']
