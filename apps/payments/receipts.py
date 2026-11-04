"""
Receipts (فاتورة) for every payment — in English and Arabic side by side — and the payment email.

Sources: a settled Transaction (every member payment: top-ups, shop, trainer sessions, courses,
renewals, facility add-ons, desk payments) or a paid website registration (before approval there
is no member account yet). A receipt opens from a signed link, so it works from an email without
signing in.

Seller details come from settings (env): GYM_LEGAL_NAME, GYM_VAT_NUMBER, GYM_ADDRESS. When a VAT
number is set, the receipt carries the ZATCA simplified-invoice QR (seller, VAT no., time, total, VAT).

With ZATCA e-invoicing on (apps.zatca), a payment that has a signed e-invoice prints as ZATCA's
simplified tax invoice instead: the seller's legal details from the ZATCA settings, net / VAT / gross
per line, the three totals and the signed QR — "TEST" outside Production, and refunds print their
credit note. A company (B2B) purchase prints as a tax invoice with the buyer's legal details and
ZATCA's stamped QR once it is cleared ("Pending clearance" until then).
"""
import base64
import logging
from decimal import Decimal
from html import escape

from django.conf import settings
from django.core import signing
from django.core.mail import EmailMultiAlternatives
from django.db import transaction as db_transaction
from django.utils import timezone

logger = logging.getLogger(__name__)
SALT = 'smartgym.receipt'

TYPE_AR = {
    'subscription': 'اشتراك', 'retail': 'المتجر', 'coaching': 'تدريب', 'topup': 'شحن رصيد الخزانة',
    'refund': 'استرداد', 'registration': 'تسجيل عضوية',
}


def seller():
    return {
        'name': getattr(settings, 'GYM_LEGAL_NAME', '') or 'Smart Gym',
        'vat': getattr(settings, 'GYM_VAT_NUMBER', ''),
        'address': getattr(settings, 'GYM_ADDRESS', ''),
    }


# ---------------------------------------------------------------- links
def token_for(kind, pk):
    return signing.dumps({'k': kind, 'id': pk}, salt=SALT, compress=True)


def read_token(token):
    try:
        data = signing.loads(token, salt=SALT)
        return data['k'], int(data['id'])
    except (signing.BadSignature, KeyError, TypeError, ValueError):
        return None, None


def receipt_url(kind, pk):
    base = getattr(settings, 'BACKEND_PUBLIC_URL', '') or 'https://gym-backend-xi-seven.vercel.app'
    return f"{base.rstrip('/')}/api/payments/receipts/{token_for(kind, pk)}/"


# ---------------------------------------------------------------- data
def _money(v):
    return Decimal(str(v or 0)).quantize(Decimal('0.01'))


def _zatca_document(**filters):
    """The signed e-invoice / credit note for a payment, if ZATCA issued one (never raises)."""
    try:
        from apps.zatca.models import ZatcaDocument
        return ZatcaDocument.objects.select_related('device', 'original').filter(**filters).order_by('icv').first()
    except Exception:   # noqa: BLE001 — e.g. ZATCA tables not migrated yet: print the plain receipt
        logger.exception('Could not look up the ZATCA document for %s', filters)
        return None


def _with_zatca(data, doc):
    if doc is None:
        return data
    from apps.zatca.models import ZatcaConfig
    cfg = ZatcaConfig.load()
    data.update({
        'number': doc.number, 'when': doc.issued_at, 'zatca': {
            'doc_type': doc.doc_type, 'test': doc.is_test, 'qr': doc.qr, 'rate': doc.vat_rate,
            'lines': doc.lines, 'net': doc.net, 'vat': doc.vat, 'rounding': doc.rounding, 'total': doc.total,
            'original': doc.original.number if doc.original_id else '', 'reason': doc.reason,
            'standard': doc.is_standard, 'buyer': doc.buyer_details, 'status': doc.status,
            'seller': {'name_ar': cfg.seller_name_ar, 'name_en': cfg.seller_name_en, 'vat': cfg.vat_number,
                       'crn': cfg.crn, 'address': cfg.address_line},
        },
    })
    return data


def from_transaction(txn):
    from .models import Invoice, PaymentIntent
    inv = Invoice.objects.filter(transaction=txn).first()
    intent = PaymentIntent.objects.select_related('order').filter(transaction=txn).first()
    lines, subtotal, discount, tax = [], _money(txn.amount), _money(0), _money(txn.tax_amount)
    order = getattr(intent, 'order', None) if intent else None
    if order is not None:
        for it in order.items.all():
            lines.append({'en': it.product_name, 'ar': it.product_name, 'qty': it.quantity, 'amount': _money(it.line_total)})
        subtotal, discount, tax = _money(order.subtotal), _money(order.discount), _money(order.tax)
    else:
        lines.append({'en': txn.description, 'ar': txn.description, 'qty': 1, 'amount': _money(txn.amount)})
    return _with_zatca({
        'kind': 't', 'pk': txn.id,
        'number': inv.invoice_number if inv else txn.transaction_id,
        'when': txn.settled_at or txn.created_at,
        'customer': txn.member.full_name or txn.member.email, 'email': txn.member.email,
        'type_en': txn.get_transaction_type_display(), 'type_ar': TYPE_AR.get(txn.transaction_type, ''),
        'lines': lines, 'subtotal': subtotal, 'discount': discount, 'tax': tax, 'total': _money(txn.amount),
        'method': txn.payment_method_label or '—', 'reference': txn.gateway_reference or txn.transaction_id,
    }, _zatca_document(transaction=txn))


def from_registration(reg):
    lines = [{'en': f'{reg.plan.display_name} membership × {reg.duration_months} months',
              'ar': f'عضوية {reg.plan.display_name} × {reg.duration_months} شهر',
              'qty': 1, 'amount': _money(reg.payment_amount) - sum(_money(f['amount']) for f in reg.facilities or [])}]
    for f in reg.facilities or []:
        lines.append({'en': f"{f['name']} × {f['months']} months", 'ar': f"{f['name']} × {f['months']} شهر",
                      'qty': 1, 'amount': _money(f['amount'])})
    method = {'mada': 'mada', 'creditcard': 'Card', 'stcpay': 'STC Pay', 'applepay': 'Apple Pay'}.get(
        (reg.payment_method or '').lower(), (reg.payment_method or 'Card').upper())
    return _with_zatca({
        'kind': 'r', 'pk': reg.id, 'number': f'REG-{reg.id:06d}', 'when': reg.payment_paid_at or reg.submitted_at,
        'customer': reg.full_name, 'email': reg.email,
        'type_en': 'Membership registration', 'type_ar': TYPE_AR['registration'],
        'lines': lines, 'subtotal': _money(reg.payment_amount), 'discount': _money(0), 'tax': _money(0),
        'total': _money(reg.payment_amount), 'method': method, 'reference': reg.moyasar_payment_id or '',
    }, _zatca_document(registration=reg, doc_type='invoice'))


def zatca_qr(data):
    """ZATCA phase-1 TLV (base64): 1 seller, 2 VAT no., 3 ISO time, 4 total, 5 VAT. Empty without a VAT number."""
    s = seller()
    if not s['vat']:
        return ''
    when = timezone.localtime(data['when']).replace(microsecond=0).isoformat()
    out = b''
    for tag, value in ((1, s['name']), (2, s['vat']), (3, when), (4, f"{data['total']:.2f}"), (5, f"{data['tax']:.2f}")):
        b = str(value).encode('utf-8')
        out += bytes([tag, len(b)]) + b
    return base64.b64encode(out).decode()


# ---------------------------------------------------------------- render
TAX_TITLES = {
    'invoice': ('Simplified tax invoice', 'فاتورة ضريبية مبسطة'),
    'credit': ('Credit note', 'إشعار دائن'),
    'debit': ('Debit note', 'إشعار مدين'),
}
STANDARD_TITLES = {
    'invoice': ('Tax invoice', 'فاتورة ضريبية'),
    'credit': ('Credit note (tax invoice)', 'إشعار دائن (فاتورة ضريبية)'),
    'debit': ('Debit note (tax invoice)', 'إشعار مدين (فاتورة ضريبية)'),
}


def _render_tax_invoice(data):
    """ZATCA's simplified tax invoice layout for a payment with a signed e-invoice."""
    z, e = data['zatca'], escape
    s = z['seller']
    when = timezone.localtime(data['when'])
    title_en, title_ar = (STANDARD_TITLES if z.get('standard') else TAX_TITLES)[z['doc_type']]
    buyer = z.get('buyer') or {}
    pending = ("<div class='test'>Pending clearance by ZATCA — this invoice is final once cleared. · "
               "بانتظار اعتماد زاتكا — تصبح الفاتورة نهائية بعد الاعتماد.</div>"
               if z.get('standard') and z.get('status') == 'pending' else '')
    if buyer:
        address = (f"{e(buyer.get('building_number'))} {e(buyer.get('street'))}, {e(buyer.get('district'))}, "
                   f"{e(buyer.get('city'))} {e(buyer.get('postal_code'))}")
        crn = f"<br><span class='muted'>CR · السجل التجاري {e(buyer.get('crn'))}</span>" if buyer.get('crn') else ''
        party = (f"<div><b>Buyer · المشتري</b>{e(buyer.get('name'))}<br><span class='muted'>{address}</span></div>"
                 f"<div><b>Buyer VAT number · الرقم الضريبي للمشتري</b>{e(buyer.get('vat_number'))}{crn}</div>")
    else:
        party = f"<div><b>Customer · العميل</b>{e(data['customer'])}</div>"
    rows = ''.join(
        f"<tr><td>{e(l['name'])}</td><td class='n'>{l['qty']}</td><td class='n'>{Decimal(l['unit_net']):.2f}</td>"
        f"<td class='n'>{Decimal(l['vat']):.2f}</td><td class='n'>{Decimal(l['net']) + Decimal(l['vat']):.2f}</td></tr>"
        for l in z['lines'])
    rounding = (f"<tr><td>Rounding · التقريب</td><td class='n'>{z['rounding']:.2f}</td></tr>" if z['rounding'] else '')
    test = ("<div class='test'>TEST — not a legal tax invoice · تجريبي — ليست فاتورة ضريبية</div>" if z['test'] else '')
    ref = (f"<div><b>Original invoice · الفاتورة الأصلية</b>{e(z['original'])}<br><span class='muted'>{e(z['reason'])}</span></div>"
           if z['original'] else '')
    names = e(s['name_ar']) + (f"<br>{e(s['name_en'])}" if s['name_en'] and s['name_en'] != s['name_ar'] else '')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title_en)} {e(data['number'])}</title>
<style>
body{{font-family:system-ui,'Segoe UI',Tahoma,sans-serif;background:#f4f4f5;margin:0;padding:24px;color:#111}}
.r{{max-width:720px;margin:auto;background:#fff;border-radius:10px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
h1{{font-size:20px;margin:0;text-align:center}} .ar{{direction:rtl;font-family:Tahoma,'Segoe UI',sans-serif}}
.muted{{color:#666;font-size:12px;margin:4px 0}} .center{{text-align:center}}
.test{{background:#fff4d6;color:#7a5200;border:1px solid #f0c36d;border-radius:6px;padding:8px;text-align:center;font-weight:700;margin-bottom:14px}}
.grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px 24px;margin:16px 0;font-size:13px}}
.grid b{{display:block;font-size:11px;color:#666;font-weight:600}} table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th{{text-align:left;font-size:11px;color:#666;border-bottom:1px solid #ddd;padding:6px 4px}} td{{padding:8px 4px;border-bottom:1px solid #f0f0f0;vertical-align:top}}
.n{{text-align:right;white-space:nowrap}} .tot td{{font-weight:700;font-size:16px;border:0;padding-top:12px}}
.btn{{display:inline-block;margin-top:18px;padding:10px 16px;background:#e51937;color:#fff;border:0;border-radius:6px;font-weight:700;cursor:pointer}}
#zqr{{display:flex;justify-content:center;margin-top:16px}} @media print{{body{{background:#fff;padding:0}}.r{{box-shadow:none}}.btn{{display:none}}}}
</style></head><body><div class="r">
{test}{pending}
<h1>{e(title_en)}</h1><h1 class="ar">{e(title_ar)}</h1>
<p class="center muted">{e(data['number'])}</p>
<div class="grid">
<div><b>Seller · البائع</b>{names}<br><span class="muted">{e(s['address'])}</span></div>
<div><b>VAT number · الرقم الضريبي</b>{e(s['vat'])}<br><span class="muted">CR · السجل التجاري {e(s['crn'])}</span></div>
<div><b>Date and time · التاريخ والوقت</b>{when:%Y-%m-%d %H:%M:%S}</div>
{party}
{ref}
</div>
<table><thead><tr><th>Item · البند</th><th class="n">Qty · الكمية</th><th class="n">Price excl. VAT · السعر</th>
<th class="n">VAT · الضريبة</th><th class="n">Total incl. VAT · الإجمالي</th></tr></thead>
<tbody>{rows}</tbody></table>
<table>
<tr><td>Total excl. VAT · الإجمالي غير شامل الضريبة</td><td class='n'>{z['net']:.2f}</td></tr>
<tr><td>VAT {z['rate']:.0f}% · ضريبة القيمة المضافة</td><td class='n'>{z['vat']:.2f}</td></tr>
{rounding}
<tr class="tot"><td>Total incl. VAT · الإجمالي شامل الضريبة</td><td class="n">SAR {z['total']:.2f}</td></tr>
</table>
<p class="muted">Paid with · طريقة الدفع: {e(data['method'])}</p>
<div id="zqr" data-v="{e(z['qr'])}"></div>
<p class="center muted">End of invoice · نهاية الفاتورة</p>
<div class="center"><button class="btn" onclick="window.print()">Print / Save PDF · طباعة</button></div>
</div>
<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script><script>var z=document.getElementById('zqr');new QRCode(z,{{text:z.dataset.v,width:160,height:160,correctLevel:QRCode.CorrectLevel.M}});</script>
</body></html>"""


def render_html(data):
    if data.get('zatca'):
        return _render_tax_invoice(data)
    s = seller()
    e = escape
    when = timezone.localtime(data['when'])
    rows = ''.join(
        f"<tr><td>{e(l['en'])}<div class='ar'>{e(l['ar'])}</div></td><td class='n'>{l['qty']}</td>"
        f"<td class='n'>{l['amount']:.2f}</td></tr>" for l in data['lines'])
    sums = ''
    if data['discount']:
        sums += f"<tr><td>Discount · الخصم</td><td class='n'>-{data['discount']:.2f}</td></tr>"
    if data['tax']:
        sums += (f"<tr><td>Subtotal · المجموع الفرعي</td><td class='n'>{data['subtotal']:.2f}</td></tr>"
                 f"<tr><td>VAT · ضريبة القيمة المضافة</td><td class='n'>{data['tax']:.2f}</td></tr>")
    qr = zatca_qr(data)
    seller_lines = e(s['name']) + (f"<br>{e(s['address'])}" if s['address'] else '') + (
        f"<br>VAT No. · الرقم الضريبي: {e(s['vat'])}" if s['vat'] else '')
    qr_block = (f"<div id='zqr' data-v='{e(qr)}'></div><p class='muted'>ZATCA QR · رمز الفاتورة</p>" if qr else '')
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Receipt {e(data['number'])} · إيصال</title>
<style>
body{{font-family:system-ui,'Segoe UI',Tahoma,sans-serif;background:#f4f4f5;margin:0;padding:24px;color:#111}}
.r{{max-width:720px;margin:auto;background:#fff;border-radius:10px;padding:28px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.top{{display:flex;justify-content:space-between;gap:16px;align-items:flex-start;border-bottom:3px solid #e51937;padding-bottom:14px}}
h1{{font-size:20px;margin:0}} .ar{{direction:rtl;text-align:right;font-family:Tahoma,'Segoe UI',sans-serif;color:#444;font-size:13px}}
.muted{{color:#666;font-size:12px;margin:4px 0}} .grid{{display:grid;grid-template-columns:1fr 1fr;gap:8px 24px;margin:16px 0;font-size:13px}}
.grid b{{display:block;font-size:11px;color:#666;font-weight:600}} table{{width:100%;border-collapse:collapse;font-size:13px;margin-top:8px}}
th{{text-align:left;font-size:11px;color:#666;border-bottom:1px solid #ddd;padding:6px 0}} td{{padding:8px 0;border-bottom:1px solid #f0f0f0;vertical-align:top}}
.n{{text-align:right;white-space:nowrap}} .tot td{{font-weight:700;font-size:16px;border:0;padding-top:12px}}
.btn{{display:inline-block;margin-top:18px;padding:10px 16px;background:#e51937;color:#fff;border:0;border-radius:6px;font-weight:700;cursor:pointer}}
#zqr{{margin-top:16px}} @media print{{body{{background:#fff;padding:0}}.r{{box-shadow:none}}.btn{{display:none}}}}
</style></head><body><div class="r">
<div class="top"><div><h1>{e(s['name'])}</h1><p class="muted">{seller_lines}</p></div>
<div style="text-align:right"><h1>Receipt · إيصال</h1><p class="muted">{e(data['number'])}</p></div></div>
<div class="grid">
<div><b>Date · التاريخ</b>{when:%d %b %Y, %H:%M}</div>
<div><b>Type · النوع</b>{e(data['type_en'])} · <span class="ar">{e(data['type_ar'])}</span></div>
<div><b>Customer · العميل</b>{e(data['customer'])}<br><span class="muted">{e(data['email'])}</span></div>
<div><b>Paid with · طريقة الدفع</b>{e(data['method'])}<br><span class="muted">{e(data['reference'])}</span></div>
</div>
<table><thead><tr><th>Item · البند</th><th class="n">Qty · الكمية</th><th class="n">SAR · ريال</th></tr></thead>
<tbody>{rows}</tbody></table>
<table>{sums}<tr class="tot"><td>Total paid · الإجمالي المدفوع</td><td class="n">SAR {data['total']:.2f}</td></tr></table>
{qr_block}
<p class="muted">Thank you for training with us. · شكرًا لتدريبك معنا.</p>
<button class="btn" onclick="window.print()">Print / Save PDF · طباعة</button>
</div>
{"<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script><script>var z=document.getElementById('zqr');new QRCode(z,{text:z.dataset.v,width:132,height:132});</script>" if qr else ''}
</body></html>"""


# ---------------------------------------------------------------- email
def send_receipt_email(data):
    """Bilingual 'payment received' email with the receipt link. Never raises."""
    if not data.get('email'):
        return
    url = receipt_url(data['kind'], data['pk'])
    s = seller()
    when = timezone.localtime(data['when'])
    items = '\n'.join(f"  - {l['en']}: SAR {l['amount']:.2f}" for l in data['lines'])
    text = (f"Hi {data['customer']},\n\nWe received your payment of SAR {data['total']:.2f} on {when:%d %b %Y %H:%M}.\n\n"
            f"{items}\n\n  Receipt: {data['number']}\n  Paid with: {data['method']}\n\nView or print your receipt: {url}\n\n"
            f"— {s['name']}\n\n"
            f"مرحبًا {data['customer']}،\n\nاستلمنا دفعتك بقيمة {data['total']:.2f} ريال.\nرقم الإيصال: {data['number']}\n"
            f"لعرض الإيصال أو طباعته: {url}\n\n— {s['name']}")
    html = (f"<div style='font-family:system-ui,sans-serif;max-width:560px'>"
            f"<p>Hi {escape(data['customer'])},</p><p>We received your payment of <b>SAR {data['total']:.2f}</b>.</p>"
            f"<p>Receipt <b>{escape(data['number'])}</b> · {escape(data['method'])}</p>"
            f"<p><a href='{url}' style='background:#e51937;color:#fff;padding:10px 16px;border-radius:6px;text-decoration:none;font-weight:700'>"
            f"View receipt · عرض الإيصال</a></p>"
            f"<div dir='rtl' style='text-align:right;font-family:Tahoma,sans-serif;color:#333;margin-top:18px'>"
            f"<p>مرحبًا {escape(data['customer'])}،</p><p>استلمنا دفعتك بقيمة <b>{data['total']:.2f} ريال</b>.</p>"
            f"<p>رقم الإيصال <b>{escape(data['number'])}</b></p></div><p style='color:#888'>— {escape(s['name'])}</p></div>")
    try:
        msg = EmailMultiAlternatives(f"Payment received · تم استلام الدفع — {data['number']}", text,
                                     settings.DEFAULT_FROM_EMAIL, [data['email']])
        msg.attach_alternative(html, 'text/html')
        msg.send()
    except Exception:   # noqa: BLE001 — the payment stands even if the email fails
        logger.exception('Receipt email %s failed', data['number'])


def email_after_commit(build, obj):
    """Send the receipt email once the surrounding database transaction has committed."""
    def go():
        try:
            send_receipt_email(build(obj))
        except Exception:   # noqa: BLE001
            logger.exception('Could not build receipt for %r', obj)
    db_transaction.on_commit(go)
