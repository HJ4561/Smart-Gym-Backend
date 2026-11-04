from django.db import models
from django.conf import settings
from decimal import Decimal


class PaymentMethod(models.Model):
    """Saved payment methods for a member."""
    METHOD_CHOICES = [
        ('card', 'Credit/Debit Card'),
        ('apple_pay', 'Apple Pay'),
        ('google_pay', 'Google Pay'),
        ('locker_credit', 'Locker Credit'),
        ('cash', 'Cash / Counter'),
    ]

    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='payment_methods',
    )
    method_type = models.CharField(max_length=20, choices=METHOD_CHOICES)
    is_default = models.BooleanField(default=False)

    # Card details (masked)
    brand = models.CharField(max_length=20, blank=True)   # Visa, Mastercard
    last4 = models.CharField(max_length=4, blank=True)
    expiry_month = models.IntegerField(null=True, blank=True)
    expiry_year = models.IntegerField(null=True, blank=True)

    # Gateway reference (Stripe customer/payment-method ID)
    gateway_token = models.CharField(max_length=100, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'payment_methods'
        ordering = ['-is_default', '-created_at']

    def __str__(self):
        if self.method_type == 'card':
            return f"{self.brand} •••• {self.last4}"
        return self.get_method_type_display()


class Transaction(models.Model):
    """A single financial transaction."""
    TYPE_CHOICES = [
        ('subscription', 'Subscription'),
        ('coaching', 'Coaching'),
        ('retail', 'Retail'),
        ('recovery', 'Recovery'),
        ('refund', 'Refund'),
        ('topup', 'Locker Top-Up'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('settled', 'Settled'),
        ('refunded', 'Refunded'),
        ('failed', 'Failed'),
    ]

    # Who
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='transactions',
    )

    # ID
    transaction_id = models.CharField(max_length=30, unique=True)  # #TXN-99410

    # Description
    description = models.CharField(max_length=200)
    transaction_type = models.CharField(max_length=20, choices=TYPE_CHOICES)

    # Amount
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Payment method
    payment_method = models.ForeignKey(
        PaymentMethod,
        on_delete=models.SET_NULL,
        null=True, blank=True,
        related_name='transactions',
    )
    payment_method_label = models.CharField(max_length=60, blank=True)  # "Visa •••• 4242"

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Gateway
    gateway_reference = models.CharField(max_length=100, blank=True)  # Stripe charge ID

    # Metadata
    created_at = models.DateTimeField(auto_now_add=True)
    settled_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'transactions'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.transaction_id} — SAR {self.amount} ({self.status})"


class LockerCredit(models.Model):
    """Prepaid balance for shop/cafe/recovery purchases."""
    member = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='locker_credit',
    )
    balance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    max_cap = models.DecimalField(max_digits=10, decimal_places=2, default=100)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'locker_credits'

    def __str__(self):
        return f"{self.member.email} — SAR {self.balance}"


class Invoice(models.Model):
    """Formal invoice record (PDF available on request)."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('overdue', 'Overdue'),
        ('cancelled', 'Cancelled'),
    ]

    invoice_number = models.CharField(max_length=30, unique=True)  # #INV-9921
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='invoices',
    )

    # References
    transaction = models.ForeignKey(
        Transaction,
        on_delete=models.SET_NULL,
        null=True, blank=True,
    )

    # Details
    description = models.CharField(max_length=200)
    amount = models.DecimalField(max_digits=10, decimal_places=2)
    tax_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2)

    # Status
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')

    # Dates
    issued_at = models.DateTimeField(auto_now_add=True)
    due_at = models.DateField(null=True, blank=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'invoices'
        ordering = ['-issued_at']

    def __str__(self):
        return f"{self.invoice_number} — SAR {self.total} ({self.status})"

class PaymentIntent(models.Model):
    """
    One attempt by a member to pay for something (locker top-up, shop order,
    trainer session, course, membership renewal). The amount is decided by the
    server; the browser pays it through the Moyasar form (or with locker
    credit), and the purchase is fulfilled only after the payment is verified
    with Moyasar — see apps.payments.gateway.
    """
    PURPOSE_CHOICES = [
        ('locker_topup', 'Locker credit top-up'),
        ('shop_order', 'Shop order'),
        ('trainer_session', 'Trainer session'),
        ('course', 'Course'),
        ('renewal', 'Membership renewal'),
        ('facility_addon', 'Facility add-on'),
        ('nutrition_plan', 'Nutrition plan'),
        ('video', 'Machine video'),
        ('class_pass', 'Class'),
    ]
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('failed', 'Failed'),
        ('cancelled', 'Cancelled'),
        ('review', 'Needs manual review'),   # money taken but something didn't match
        ('refunded', 'Refunded'),
    ]
    METHOD_CHOICES = [
        ('moyasar', 'Card / STC Pay (Moyasar)'),
        ('locker_credit', 'Locker credit'),
    ]

    member = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='payment_intents')
    purpose = models.CharField(max_length=20, choices=PURPOSE_CHOICES)
    method = models.CharField(max_length=20, choices=METHOD_CHOICES, default='moyasar')
    amount = models.DecimalField(max_digits=10, decimal_places=2)          # SAR
    description = models.CharField(max_length=200)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending', db_index=True)

    # What is being paid for (one of these, depending on purpose)
    order = models.ForeignKey('shop.Order', on_delete=models.SET_NULL, null=True, blank=True, related_name='payment_intents')
    trainer_session = models.ForeignKey('catalog.TrainerSession', on_delete=models.SET_NULL, null=True, blank=True,
                                        related_name='payment_intents')
    course = models.ForeignKey('catalog.Course', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    plan = models.ForeignKey('members.MembershipPlan', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    months = models.PositiveIntegerField(null=True, blank=True)            # renewal / add-on length
    facility_item = models.ForeignKey('site_content.SiteItem', on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name='+')
    nutrition_plan = models.ForeignKey('nutrition.NutritionPlan', on_delete=models.SET_NULL, null=True, blank=True,
                                       related_name='payment_intents')
    video = models.ForeignKey('workouts.ExerciseVideo', on_delete=models.SET_NULL, null=True, blank=True,
                              related_name='+')
    gym_class = models.ForeignKey('classes.GymClass', on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='+')
    pass_kind = models.CharField(max_length=10, blank=True, db_default='')     # class_pass: full | session
    # A company the member is buying for → a standard (B2B) tax invoice, cleared by ZATCA.
    business_customer = models.ForeignKey('zatca.BusinessCustomer', on_delete=models.SET_NULL, null=True,
                                          blank=True, related_name='+')
    session_date = models.DateField(null=True, blank=True)                    # class_pass: kind=session

    # Moyasar
    attempt_token = models.CharField(max_length=64)                        # echoed in the payment metadata
    moyasar_payment_id = models.CharField(max_length=64, unique=True, null=True, blank=True)
    payment_raw = models.JSONField(default=dict, blank=True)               # redacted provider payload
    failure_reason = models.CharField(max_length=255, blank=True)

    transaction = models.ForeignKey(Transaction, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    created_at = models.DateTimeField(auto_now_add=True)
    paid_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = 'payment_intents'
        ordering = ['-created_at']

    def __str__(self):
        return f"Intent #{self.id} {self.purpose} SAR {self.amount} ({self.status})"

    @property
    def amount_halalas(self):
        from decimal import ROUND_HALF_UP
        return int((Decimal(self.amount) * 100).to_integral_value(ROUND_HALF_UP))
