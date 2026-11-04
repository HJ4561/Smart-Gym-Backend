"""
ZATCA Phase 2 (e-invoicing) records.

  ZatcaConfig    the seller's legal details, the environment and the on/off switch (one row)
  ZatcaDevice    the EGS unit: its key and ZATCA certificates (encrypted) and its invoice chain
  ZatcaDocument  one signed e-invoice, credit note or debit note, and what ZATCA said about it
  ZatcaEvent     audit log of every ZATCA action (who, when, what — never a secret)
"""
from decimal import Decimal

from django.conf import settings
from django.db import models
from django.db.models import Q

from . import environments
from .xml_builder import FIRST_PIH, Seller


class ZatcaConfig(models.Model):
    enabled = models.BooleanField(default=False)
    enabled_at = models.DateTimeField(null=True, blank=True)     # payments before this are never invoiced
    environment = models.CharField(max_length=12, choices=environments.CHOICES, default='sandbox')

    seller_name_ar = models.CharField(max_length=150, blank=True)
    seller_name_en = models.CharField(max_length=150, blank=True)
    vat_number = models.CharField(max_length=15, blank=True)
    crn = models.CharField(max_length=10, blank=True)             # commercial registration number
    street = models.CharField(max_length=120, blank=True)
    building_number = models.CharField(max_length=4, blank=True)
    additional_number = models.CharField(max_length=4, blank=True)
    district = models.CharField(max_length=80, blank=True)
    city = models.CharField(max_length=80, blank=True)
    postal_code = models.CharField(max_length=5, blank=True)
    branch_name = models.CharField(max_length=80, default='Main Branch')
    business_category = models.CharField(max_length=60, default='Fitness')
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15.00'))
    updated_at = models.DateTimeField(auto_now=True)

    SANDBOX_DEFAULTS = {
        'seller_name_ar': 'سمارت جيم', 'seller_name_en': 'Smart Gym', 'vat_number': environments.SANDBOX_VAT,
        'crn': '1010010000', 'street': 'Prince Sultan', 'building_number': '2322', 'additional_number': '1234',
        'district': 'Al-Murabba', 'city': 'Riyadh', 'postal_code': '23333',
    }

    class Meta:
        db_table = 'zatca_config'

    @classmethod
    def load(cls):
        obj, _ = cls.objects.get_or_create(pk=1, defaults=cls.SANDBOX_DEFAULTS)
        return obj

    @property
    def is_test(self):
        return not environments.get(self.environment)['legal']

    @property
    def seller_name(self):
        return self.seller_name_ar or self.seller_name_en

    def seller(self):
        return Seller(self.seller_name, self.vat_number, self.crn, self.street, self.building_number,
                      self.district, self.city, self.postal_code, self.additional_number)

    @property
    def address_line(self):
        return ', '.join(p for p in (f'{self.building_number} {self.street}'.strip(), self.district, self.city,
                                     self.postal_code) if p)


class ZatcaDevice(models.Model):
    STATUS_CHOICES = [('onboarding', 'Onboarding'), ('active', 'Active'), ('archived', 'Archived')]

    environment = models.CharField(max_length=12, choices=environments.CHOICES)
    status = models.CharField(max_length=12, choices=STATUS_CHOICES, default='onboarding', db_index=True)
    common_name = models.CharField(max_length=64)
    serial = models.CharField(max_length=120)                    # 1-<solution>|2-<model>|3-<uuid>

    private_key_enc = models.TextField()                         # encrypted PEM
    csr = models.TextField(blank=True)
    compliance_token_enc = models.TextField(blank=True)
    compliance_secret_enc = models.TextField(blank=True)
    compliance_request_id = models.CharField(max_length=40, blank=True)
    compliance_results = models.JSONField(default=dict, blank=True)
    production_token_enc = models.TextField(blank=True)
    production_secret_enc = models.TextField(blank=True)

    certificate = models.TextField(blank=True)                   # production certificate body (public)
    certificate_issuer = models.CharField(max_length=255, blank=True)
    certificate_expires_at = models.DateTimeField(null=True, blank=True)

    last_icv = models.PositiveIntegerField(default=0)            # invoice counter value of the last document
    last_hash = models.TextField(default=FIRST_PIH)              # its hash = the next document's PIH

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    activated_at = models.DateTimeField(null=True, blank=True)
    archived_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'zatca_devices'
        ordering = ['-created_at']
        constraints = [models.UniqueConstraint(fields=['status'], condition=Q(status='active'),
                                               name='zatca_one_active_device')]

    def __str__(self):
        return f'{self.common_name} ({self.environment}, {self.status})'


class ZatcaDocument(models.Model):
    TYPE_CHOICES = [('invoice', 'Tax invoice'), ('credit', 'Credit note'), ('debit', 'Debit note')]
    STATUS_CHOICES = [('pending', 'Waiting to be sent'), ('reported', 'Reported'), ('rejected', 'Rejected')]

    device = models.ForeignKey(ZatcaDevice, on_delete=models.PROTECT, related_name='documents')
    doc_type = models.CharField(max_length=8, choices=TYPE_CHOICES, default='invoice')
    number = models.CharField(max_length=30, unique=True)
    uuid = models.UUIDField(unique=True)
    icv = models.PositiveIntegerField()
    issued_at = models.DateTimeField()
    pih = models.TextField()
    invoice_hash = models.CharField(max_length=64)
    signed_xml = models.TextField()
    qr = models.TextField()

    lines = models.JSONField(default=list)                       # [{name, qty, unit_net, net, vat}] in SAR
    net = models.DecimalField(max_digits=12, decimal_places=2)
    vat = models.DecimalField(max_digits=12, decimal_places=2)
    rounding = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=12, decimal_places=2)  # PayableAmount = what was paid / refunded
    vat_rate = models.DecimalField(max_digits=5, decimal_places=2, default=Decimal('15.00'))
    payment_means = models.CharField(max_length=4, default='48')
    payment_label = models.CharField(max_length=60, blank=True)
    customer_name = models.CharField(max_length=150, blank=True)

    transaction = models.ForeignKey('payments.Transaction', on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='zatca_documents')
    registration = models.ForeignKey('members.MembershipRegistration', on_delete=models.SET_NULL, null=True,
                                     blank=True, related_name='zatca_documents')
    original = models.ForeignKey('self', on_delete=models.PROTECT, null=True, blank=True, related_name='notes')
    reason = models.CharField(max_length=200, blank=True)

    status = models.CharField(max_length=10, choices=STATUS_CHOICES, default='pending', db_index=True)
    attempts = models.PositiveIntegerField(default=0)
    next_attempt_at = models.DateTimeField(null=True, blank=True, db_index=True)
    last_error = models.CharField(max_length=500, blank=True)
    zatca_warnings = models.JSONField(default=list, blank=True)
    zatca_errors = models.JSONField(default=list, blank=True)
    reported_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'zatca_documents'
        ordering = ['-icv']
        constraints = [
            models.UniqueConstraint(fields=['device', 'icv'], name='zatca_unique_icv'),
            # one tax invoice per payment — a retried hook can never invoice twice
            models.UniqueConstraint(fields=['transaction'], condition=Q(doc_type='invoice', transaction__isnull=False),
                                    name='zatca_one_invoice_per_transaction'),
            models.UniqueConstraint(fields=['registration'],
                                    condition=Q(doc_type='invoice', registration__isnull=False),
                                    name='zatca_one_invoice_per_registration'),
        ]

    def __str__(self):
        return f'{self.number} ({self.get_doc_type_display()}, {self.status})'

    @property
    def is_test(self):
        return not environments.get(self.device.environment)['legal']


class ZatcaEvent(models.Model):
    device = models.ForeignKey(ZatcaDevice, on_delete=models.SET_NULL, null=True, blank=True, related_name='events')
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+')
    action = models.CharField(max_length=60)
    detail = models.JSONField(default=dict, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'zatca_events'
        ordering = ['-created_at']
