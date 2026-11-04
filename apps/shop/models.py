from django.db import models
from django.conf import settings


# ============================================
# PRODUCTS
# ============================================
class ProductCategory(models.Model):
    """Categories: Protein, Pre-Workout, Recovery, Gear, Apparel"""
    key = models.SlugField(max_length=40, unique=True)
    name = models.CharField(max_length=60)
    name_ar = models.CharField(max_length=60, blank=True, db_default='')   # Arabic; empty = show the English
    icon = models.CharField(max_length=40, blank=True)
    description = models.CharField(max_length=200, blank=True)
    description_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    display_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'product_categories'
        ordering = ['display_order', 'name']

    def __str__(self):
        return self.name


class Product(models.Model):
    """A product available in the gym shop."""
    category = models.ForeignKey(
        ProductCategory,
        on_delete=models.PROTECT,
        related_name='products',
    )

    name = models.CharField(max_length=200)
    name_ar = models.CharField(max_length=200, blank=True, db_default='')   # Arabic; empty = show the English
    sku = models.CharField(max_length=50, unique=True)
    description = models.TextField(blank=True)
    description_ar = models.TextField(blank=True, db_default='')   # Arabic; empty = show the English

    # Media
    image_url = models.URLField(blank=True)

    # Pricing
    price = models.DecimalField(max_digits=10, decimal_places=2)
    original_price = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    price_note = models.CharField(max_length=100, blank=True)  # "Pro Athletic Tier Price"
    price_note_ar = models.CharField(max_length=100, blank=True, db_default='')   # Arabic; empty = show the English

    # Badges
    badge_top = models.CharField(max_length=60, blank=True)     # "Pro Perk 15% Off"
    badge_top_ar = models.CharField(max_length=60, blank=True, db_default='')   # Arabic; empty = show the English
    badge_top_accent = models.BooleanField(default=False)
    badge_secondary = models.CharField(max_length=60, blank=True)  # "Cold-Filtered"
    badge_secondary_ar = models.CharField(max_length=60, blank=True, db_default='')   # Arabic; empty = show the English
    telemetry = models.JSONField(default=list, blank=True)  # ["28G PROTEIN / SRV", "6.8G BCAA"]
    telemetry_ar = models.JSONField(default=list, blank=True, null=True)   # Arabic; empty = show the English

    # Stock
    stock_quantity = models.IntegerField(default=0)
    stock_label = models.CharField(max_length=60, blank=True)  # "In Stock (14 Tubs)"
    stock_label_ar = models.CharField(max_length=60, blank=True, db_default='')   # Arabic; empty = show the English
    low_stock_threshold = models.IntegerField(default=5)

    # Metrics
    rating = models.DecimalField(max_digits=3, decimal_places=2, default=0)
    total_reviews = models.IntegerField(default=0)
    total_sold = models.IntegerField(default=0)

    # Flags
    is_active = models.BooleanField(default=True)
    is_featured = models.BooleanField(default=False)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'products'
        ordering = ['-is_featured', 'name']

    def __str__(self):
        return f"{self.name} (SAR {self.price})"

    @property
    def is_low_stock(self):
        return self.stock_quantity <= self.low_stock_threshold

    @property
    def is_out_of_stock(self):
        return self.stock_quantity <= 0


# ============================================
# CART
# ============================================
class Cart(models.Model):
    """A member's current shopping cart (one per member)."""
    member = models.OneToOneField(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='cart',
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = 'carts'

    def __str__(self):
        return f"Cart — {self.member.email}"

    @property
    def subtotal(self):
        return sum(item.line_total for item in self.items.all())

    @property
    def item_count(self):
        return self.items.count()


class CartItem(models.Model):
    cart = models.ForeignKey(
        Cart,
        on_delete=models.CASCADE,
        related_name='items',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.CASCADE,
    )
    quantity = models.IntegerField(default=1)
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'cart_items'
        unique_together = ['cart', 'product']

    def __str__(self):
        return f"{self.product.name} × {self.quantity}"

    @property
    def line_total(self):
        return self.product.price * self.quantity


# ============================================
# ORDERS
# ============================================
class Order(models.Model):
    """A completed order."""
    STATUS_CHOICES = [
        ('pending', 'Pending'),
        ('paid', 'Paid'),
        ('preparing', 'Preparing'),
        ('ready', 'Ready for Pickup'),
        ('dispatched', 'Dispatched to Locker'),
        ('completed', 'Completed'),
        ('cancelled', 'Cancelled'),
        ('refunded', 'Refunded'),
    ]
    DELIVERY_CHOICES = [
        ('locker', 'Locker Dispatch'),
        ('counter', 'Counter Pickup'),
        ('delivery', 'Home Delivery'),
    ]

    order_number = models.CharField(max_length=30, unique=True)  # #ORD-8472
    member = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.PROTECT,
        related_name='orders',
    )

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    delivery_method = models.CharField(max_length=20, choices=DELIVERY_CHOICES, default='locker')

    # Delivery details
    locker_number = models.CharField(max_length=20, blank=True)
    pickup_facility = models.CharField(max_length=100, blank=True, default='Metro Station Core 01')

    # Totals
    subtotal = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    discount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    total = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Promo
    promo_code = models.CharField(max_length=40, blank=True)

    # Timing
    created_at = models.DateTimeField(auto_now_add=True)
    ready_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True)

    class Meta:
        db_table = 'orders'
        ordering = ['-created_at']

    def __str__(self):
        return f"{self.order_number} — {self.member.email} ({self.status})"


class OrderItem(models.Model):
    """A product line inside an order (snapshot at purchase time)."""
    order = models.ForeignKey(
        Order,
        on_delete=models.CASCADE,
        related_name='items',
    )
    product = models.ForeignKey(
        Product,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
    )

    # Snapshot (so we keep the historical values)
    product_name = models.CharField(max_length=200)
    product_sku = models.CharField(max_length=50)
    unit_price = models.DecimalField(max_digits=10, decimal_places=2)
    quantity = models.IntegerField(default=1)
    line_total = models.DecimalField(max_digits=10, decimal_places=2)

    class Meta:
        db_table = 'order_items'
        ordering = ['id']

    def __str__(self):
        return f"{self.product_name} × {self.quantity}"


# ============================================
# PROMO CODES
# ============================================
class PromoCode(models.Model):
    """Discount codes like PRO15."""
    code = models.CharField(max_length=40, unique=True)
    description = models.CharField(max_length=200, blank=True)

    # Discount
    discount_percent = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    max_uses = models.IntegerField(null=True, blank=True)
    used_count = models.IntegerField(default=0)

    # Validity
    valid_from = models.DateTimeField(null=True, blank=True)
    valid_until = models.DateTimeField(null=True, blank=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = 'promo_codes'

    def __str__(self):
        return f"{self.code} (-{self.discount_percent}%)"

    @property
    def is_valid(self):
        from django.utils import timezone
        now = timezone.now()
        if not self.is_active:
            return False
        if self.valid_from and now < self.valid_from:
            return False
        if self.valid_until and now > self.valid_until:
            return False
        if self.max_uses and self.used_count >= self.max_uses:
            return False
        return True