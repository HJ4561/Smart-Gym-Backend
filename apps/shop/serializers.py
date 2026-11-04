from rest_framework import serializers
from .models import (
    ProductCategory, Product,
    Cart, CartItem,
    Order, OrderItem,
    PromoCode,
)


# ============================================
# CATEGORIES
# ============================================
class ProductCategorySerializer(serializers.ModelSerializer):
    product_count = serializers.SerializerMethodField()

    class Meta:
        model = ProductCategory
        fields = ['id', 'key', 'name', 'name_ar', 'icon', 'description', 'display_order', 'product_count']

    def get_product_count(self, obj):
        return obj.products.filter(is_active=True).count()


# ============================================
# PRODUCTS
# ============================================
class ProductListSerializer(serializers.ModelSerializer):
    category = ProductCategorySerializer(read_only=True)

    class Meta:
        model = Product
        fields = [
            'id', 'name', 'name_ar', 'sku', 'category',
            'description', 'description_ar', 'image_url',
            'price', 'original_price', 'price_note',
            'badge_top', 'badge_top_accent', 'badge_secondary', 'telemetry',
            'stock_quantity', 'stock_label', 'is_low_stock', 'is_out_of_stock',
            'rating', 'total_reviews', 'total_sold',
            'is_featured',
        ]


class ProductDetailSerializer(ProductListSerializer):
    class Meta(ProductListSerializer.Meta):
        fields = ProductListSerializer.Meta.fields + ['created_at', 'updated_at']


# ============================================
# CART
# ============================================
class CartItemSerializer(serializers.ModelSerializer):
    product = ProductListSerializer(read_only=True)
    product_id = serializers.IntegerField(write_only=True)
    line_total = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)

    class Meta:
        model = CartItem
        fields = ['id', 'product', 'product_id', 'quantity', 'line_total', 'added_at']
        read_only_fields = ['id', 'added_at']


class CartSerializer(serializers.ModelSerializer):
    items = CartItemSerializer(many=True, read_only=True)
    subtotal = serializers.DecimalField(max_digits=10, decimal_places=2, read_only=True)
    item_count = serializers.IntegerField(read_only=True)

    class Meta:
        model = Cart
        fields = ['id', 'items', 'subtotal', 'item_count', 'updated_at']


# ============================================
# ORDERS
# ============================================
class OrderItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = OrderItem
        fields = ['id', 'product_name', 'product_sku', 'unit_price', 'quantity', 'line_total']


class OrderListSerializer(serializers.ModelSerializer):
    item_count = serializers.SerializerMethodField()

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'status', 'delivery_method',
            'subtotal', 'discount', 'tax', 'total',
            'locker_number', 'pickup_facility',
            'item_count', 'created_at', 'ready_at', 'completed_at',
        ]

    def get_item_count(self, obj):
        return obj.items.count()


class OrderDetailSerializer(serializers.ModelSerializer):
    items = OrderItemSerializer(many=True, read_only=True)

    class Meta:
        model = Order
        fields = [
            'id', 'order_number', 'status', 'delivery_method',
            'subtotal', 'discount', 'tax', 'total', 'promo_code',
            'locker_number', 'pickup_facility',
            'items', 'notes',
            'created_at', 'ready_at', 'completed_at',
        ]


# ============================================
# PROMO
# ============================================
# apps/shop/serializers.py — replace PromoCodeSerializer
class PromoCodeSerializer(serializers.ModelSerializer):
    is_valid = serializers.BooleanField(read_only=True)

    class Meta:
        model = PromoCode
        fields = [
            'id', 'code', 'description', 'discount_percent',
            'is_active', 'is_valid', 'valid_until', 'used_count',
        ]