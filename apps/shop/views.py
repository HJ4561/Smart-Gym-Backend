from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.permissions import IsAuthenticated, AllowAny
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.db import transaction as db_transaction
from django.utils import timezone
from decimal import Decimal
import uuid
from django.db.models import F, Q
from django.conf import settings

from .services import expire_stale_orders, release_stock
from .models import (
    ProductCategory, Product,
    Cart, CartItem,
    Order, OrderItem,
    PromoCode,
)
from .serializers import (
    ProductCategorySerializer,
    ProductListSerializer, ProductDetailSerializer,
    CartSerializer, CartItemSerializer,
    OrderListSerializer, OrderDetailSerializer,
    PromoCodeSerializer,
)


VAT_RATE = Decimal(str(getattr(settings, 'SHOP_VAT_RATE', '0.15')))   # Saudi VAT 15%

# ============================================
# CATEGORIES (public)
# ============================================
@api_view(['GET'])
@permission_classes([AllowAny])
def categories_list(request):
    """
    GET /api/shop/categories/
    """
    qs = ProductCategory.objects.filter(is_active=True)
    return Response(ProductCategorySerializer(qs, many=True).data)


# ============================================
# PRODUCTS (public)
# ============================================
@api_view(['GET'])
@permission_classes([AllowAny])
def products_list(request):
    """
    GET /api/shop/products/
    Query params: ?category=protein&search=&featured=true
    Out-of-stock products are left out unless ?in_stock=false (the admin shop lists everything).
    """
    expire_stale_orders()
    qs = Product.objects.filter(is_active=True, category__is_active=True).select_related('category')

    category = request.query_params.get('category')
    if category:
        qs = qs.filter(category__key=category)

    if request.query_params.get('featured') == 'true':
        qs = qs.filter(is_featured=True)

    if request.query_params.get('in_stock') != 'false':
        qs = qs.filter(stock_quantity__gt=0)

    search = request.query_params.get('search')
    if search:
        qs = qs.filter(
            Q(name__icontains=search) | Q(description__icontains=search)
        )

    return Response(ProductListSerializer(qs, many=True).data)


@api_view(['GET'])
@permission_classes([AllowAny])
def product_detail(request, product_id):
    """
    GET /api/shop/products/<id>/
    """
    product = get_object_or_404(Product, id=product_id, is_active=True)
    return Response(ProductDetailSerializer(product).data)


# ============================================
# CART
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def get_cart(request):
    """
    GET /api/shop/cart/
    """
    cart, _ = Cart.objects.get_or_create(member=request.user)
    return Response(CartSerializer(cart).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def add_to_cart(request):
    """
    POST /api/shop/cart/add/
    Body: { "product_id": 5, "quantity": 1 }
    """
    product_id = request.data.get('product_id')
    quantity = int(request.data.get('quantity', 1))

    if quantity <= 0:
        return Response({'detail': 'Quantity must be positive.'}, status=status.HTTP_400_BAD_REQUEST)

    product = get_object_or_404(Product, id=product_id, is_active=True)

    if product.is_out_of_stock:
        return Response({'detail': 'Product is out of stock.'}, status=status.HTTP_400_BAD_REQUEST)

    if quantity > product.stock_quantity:
        return Response(
            {'detail': f'Only {product.stock_quantity} units available.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    cart, _ = Cart.objects.get_or_create(member=request.user)

    cart_item, created = CartItem.objects.get_or_create(
        cart=cart, product=product,
        defaults={'quantity': quantity},
    )
    if not created:
        cart_item.quantity += quantity
        cart_item.save()

    return Response(CartSerializer(cart).data, status=status.HTTP_201_CREATED)


@api_view(['PATCH'])
@permission_classes([IsAuthenticated])
def update_cart_item(request, item_id):
    """
    PATCH /api/shop/cart/items/<id>/
    Body: { "quantity": 3 }
    """
    try:
        item = CartItem.objects.get(id=item_id, cart__member=request.user)
    except CartItem.DoesNotExist:
        return Response({'detail': 'Cart item not found.'}, status=status.HTTP_404_NOT_FOUND)

    try:
        quantity = int(request.data.get('quantity', 1))
    except (ValueError, TypeError):
        return Response({'detail': 'Invalid quantity.'}, status=status.HTTP_400_BAD_REQUEST)

    if quantity <= 0:
        item.delete()
        return Response(CartSerializer(item.cart).data)

    if quantity > item.product.stock_quantity:
        return Response(
            {'detail': f'Only {item.product.stock_quantity} units available.'},
            status=status.HTTP_400_BAD_REQUEST,
        )

    item.quantity = quantity
    item.save()

    return Response(CartSerializer(item.cart).data)


@api_view(['DELETE'])
@permission_classes([IsAuthenticated])
def remove_cart_item(request, item_id):
    """
    DELETE /api/shop/cart/items/<id>/
    """
    try:
        item = CartItem.objects.get(id=item_id, cart__member=request.user)
    except CartItem.DoesNotExist:
        return Response({'detail': 'Cart item not found.'}, status=status.HTTP_404_NOT_FOUND)

    cart = item.cart
    item.delete()
    return Response(CartSerializer(cart).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def clear_cart(request):
    """
    POST /api/shop/cart/clear/
    """
    cart, _ = Cart.objects.get_or_create(member=request.user)
    cart.items.all().delete()
    return Response(CartSerializer(cart).data)


# ============================================
# PROMO CODES
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def apply_promo(request):
    """
    POST /api/shop/cart/apply-promo/
    Body: { "code": "PRO15" }
    """
    code = request.data.get('code', '').strip().upper()
    if not code:
        return Response({'detail': 'Code is required.'}, status=status.HTTP_400_BAD_REQUEST)

    try:
        promo = PromoCode.objects.get(code=code)
    except PromoCode.DoesNotExist:
        return Response({'detail': 'Invalid promo code.'}, status=status.HTTP_404_NOT_FOUND)

    if not promo.is_valid:
        return Response({'detail': 'Promo code is no longer valid.'}, status=status.HTTP_400_BAD_REQUEST)

    return Response({
        'code': promo.code,
        'discount_percent': float(promo.discount_percent),
        'description': promo.description,
    })


# ============================================
# CHECKOUT → ORDER
# ============================================
@api_view(['POST'])
@permission_classes([IsAuthenticated])
def checkout(request):
    """
    POST /api/shop/checkout/
    Body: { "delivery_method": "locker" | "counter", "promo_code": "PRO15", "notes": "..." }
    Turns the cart into an order awaiting payment (status 'pending') and reserves
    the stock. The order is paid through /api/payments/checkout/ (Moyasar or
    locker credit); cancelling an unpaid order releases the stock.
    """
    expire_stale_orders()   # frees stock held by abandoned checkouts
    delivery_method = request.data.get('delivery_method', 'locker')
    if delivery_method not in ('locker', 'counter'):
        return Response({'detail': 'Choose locker or counter pickup.'}, status=status.HTTP_400_BAD_REQUEST)
    promo_code_str = (request.data.get('promo_code') or '').strip().upper()
    notes = (request.data.get('notes') or '')[:500]

    with db_transaction.atomic():
        cart, _ = Cart.objects.get_or_create(member=request.user)
        items = list(cart.items.select_related('product'))
        if not items:
            return Response({'detail': 'Cart is empty.'}, status=status.HTTP_400_BAD_REQUEST)

        # Lock the products and check stock before taking anything.
        products = {p.id: p for p in Product.objects.select_for_update().filter(id__in=[i.product_id for i in items])}
        short = [f"{products[i.product_id].name} (only {products[i.product_id].stock_quantity} left)"
                 for i in items if products[i.product_id].stock_quantity < i.quantity]
        if short:
            return Response({'detail': 'Not enough stock: ' + ', '.join(short) + '.'}, status=status.HTTP_409_CONFLICT)

        subtotal = sum((products[i.product_id].price * i.quantity for i in items), Decimal('0.00'))

        discount = Decimal('0.00')
        promo = None
        if promo_code_str:
            promo = PromoCode.objects.select_for_update().filter(code=promo_code_str).first()
            if promo is None or not promo.is_valid:
                return Response({'detail': 'That promo code is not valid.'}, status=status.HTTP_400_BAD_REQUEST)
            discount = (subtotal * promo.discount_percent / Decimal('100')).quantize(Decimal('0.01'))

        taxable = subtotal - discount
        tax = (taxable * VAT_RATE).quantize(Decimal('0.01'))
        total = taxable + tax

        order = Order.objects.create(
            order_number=f"ORD-{uuid.uuid4().hex[:8].upper()}",
            member=request.user,
            status='pending',
            delivery_method=delivery_method,
            subtotal=subtotal, discount=discount, tax=tax, total=total,
            promo_code=promo.code if promo else '',
            notes=notes,
            locker_number=f"LKR-{uuid.uuid4().hex[:4].upper()}" if delivery_method == 'locker' else '',
        )
        for item in items:
            product = products[item.product_id]
            OrderItem.objects.create(
                order=order, product=product, product_name=product.name, product_sku=product.sku,
                unit_price=product.price, quantity=item.quantity, line_total=product.price * item.quantity,
            )
            product.stock_quantity -= item.quantity          # reserved until paid or cancelled
            product.total_sold += item.quantity
            product.save(update_fields=['stock_quantity', 'total_sold'])
        if promo:
            PromoCode.objects.filter(id=promo.id).update(used_count=F('used_count') + 1)
        cart.items.all().delete()

    return Response(OrderDetailSerializer(order).data, status=status.HTTP_201_CREATED)


# ============================================
# ORDERS
# ============================================
@api_view(['GET'])
@permission_classes([IsAuthenticated])
def my_orders(request):
    """
    GET /api/shop/orders/
    Query params: ?status=dispatched
    """
    qs = Order.objects.filter(member=request.user)

    status_filter = request.query_params.get('status')
    if status_filter:
        qs = qs.filter(status=status_filter)

    return Response(OrderListSerializer(qs[:50], many=True).data)


@api_view(['GET'])
@permission_classes([IsAuthenticated])
def order_detail(request, order_number):
    """
    GET /api/shop/orders/<order_number>/
    """
    try:
        order = Order.objects.get(order_number=order_number, member=request.user)
    except Order.DoesNotExist:
        return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)

    return Response(OrderDetailSerializer(order).data)


@api_view(['POST'])
@permission_classes([IsAuthenticated])
def cancel_order(request, order_number):
    """
    POST /api/shop/orders/<order_number>/cancel/
    Members can cancel orders that are not paid yet (releases the stock).
    Paid orders are refunded by staff — contact the front desk.
    """
    with db_transaction.atomic():
        order = Order.objects.select_for_update().filter(order_number=order_number, member=request.user).first()
        if order is None:
            return Response({'detail': 'Order not found.'}, status=status.HTTP_404_NOT_FOUND)
        if order.status != 'pending':
            return Response(
                {'detail': 'Paid orders can’t be cancelled here — please ask the front desk for a refund.'},
                status=status.HTTP_400_BAD_REQUEST,
            )
        release_stock(order)
        order.status = 'cancelled'
        order.save(update_fields=['status'])

    return Response(OrderDetailSerializer(order).data)