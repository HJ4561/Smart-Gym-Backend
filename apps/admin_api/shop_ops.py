"""
Admin → Shop: products (price, stock, photo, category, on sale or not), categories and promo
codes. Members see active products in active categories; checkout takes stock off.
"""
import re
from decimal import Decimal, InvalidOperation

from django.db.models import ProtectedError, Q, Sum
from django.shortcuts import get_object_or_404
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from django.utils.text import slugify
from rest_framework.decorators import api_view, parser_classes, permission_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.response import Response

from apps.core.i18n import read_ar, with_ar
from apps.core.validators import duplicate_error
from apps.shop.models import Order, OrderItem, Product, ProductCategory, PromoCode

from .catalog_ops import _save_image
from .permissions import IsAdminUser


def _f(v):
    return round(float(v or 0), 2) if v is not None else None


def _money(data, field, errors, required=False, allow_null=False):
    v = data.get(field)
    if v in (None, ''):
        if required:
            errors[field] = 'Required.'
        return None
    try:
        d = Decimal(str(v)).quantize(Decimal('0.01'))
        if d < 0 or d > Decimal('1000000'):
            raise InvalidOperation
        return d
    except (InvalidOperation, ValueError):
        errors[field] = 'Enter an amount in SAR.'
        return None


def _int(data, field, errors, lo, hi):
    try:
        n = int(data.get(field))
        if not lo <= n <= hi:
            raise ValueError
        return n
    except (TypeError, ValueError):
        errors[field] = f'Enter a whole number from {lo} to {hi}.'
        return None


# ============================================================
# PRODUCTS
# ============================================================
def _product_row(p):
    return {
        'id': p.id, 'name': p.name, 'sku': p.sku, 'description': p.description, **with_ar(p, 'name', 'description'),
        'category_id': p.category_id, 'category': p.category.name, 'category_ar': p.category.name_ar,
        'image_url': p.image_url, 'price': _f(p.price), 'original_price': _f(p.original_price),
        'price_note': p.price_note, 'badge_top': p.badge_top, 'badge_secondary': p.badge_secondary,
        'telemetry': p.telemetry or [], 'stock_quantity': p.stock_quantity, 'stock_label': p.stock_label,
        'low_stock_threshold': p.low_stock_threshold, 'is_low_stock': p.is_low_stock,
        'is_active': p.is_active, 'is_featured': p.is_featured, 'total_sold': p.total_sold,
        'rating': _f(p.rating), 'total_reviews': p.total_reviews, 'updated_at': p.updated_at,
    }


# Fixed format: the category's 3-letter prefix, a dash and 4 digits — PRO-0001, GEA-0012.
SKU_RE = re.compile(r'[A-Z]{3}-\d{4}')


def sku_prefix(category):
    """The category's 3 letters (from its key, else its name): protein → PRO, pre-workout → PRE."""
    return (re.sub(r'[^A-Za-z]', '', getattr(category, 'key', '') or getattr(category, 'name', '')).upper() + 'XXX')[:3]


def next_sku(category, exclude_id=None):
    """First free SKU for a category: its prefix + a running number (SUP-0001)."""
    prefix = sku_prefix(category)
    taken = Product.objects.filter(sku__startswith=f'{prefix}-')
    if exclude_id:
        taken = taken.exclude(id=exclude_id)
    taken = set(taken.values_list('sku', flat=True))
    n = 1
    while f'{prefix}-{n:04d}' in taken:
        n += 1
    return f'{prefix}-{n:04d}'


def _apply_sku(p, data, errors, category_changed):
    """The number part can be chosen; the prefix always follows the category."""
    if p.category_id is None:
        return
    prefix = sku_prefix(p.category)
    raw = str(data.get('sku') or '').strip().upper() if 'sku' in data else ''
    if not raw:
        if not p.id or category_changed or not SKU_RE.fullmatch(p.sku or '') or not p.sku.startswith(prefix + '-'):
            p.sku = next_sku(p.category, exclude_id=p.id)
        return
    if raw.isdigit() and len(raw) <= 4:
        raw = f'{prefix}-{int(raw):04d}'
    if not SKU_RE.fullmatch(raw) or not raw.startswith(prefix + '-'):
        errors['sku'] = (f'SKU for {p.category.name} products is {prefix}- and 4 digits, e.g. {prefix}-0007 '
                         '(or type just the number).')
    elif Product.objects.filter(sku__iexact=raw).exclude(id=p.id).exists():
        errors['sku'] = f'SKU {raw} is already used by another product. Next free: {next_sku(p.category, p.id)}.'
    p.sku = raw


def _apply_product(p, data, errors):
    for f, n, req in (('name', 200, True), ('description', 4000, False), ('price_note', 100, False),
                      ('badge_top', 60, False), ('badge_secondary', 60, False), ('stock_label', 60, False)):
        if f in data:
            v = str(data.get(f) or '').strip()
            if f == 'name':
                v = ' '.join(v.split())
            if req and not v:
                errors[f] = 'Required.'
            elif len(v) > n:
                errors[f] = f'Keep it under {n} characters.'
            elif f == 'name' and len(v) < 3:
                errors[f] = 'Product name must be at least 3 characters.'
            setattr(p, f, v)
    read_ar(p, data, {'name': 200, 'description': 4000}, errors)
    if 'name' in data and 'name' not in errors:
        dup = duplicate_error(Product.objects.all(), 'name', p.name, 'A product named', exclude_id=p.id)
        if dup:
            errors['name'] = dup
    category_changed = False
    if 'category_id' in data:
        c = ProductCategory.objects.filter(id=data.get('category_id')).first()
        if c is None:
            errors['category_id'] = 'Choose a category.'
        else:
            category_changed = bool(p.id) and c.id != p.category_id
            p.category = c
    if 'sku' in data or category_changed or not p.id:
        _apply_sku(p, data, errors, category_changed)
    if 'price' in data:
        d = _money(data, 'price', errors, required=True)
        if d is not None:
            p.price = d
    if 'original_price' in data:
        p.original_price = _money(data, 'original_price', errors)
        if p.original_price is not None and p.price is not None and p.original_price <= p.price:
            errors['original_price'] = 'The “was” price must be higher than the price.'
    for f, lo, hi in (('stock_quantity', 0, 100000), ('low_stock_threshold', 0, 10000)):
        if f in data:
            n = _int(data, f, errors, lo, hi)
            if n is not None:
                setattr(p, f, n)
    if 'telemetry' in data:
        raw = data.get('telemetry') or []
        if isinstance(raw, str):
            raw = raw.splitlines()
        p.telemetry = [str(x).strip()[:60] for x in raw if str(x).strip()][:6]
    for flag in ('is_active', 'is_featured'):
        if flag in data:
            setattr(p, flag, bool(data.get(flag)))


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
@parser_classes([JSONParser])
def products(request):
    """GET /api/admin/shop/products/?q=&category=&status=active|hidden|low|out · POST {name, sku, category_id, price, …}"""
    if request.method == 'GET':
        qs = Product.objects.select_related('category').order_by('-is_active', 'category__display_order', 'name')
        q = (request.query_params.get('q') or '').strip()
        if q:
            qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q))
        if request.query_params.get('category'):
            qs = qs.filter(category_id=request.query_params['category'])
        st = request.query_params.get('status')
        if st == 'active':
            qs = qs.filter(is_active=True)
        elif st == 'hidden':
            qs = qs.filter(is_active=False)
        elif st == 'out':
            qs = qs.filter(stock_quantity__lte=0)
        rows = [_product_row(p) for p in qs]
        if st == 'low':
            rows = [r for r in rows if r['is_low_stock'] and r['stock_quantity'] > 0]
        return Response(rows)
    p = Product(stock_quantity=0)
    errors = {}
    data = {'name': '', 'price': None, 'category_id': None, **request.data}
    _apply_product(p, data, errors)
    if errors:
        return Response(errors, status=400)
    p.save()
    return Response(_product_row(p), status=201)


@api_view(['GET'])
@permission_classes([IsAdminUser])
def product_next_sku(request):
    """GET /api/admin/shop/products/next-sku/?category_id=&product_id= → {prefix, sku} for the product form."""
    c = get_object_or_404(ProductCategory, id=request.query_params.get('category_id'))
    pid = request.query_params.get('product_id')
    return Response({'prefix': sku_prefix(c), 'sku': next_sku(c, exclude_id=int(pid) if str(pid).isdigit() else None)})


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
@parser_classes([JSONParser])
def product_detail(request, product_id):
    """PATCH any field · DELETE only if it was never ordered (hide it otherwise)."""
    p = get_object_or_404(Product.objects.select_related('category'), id=product_id)
    if request.method == 'DELETE':
        if OrderItem.objects.filter(product=p).exists():
            return Response({'detail': 'This product has been ordered before — hide it instead.'}, status=400)
        p.delete()
        return Response(status=204)
    errors = {}
    _apply_product(p, request.data, errors)
    if errors:
        return Response(errors, status=400)
    p.save()
    return Response(_product_row(p))


@api_view(['POST'])
@permission_classes([IsAdminUser])
@parser_classes([MultiPartParser, FormParser])
def product_image(request, product_id):
    """POST /api/admin/shop/products/<id>/image/ multipart {image}"""
    p = get_object_or_404(Product.objects.select_related('category'), id=product_id)
    url, err = _save_image(request, 'shop')
    if err:
        return Response({'image': [err]}, status=400)
    p.image_url = url
    p.save(update_fields=['image_url', 'updated_at'])
    return Response(_product_row(p))


@api_view(['GET'])
@permission_classes([IsAdminUser])
def shop_stats(request):
    from apps.shop.services import expire_stale_orders
    expire_stale_orders()
    since = timezone.now() - timezone.timedelta(days=30)
    active = Product.objects.filter(is_active=True)
    paid = Order.objects.filter(created_at__gte=since).exclude(status__in=['pending', 'cancelled', 'refunded'])
    return Response({
        'products': active.count(),
        'hidden': Product.objects.filter(is_active=False).count(),
        'out_of_stock': active.filter(stock_quantity__lte=0).count(),
        'low_stock': sum(1 for p in active.filter(stock_quantity__gt=0) if p.is_low_stock),
        'orders_30d': paid.count(),
        'sales_30d': _f(paid.aggregate(t=Sum('total'))['t'] or 0),
    })


# ============================================================
# CATEGORIES
# ============================================================
def _cat_row(c):
    return {'id': c.id, 'key': c.key, 'name': c.name, 'name_ar': c.name_ar, 'icon': c.icon, 'description': c.description,
            'display_order': c.display_order, 'is_active': c.is_active, 'products': c.products.count()}


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def categories(request):
    if request.method == 'GET':
        return Response([_cat_row(c) for c in ProductCategory.objects.order_by('display_order', 'name')])
    name = ' '.join(str(request.data.get('name') or '').split())
    if not name:
        return Response({'name': 'Category name is required.'}, status=400)
    if len(name) > 60:
        return Response({'name': 'Category name must be 60 characters or fewer.'}, status=400)
    dup = duplicate_error(ProductCategory.objects.all(), 'name', name, 'A category named')
    if dup:
        return Response({'name': dup}, status=400)
    key = slugify(name)[:40] or 'category'
    base, n = key, 1
    while ProductCategory.objects.filter(key=key).exists():
        n += 1
        key = f'{base}-{n}'
    c = ProductCategory.objects.create(
        key=key, name=name, name_ar=' '.join(str(request.data.get('name_ar') or '').split())[:60],
        icon=str(request.data.get('icon') or 'category')[:40],
        description=str(request.data.get('description') or '')[:200],
        display_order=ProductCategory.objects.count())
    return Response(_cat_row(c), status=201)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def category_detail(request, category_id):
    c = get_object_or_404(ProductCategory, id=category_id)
    if request.method == 'DELETE':
        try:
            c.delete()
        except ProtectedError:
            return Response({'detail': 'Move or delete its products first, or hide the category.'}, status=400)
        return Response(status=204)
    for f, n in (('name', 60), ('name_ar', 60), ('icon', 40), ('description', 200)):
        if f in request.data:
            setattr(c, f, ' '.join(str(request.data.get(f) or '').split())[:n])
    if not c.name:
        return Response({'name': 'Category name is required.'}, status=400)
    dup = duplicate_error(ProductCategory.objects.all(), 'name', c.name, 'A category named', exclude_id=c.id)
    if dup:
        return Response({'name': dup}, status=400)
    if 'display_order' in request.data:
        try:
            c.display_order = int(request.data['display_order'])
        except (TypeError, ValueError):
            return Response({'display_order': 'Enter a number.'}, status=400)
    if 'is_active' in request.data:
        c.is_active = bool(request.data['is_active'])
    c.save()
    return Response(_cat_row(c))


# ============================================================
# PROMO CODES
# ============================================================
def _promo_row(p):
    return {'id': p.id, 'code': p.code, 'description': p.description, 'discount_percent': _f(p.discount_percent),
            'max_uses': p.max_uses, 'used_count': p.used_count, 'valid_from': p.valid_from,
            'valid_until': p.valid_until, 'is_active': p.is_active, 'is_valid': p.is_valid}


def _apply_promo(p, data, errors):
    if 'code' in data:
        code = re.sub(r'[^A-Z0-9]', '', str(data.get('code') or '').upper())[:40]
        if len(code) < 3:
            errors['code'] = 'At least 3 letters or numbers.'
        elif PromoCode.objects.filter(code=code).exclude(id=p.id).exists():
            errors['code'] = 'This code already exists.'
        p.code = code
    if 'description' in data:
        p.description = str(data.get('description') or '')[:200]
    if 'discount_percent' in data:
        try:
            d = Decimal(str(data.get('discount_percent')))
            if not Decimal('1') <= d <= Decimal('90'):
                raise InvalidOperation
            p.discount_percent = d
        except (InvalidOperation, ValueError):
            errors['discount_percent'] = 'Enter 1 to 90.'
    if 'max_uses' in data:
        v = data.get('max_uses')
        if v in (None, ''):
            p.max_uses = None
        else:
            n = _int(data, 'max_uses', errors, 1, 1000000)
            p.max_uses = n
    for f in ('valid_from', 'valid_until'):
        if f in data:
            v = data.get(f)
            if v in (None, ''):
                setattr(p, f, None)
            else:
                dt = parse_datetime(str(v)) or (parse_datetime(f'{v}T00:00:00') if len(str(v)) == 10 else None)
                if dt is None:
                    errors[f] = 'Use a date.'
                else:
                    if timezone.is_naive(dt):
                        dt = timezone.make_aware(dt)
                    if f == 'valid_until' and len(str(v)) == 10:
                        dt = dt.replace(hour=23, minute=59, second=59)
                    setattr(p, f, dt)
    if p.valid_from and p.valid_until and p.valid_until < p.valid_from:
        errors['valid_until'] = 'Ends before it starts.'
    if 'is_active' in data:
        p.is_active = bool(data.get('is_active'))


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def promos(request):
    if request.method == 'GET':
        return Response([_promo_row(p) for p in PromoCode.objects.order_by('-is_active', 'code')])
    p = PromoCode()
    errors = {}
    _apply_promo(p, {'code': '', 'discount_percent': None, **request.data}, errors)
    if errors:
        return Response(errors, status=400)
    p.save()
    return Response(_promo_row(p), status=201)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def promo_detail(request, promo_id):
    p = get_object_or_404(PromoCode, id=promo_id)
    if request.method == 'DELETE':
        if p.used_count:
            return Response({'detail': 'This code has been used — switch it off instead.'}, status=400)
        p.delete()
        return Response(status=204)
    errors = {}
    _apply_promo(p, request.data, errors)
    if errors:
        return Response(errors, status=400)
    p.save()
    return Response(_promo_row(p))
