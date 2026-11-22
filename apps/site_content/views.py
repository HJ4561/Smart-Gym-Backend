import logging

from django.conf import settings
from django.core.mail import send_mail
from django.db import transaction
from django.db.models import Count, F, Max, Q
from django.http import Http404
from django.shortcuts import get_object_or_404
from django.utils.text import slugify
from rest_framework import status
from rest_framework.decorators import api_view, parser_classes, permission_classes, throttle_classes
from rest_framework.parsers import FormParser, JSONParser, MultiPartParser
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.throttling import SimpleRateThrottle

from apps.core.roles import DeskRead
from apps.admin_api.permissions import IsAdminUser
from apps.members.models import MembershipPlan

from .collections import COLLECTIONS, CollectionError, clean_data, make_slug, schema_for
from .models import PAGES, ContactMessage, SiteImage, SiteItem, SiteText
from .serializers import (
    AdminPlanSerializer, ContactCreateSerializer, ContactMessageSerializer, SiteImageSerializer,
    SiteImageUploadSerializer, SiteItemSerializer, SiteTextSerializer, public_item,
)

logger = logging.getLogger(__name__)
CONSOLE_EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'


# ============================================================
# PUBLIC
# ============================================================
@api_view(['GET'])
@permission_classes([AllowAny])
def public_site_content(request):
    """
    GET /api/public/site-content/
    Every text (en + ar) and image URL of the public site, keyed like the
    site's i18n keys. The site uses these over its built-in text.
    """
    texts = {key: {'en': en, 'ar': ar} for key, en, ar in SiteText.objects.values_list('key', 'en', 'ar')}
    images = {
        img.key: {'url': img.url, 'alt_en': img.alt_en, 'alt_ar': img.alt_ar}
        for img in SiteImage.objects.all()
    }
    updated = max(filter(None, [
        SiteText.objects.aggregate(m=Max('updated_at'))['m'],
        SiteImage.objects.aggregate(m=Max('updated_at'))['m'],
    ]), default=None)
    response = Response({'texts': texts, 'images': images, 'updated_at': updated})
    # Short CDN/browser cache: admin edits show up within a minute.
    response['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
    return response


class ContactThrottle(SimpleRateThrottle):
    """Per client IP, rate from REST_FRAMEWORK['DEFAULT_THROTTLE_RATES']['contact'].
    (ScopedRateThrottle needs a throttle_scope on the view and silently allows
    everything on function views without one.) The cache is per serverless
    instance, so this is a speed bump against spam bursts, not a hard global limit."""
    scope = 'contact'

    def get_cache_key(self, request, view):
        return self.cache_format % {'scope': self.scope, 'ident': self.get_ident(request)}


@api_view(['POST'])
@permission_classes([AllowAny])
@throttle_classes([ContactThrottle])
def public_contact(request):
    """POST /api/public/contact/  {name, email, phone?, topic?, subtopic?, message, lang?}"""
    ser = ContactCreateSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    if ser.validated_data.pop('website', ''):
        # Honeypot filled in: a bot. Pretend success, store nothing.
        return Response({'ok': True}, status=status.HTTP_201_CREATED)
    msg = ser.save()
    transaction.on_commit(lambda: _notify_staff(msg))
    return Response({'ok': True, 'id': msg.id}, status=status.HTTP_201_CREATED)


def _notify_staff(msg):
    to = getattr(settings, 'CONTACT_NOTIFY_EMAIL', '')
    if not to or (settings.EMAIL_BACKEND == CONSOLE_EMAIL_BACKEND and not settings.DEBUG):
        return
    try:
        send_mail(
            f'[Smart Gym] New contact message: {msg.topic or "general"}',
            f'From: {msg.name} <{msg.email}> {msg.phone}\nTopic: {msg.topic} / {msg.subtopic}\n\n{msg.message}',
            settings.DEFAULT_FROM_EMAIL, [to],
        )
    except Exception:
        logger.exception('Contact notification for message #%s failed.', msg.id)


# ============================================================
# ADMIN — pages overview
# ============================================================
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_pages(request):
    """GET /api/admin/site-content/pages/ — counts per page for the editor's sidebar."""
    texts = {r['page']: r for r in SiteText.objects.values('page').annotate(
        total=Count('id'),
        customized=Count('id', filter=~Q(en=F('default_en')) | ~Q(ar=F('default_ar'))),
    )}
    images = {r['page']: r for r in SiteImage.objects.values('page').annotate(
        total=Count('id'),
        customized=Count('id', filter=~Q(image='') & Q(image__isnull=False)),
    )}
    return Response([
        {
            'page': page, 'label': label,
            'texts': texts.get(page, {}).get('total', 0),
            'texts_customized': texts.get(page, {}).get('customized', 0),
            'images': images.get(page, {}).get('total', 0),
            'images_customized': images.get(page, {}).get('customized', 0),
        }
        for page, label in PAGES.items()
        if page in texts or page in images
    ])


# ============================================================
# ADMIN — texts
# ============================================================
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_texts(request):
    """GET /api/admin/site-content/texts/?page=home&q=hero&customized=true"""
    qs = SiteText.objects.select_related('updated_by')
    if page := request.query_params.get('page'):
        qs = qs.filter(page=page)
    if q := request.query_params.get('q', '').strip():
        qs = qs.filter(Q(key__icontains=q) | Q(en__icontains=q) | Q(ar__icontains=q))
    if request.query_params.get('customized') == 'true':
        qs = qs.filter(~Q(en=F('default_en')) | ~Q(ar=F('default_ar')))
    # Creation order = the order the texts appear on the page (seeded top to bottom).
    return Response(SiteTextSerializer(qs.order_by('page', 'id'), many=True).data)


@api_view(['PATCH'])
@permission_classes([IsAdminUser])
def admin_text_update(request, key):
    """PATCH /api/admin/site-content/texts/<key>/  {en?, ar?}"""
    obj = get_object_or_404(SiteText, key=key)
    ser = SiteTextSerializer(obj, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save(updated_by=request.user)
    return Response(ser.data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_text_reset(request, key):
    """POST /api/admin/site-content/texts/<key>/reset/ — back to the shipped text."""
    obj = get_object_or_404(SiteText, key=key)
    obj.en, obj.ar, obj.updated_by = obj.default_en, obj.default_ar, request.user
    obj.save()
    return Response(SiteTextSerializer(obj).data)


# ============================================================
# ADMIN — images
# ============================================================
@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_images(request):
    """GET /api/admin/site-content/images/?page=fac"""
    qs = SiteImage.objects.select_related('updated_by')
    if page := request.query_params.get('page'):
        qs = qs.filter(page=page)
    return Response(SiteImageSerializer(qs, many=True).data)


@api_view(['POST', 'PATCH'])
@permission_classes([IsAdminUser])
@parser_classes([MultiPartParser, FormParser, JSONParser])
def admin_image_update(request, key):
    """
    POST  /api/admin/site-content/images/<key>/  multipart {image, alt_en?, alt_ar?} — upload/replace
    PATCH /api/admin/site-content/images/<key>/  {alt_en?, alt_ar?} — alt text only
    """
    obj = get_object_or_404(SiteImage, key=key)
    ser = SiteImageUploadSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    data = ser.validated_data
    if request.method == 'POST' and 'image' not in data:
        return Response({'image': ['Choose an image to upload.']}, status=status.HTTP_400_BAD_REQUEST)

    old = obj.image.name if obj.image else None
    if 'image' in data:
        obj.image = data['image']
    for field in ('alt_en', 'alt_ar'):
        if field in data:
            setattr(obj, field, data[field])
    obj.updated_by = request.user
    obj.save()
    if old and 'image' in data and old != obj.image.name:
        _delete_file(obj.image.storage, old)
    return Response(SiteImageSerializer(obj).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_image_reset(request, key):
    """POST /api/admin/site-content/images/<key>/reset/ — remove the upload, back to the shipped image."""
    obj = get_object_or_404(SiteImage, key=key)
    if obj.image:
        storage, name = obj.image.storage, obj.image.name
        obj.image = None
        obj.updated_by = request.user
        obj.save()
        _delete_file(storage, name)
    return Response(SiteImageSerializer(obj).data)


def _delete_file(storage, name):
    try:
        storage.delete(name)
    except Exception:
        logger.exception('Could not delete replaced site image %s', name)


# ============================================================
# COLLECTIONS (admin-managed lists: facilities, articles, …)
# ============================================================
def _collection_or_404(name):
    schema = schema_for(name)
    if schema is None:
        raise Http404('Unknown collection.')
    return schema


@api_view(['GET'])
@permission_classes([AllowAny])
def public_collection(request, name):
    """GET /api/public/collections/<name>/ — active items in display order (no article bodies)."""
    schema = _collection_or_404(name)
    listed = [fd['name'] for fd in schema['fields'] if fd['name'] != 'body']
    items = SiteItem.objects.filter(collection=name, is_active=True)
    data = [public_item(i, include=listed) for i in items]
    if name == 'facilities':
        # Facility add-on price (Admin → Facility Pricing) for the signup page.
        from apps.members.models import FacilityAddon
        addons = {a.item_id: a for a in FacilityAddon.objects.filter(item__in=items)}
        for row in data:
            a = addons.get(row['id'])
            bookable = bool(a and a.is_bookable and a.monthly_price > 0)
            row['monthly_price'] = str(a.monthly_price) if bookable else None
            row['bookable'] = bookable
    response = Response(data)
    response['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
    return response


@api_view(['GET'])
@permission_classes([AllowAny])
def public_collection_item(request, name, slug):
    """GET /api/public/collections/<name>/<slug>/ — one active item, full data (article page)."""
    _collection_or_404(name)
    item = get_object_or_404(SiteItem, collection=name, slug=slug, is_active=True)
    response = Response(public_item(item))
    response['Cache-Control'] = 'public, max-age=60, stale-while-revalidate=300'
    return response


@api_view(['GET'])
@permission_classes([IsAdminUser])
def admin_collections(request):
    """GET /api/admin/site-content/collections/ — schemas (for building forms) + item counts."""
    counts = dict(SiteItem.objects.values_list('collection').annotate(n=Count('id')))
    return Response([{'name': name, **schema, 'count': counts.get(name, 0)} for name, schema in COLLECTIONS.items()])


def _duplicate_title(name, data, exclude_pk=None):
    """{'title': msg} when another item of this list already has the same English or Arabic title."""
    from apps.core.validators import norm
    title = data.get('title')
    if not isinstance(title, dict):
        return None
    mine = {lang: norm(title.get(lang)) for lang in ('en', 'ar') if norm(title.get(lang))}
    for other in SiteItem.objects.filter(collection=name).exclude(pk=exclude_pk):
        theirs = other.data.get('title') if isinstance(other.data.get('title'), dict) else {}
        for lang, value in mine.items():
            if norm(theirs.get(lang)) == value:
                label = 'English' if lang == 'en' else 'Arabic'
                return {'title': f'“{theirs.get(lang)}” is already in this list ({label} title). '
                                 'Use a different title or edit the existing one.'}
    return None


@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def admin_collection_items(request, name):
    """
    GET  /api/admin/site-content/collections/<name>/items/ — all items, incl. hidden
    POST /api/admin/site-content/collections/<name>/items/  {data, is_active?} — create (added last)
    """
    schema = _collection_or_404(name)
    if request.method == 'GET':
        items = SiteItem.objects.filter(collection=name).select_related('updated_by')
        return Response(SiteItemSerializer(items, many=True).data)
    try:
        data = clean_data(name, request.data.get('data', {}))
    except CollectionError as exc:
        return Response({'data': exc.errors}, status=status.HTTP_400_BAD_REQUEST)
    dup = _duplicate_title(name, data)
    if dup:
        return Response({'data': dup}, status=status.HTTP_400_BAD_REQUEST)
    last = SiteItem.objects.filter(collection=name).aggregate(m=Max('order'))['m'] or 0
    item = SiteItem(collection=name, data=data, order=last + 1, updated_by=request.user,
                    is_active=_as_bool(request.data.get('is_active', True)))
    if schema['has_slug']:
        item.slug = make_slug(SiteItem, name, data)
    item.save()
    return Response(SiteItemSerializer(item).data, status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def admin_collection_item(request, name, pk):
    """
    PATCH  …/collections/<name>/items/<id>/  {data? (only the fields sent), is_active?, slug?}
    DELETE …/collections/<name>/items/<id>/  (also deletes its uploaded image)
    """
    schema = _collection_or_404(name)
    item = get_object_or_404(SiteItem, collection=name, pk=pk)
    if request.method == 'DELETE':
        storage, file_name = (item.image.storage, item.image.name) if item.image else (None, None)
        item.delete()
        if file_name:
            _delete_file(storage, file_name)
        return Response(status=status.HTTP_204_NO_CONTENT)

    if 'data' in request.data:
        try:
            item.data = clean_data(name, request.data['data'], partial=True, existing=item.data)
        except CollectionError as exc:
            return Response({'data': exc.errors}, status=status.HTTP_400_BAD_REQUEST)
        dup = _duplicate_title(name, item.data, exclude_pk=item.pk)
        if dup:
            return Response({'data': dup}, status=status.HTTP_400_BAD_REQUEST)
    if 'is_active' in request.data:
        item.is_active = _as_bool(request.data['is_active'])
    if 'slug' in request.data and schema['has_slug']:
        slug = slugify(str(request.data['slug']))[:100]
        if not slug:
            return Response({'slug': 'Use letters, numbers and dashes.'}, status=status.HTTP_400_BAD_REQUEST)
        if SiteItem.objects.filter(collection=name, slug=slug).exclude(pk=item.pk).exists():
            return Response({'slug': 'Already used by another item.'}, status=status.HTTP_400_BAD_REQUEST)
        item.slug = slug
    item.updated_by = request.user
    item.save()
    return Response(SiteItemSerializer(item).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
@parser_classes([MultiPartParser, FormParser])
def admin_collection_item_image(request, name, pk):
    """POST …/collections/<name>/items/<id>/image/  multipart {image} — upload/replace."""
    schema = _collection_or_404(name)
    if not schema['has_image']:
        raise Http404('This collection has no images.')
    item = get_object_or_404(SiteItem, collection=name, pk=pk)
    ser = SiteImageUploadSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    if 'image' not in ser.validated_data:
        return Response({'image': ['Choose an image to upload.']}, status=status.HTTP_400_BAD_REQUEST)
    old = item.image.name if item.image else None
    item.image = ser.validated_data['image']
    item.updated_by = request.user
    item.save()
    if old and old != item.image.name:
        _delete_file(item.image.storage, old)
    return Response(SiteItemSerializer(item).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_collection_item_image_reset(request, name, pk):
    """POST …/collections/<name>/items/<id>/image/reset/ — back to the seeded image (or none)."""
    _collection_or_404(name)
    item = get_object_or_404(SiteItem, collection=name, pk=pk)
    if item.image:
        storage, file_name = item.image.storage, item.image.name
        item.image = None
        item.updated_by = request.user
        item.save()
        _delete_file(storage, file_name)
    return Response(SiteItemSerializer(item).data)


@api_view(['POST'])
@permission_classes([IsAdminUser])
def admin_collection_reorder(request, name):
    """POST …/collections/<name>/reorder/  {ids: [3, 1, 2, …]} — every item id, in the new order."""
    _collection_or_404(name)
    ids = request.data.get('ids')
    existing = set(SiteItem.objects.filter(collection=name).values_list('id', flat=True))
    if not isinstance(ids, list) or set(ids) != existing or len(ids) != len(existing):
        return Response({'ids': 'Send every item id of this collection exactly once.'},
                        status=status.HTTP_400_BAD_REQUEST)
    with transaction.atomic():
        for position, pk in enumerate(ids, 1):
            SiteItem.objects.filter(pk=pk).update(order=position)
    items = SiteItem.objects.filter(collection=name)
    return Response(SiteItemSerializer(items, many=True).data)


def _as_bool(value):
    return value if isinstance(value, bool) else str(value).lower() in ('1', 'true', 'yes', 'on')


# ============================================================
# ADMIN — membership plans (shown on the public site + signup)
# ============================================================
@api_view(['GET', 'POST'])
@permission_classes([IsAdminUser])
def admin_plans(request):
    """GET all plans (incl. inactive) · POST create — /api/admin/site-content/plans/"""
    if request.method == 'GET':
        plans = MembershipPlan.objects.all().order_by('price_monthly')
        return Response(AdminPlanSerializer(plans, many=True).data)
    ser = AdminPlanSerializer(data=request.data)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data, status=status.HTTP_201_CREATED)


@api_view(['PATCH', 'DELETE'])
@permission_classes([IsAdminUser])
def admin_plan(request, pk):
    """
    PATCH  /api/admin/site-content/plans/<id>/  (price changes apply to new signups only)
    DELETE — only a plan nobody has signed up for or holds; otherwise hide it (is_active=false).
    """
    plan = get_object_or_404(MembershipPlan, pk=pk)
    if request.method == 'DELETE':
        members = plan.memberships.count()
        signups = plan.registrations.count()
        if members or signups:
            parts = [f'{members} member{"s" if members != 1 else ""}' if members else '',
                     f'{signups} signup{"s" if signups != 1 else ""}' if signups else '']
            return Response({'detail': f'“{plan.display_name}” is used by {" and ".join(p for p in parts if p)}, '
                                       'so it can’t be deleted — hide it from the website instead.'},
                            status=status.HTTP_400_BAD_REQUEST)
        plan.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    ser = AdminPlanSerializer(plan, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data)


# ============================================================
# ADMIN — contact messages
# ============================================================
@api_view(['GET'])
@permission_classes([DeskRead('inbox', also_write=('PATCH',))])
def admin_messages(request):
    """GET /api/admin/site-content/messages/?status=new"""
    qs = ContactMessage.objects.all()
    if st := request.query_params.get('status'):
        qs = qs.filter(status=st)
    counts = dict(ContactMessage.objects.values_list('status').annotate(n=Count('id')))
    return Response({'counts': counts, 'results': ContactMessageSerializer(qs[:500], many=True).data})


@api_view(['PATCH', 'DELETE'])
@permission_classes([DeskRead('inbox', also_write=('PATCH',))])
def admin_message_detail(request, pk):
    """PATCH {status} · DELETE — /api/admin/site-content/messages/<id>/"""
    msg = get_object_or_404(ContactMessage, pk=pk)
    if request.method == 'DELETE':
        msg.delete()
        return Response(status=status.HTTP_204_NO_CONTENT)
    ser = ContactMessageSerializer(msg, data=request.data, partial=True)
    ser.is_valid(raise_exception=True)
    ser.save()
    return Response(ser.data)
