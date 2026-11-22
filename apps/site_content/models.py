"""
Public website content managed from the admin portal.

Every text and image on the public site is a keyed slot, pre-filled (seeded)
from what the site shipped with. Admins edit en/ar text and upload images;
the shipped values are kept in default_* so any slot can be reset.
"""
from django.conf import settings
from django.db import models

from apps.core.sides import SideManager, side_field

PAGES = {
    'meta': 'Site (browser title)',
    'nav': 'Navigation',
    'boot': 'Loading screen',
    'foot': 'Footer',
    'tab': 'Mobile tab bar',
    'home': 'Home',
    'fac': 'Facilities',
    'svc': 'Services',
    'ins': 'Insights (blog)',
    'ct': 'Contact',
    'en': 'Membership signup',
    'cam': 'Signup camera',
    'pay': 'Payment result',
    'art': 'Article page',
    'nf': 'Page not found (404)',
}


class SiteText(models.Model):
    key = models.CharField(max_length=120)          # e.g. "home.hero.title"
    page = models.CharField(max_length=20, db_index=True)        # key prefix, see PAGES
    en = models.TextField(blank=True)
    ar = models.TextField(blank=True)
    default_en = models.TextField(blank=True)                    # shipped text (for reset)
    default_ar = models.TextField(blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['side', 'key'], name='uniq_sitetext_key_per_side')]
        db_table = 'site_texts'
        ordering = ['key']

    def __str__(self):
        return self.key

    @property
    def is_customized(self):
        return self.en != self.default_en or self.ar != self.default_ar


class SiteImage(models.Model):
    key = models.CharField(max_length=120)          # e.g. "fac.pool"
    page = models.CharField(max_length=20, db_index=True)
    label = models.CharField(max_length=160, blank=True)         # human description for admins
    image = models.ImageField(upload_to='site/', blank=True, null=True)   # uploaded (Cloudinary)
    default_url = models.URLField(max_length=1000, blank=True)   # shipped image
    alt_en = models.CharField(max_length=200, blank=True)
    alt_ar = models.CharField(max_length=200, blank=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        constraints = [models.UniqueConstraint(fields=['side', 'key'], name='uniq_siteimage_key_per_side')]
        db_table = 'site_images'
        ordering = ['key']

    def __str__(self):
        return self.key

    @property
    def url(self):
        return self.image.url if self.image else self.default_url

    @property
    def is_customized(self):
        return bool(self.image)


class SiteItem(models.Model):
    """One entry of an admin-managed list (see apps.site_content.collections)."""
    collection = models.CharField(max_length=40, db_index=True)   # "facilities", "articles", …
    slug = models.SlugField(max_length=100, blank=True)            # collections with detail pages
    order = models.PositiveIntegerField(default=0)
    is_active = models.BooleanField(default=True)                  # hidden from the site when False
    data = models.JSONField(default=dict)                          # field values, i18n as {"en","ar"}
    image = models.ImageField(upload_to='site/items/', blank=True, null=True)
    default_image_url = models.URLField(max_length=1000, blank=True)   # seeded image
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name='+',
    )

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'site_items'
        ordering = ['collection', 'order', 'id']
        constraints = [
            models.UniqueConstraint(
                fields=['side', 'collection', 'slug'], condition=~models.Q(slug=''), name='uniq_item_slug_per_side_collection',
            ),
        ]

    def __str__(self):
        title = (self.data.get('title') or {}).get('en', '')
        return f'{self.collection}: {title or self.pk}'

    @property
    def image_url(self):
        return self.image.url if self.image else self.default_image_url


class ContactMessage(models.Model):
    STATUS_NEW, STATUS_READ, STATUS_ARCHIVED = 'new', 'read', 'archived'
    STATUS = [(STATUS_NEW, 'New'), (STATUS_READ, 'Read'), (STATUS_ARCHIVED, 'Archived')]

    name = models.CharField(max_length=120)
    email = models.EmailField()
    phone = models.CharField(max_length=30, blank=True)
    topic = models.CharField(max_length=60, blank=True)
    subtopic = models.CharField(max_length=80, blank=True)
    message = models.TextField(max_length=5000)
    lang = models.CharField(max_length=5, blank=True)            # site language when sent
    status = models.CharField(max_length=10, choices=STATUS, default=STATUS_NEW, db_index=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)

    side = side_field()            # the male or female gym (apps.core.sides)
    objects = SideManager()

    class Meta:
        db_table = 'contact_messages'
        ordering = ['-created_at']

    def __str__(self):
        return f'{self.name} <{self.email}> — {self.topic or "general"}'
