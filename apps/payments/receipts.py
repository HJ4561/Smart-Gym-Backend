"""
Receipts (فاتورة) for every payment — in English and Arabic side by side — and the payment email.

Sources: a settled Transaction (every member payment: top-ups, shop, trainer sessions, courses,
renewals, facility add-ons, desk payments) or a paid website registration (before approval there
is no member account yet). A receipt opens from a signed link, so it works from an email without
signing in.

Seller details come from settings (env): GYM_LEGAL_NAME (+ _AR), GYM_VAT_NUMBER, GYM_ADDRESS (+ _AR); the
logo and the Arabic address can also be set on the ZATCA page. When a VAT number is set, the receipt
carries the ZATCA simplified-invoice QR (seller, VAT no., time, total, VAT).

The layout follows a till Fatoora: English label · value · Arabic label on each line, items with their
Arabic and English names, price excl. VAT / VAT / total per line, the three totals, the QR, "End of invoice".

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
        for it in order.items.select_related('product'):
            lines.append({'en': it.product_name, 'ar': getattr(it.product, 'name_ar', '') or '', 'qty': it.quantity,
                          'amount': _money(it.line_total)})
        subtotal, discount, tax = _money(order.subtotal), _money(order.discount), _money(order.tax)
    else:
        # A free-text description has no Arabic of its own: the Arabic line names the kind of payment.
        lines.append({'en': txn.description, 'ar': TYPE_AR.get(txn.transaction_type, ''), 'qty': 1,
                      'amount': _money(txn.amount)})
    return _with_zatca({
        'kind': 't', 'pk': txn.id,
        'number': inv.invoice_number if inv else txn.transaction_id,
        'when': txn.settled_at or txn.created_at,
        'customer': txn.member.full_name or txn.member.email, 'email': txn.member.email,
        'type_en': txn.get_transaction_type_display(), 'type_ar': TYPE_AR.get(txn.transaction_type, ''),
        'lines': lines, 'subtotal': subtotal, 'discount': discount, 'tax': tax, 'total': _money(txn.amount),
        'method': txn.payment_method_label or '—', 'reference': txn.gateway_reference or txn.transaction_id,
        'order': order.order_number if order is not None else '',
    }, _zatca_document(transaction=txn))


def _facility_names_ar(ids):
    try:
        from apps.site_content.models import SiteItem
        return {i.id: ((i.data or {}).get('title') or {}).get('ar') or ''
                for i in SiteItem.objects.filter(id__in=ids)}
    except Exception:   # noqa: BLE001 — the receipt prints the English name alone
        return {}


def from_registration(reg):
    facilities = reg.facilities or []
    names_ar = _facility_names_ar([f.get('id') for f in facilities])
    plan_ar = reg.plan.display_name_ar or reg.plan.display_name
    lines = [{'en': f'{reg.plan.display_name} membership × {reg.duration_months} months',
              'ar': f'عضوية {plan_ar} × {reg.duration_months} شهر',
              'qty': 1, 'amount': _money(reg.payment_amount) - sum(_money(f['amount']) for f in facilities)}]
    for f in facilities:
        lines.append({'en': f"{f['name']} × {f['months']} months",
                      'ar': f"{names_ar.get(f.get('id')) or f['name']} × {f['months']} شهر",
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
# One receipt layout (a till-style Fatoora): English labels on the left, Arabic labels on the right, the value
# in between. A payment with a signed ZATCA e-invoice prints as the tax invoice; one without prints as a receipt.
TAX_TITLES = {
    'invoice': ('SIMPLIFIED TAX INVOICE', 'فاتورة ضريبية مبسطة'),
    'credit': ('CREDIT NOTE', 'إشعار دائن'),
    'debit': ('DEBIT NOTE', 'إشعار مدين'),
}
STANDARD_TITLES = {
    'invoice': ('TAX INVOICE', 'فاتورة ضريبية'),
    'credit': ('CREDIT NOTE (TAX INVOICE)', 'إشعار دائن (فاتورة ضريبية)'),
    'debit': ('DEBIT NOTE (TAX INVOICE)', 'إشعار مدين (فاتورة ضريبية)'),
}
RECEIPT_TITLE = ('RECEIPT', 'إيصال')


def _config():
    """The ZATCA settings row if there is one (never creates it, never raises)."""
    try:
        from apps.zatca.models import ZatcaConfig
        return ZatcaConfig.objects.filter(pk=1).first()
    except Exception:   # noqa: BLE001 — ZATCA tables not migrated yet
        return None


def _logo_url(cfg):
    try:
        return cfg.logo.url if cfg is not None and cfg.logo else ''
    except Exception:   # noqa: BLE001 — storage not reachable: print without the logo
        return ''


def _header(data):
    """Seller name, address, VAT and CR numbers and the logo for the top of the receipt."""
    cfg = _config()
    z = data.get('zatca')
    if z:
        s = z['seller']
        return {'name_ar': s['name_ar'], 'name_en': s['name_en'], 'address': s['address'],
                'address_ar': getattr(cfg, 'address_ar', ''), 'vat': s['vat'], 'crn': s['crn'], 'logo': _logo_url(cfg)}
    s = seller()
    return {'name_ar': getattr(settings, 'GYM_LEGAL_NAME_AR', ''), 'name_en': s['name'], 'address': s['address'],
            'address_ar': getattr(cfg, 'address_ar', '') or getattr(settings, 'GYM_ADDRESS_AR', ''),
            'vat': s['vat'], 'crn': '', 'logo': _logo_url(cfg)}


def _plain_lines(data):
    """Net / VAT / gross per line for a receipt without an e-invoice: what was paid spread over the items."""
    lines = data['lines']
    base = sum((l['amount'] for l in lines), Decimal(0))
    total, tax = data['total'], data['tax']
    out, gross_left, tax_left = [], total, tax
    for i, l in enumerate(lines):
        last = i == len(lines) - 1
        gross = gross_left if last else _money(total * l['amount'] / base) if base else _money(0)
        vat = tax_left if last else _money(tax * gross / total) if total else _money(0)
        gross_left, tax_left = gross_left - gross, tax_left - vat
        qty = l['qty'] or 1
        out.append({'en': l['en'], 'ar': l['ar'], 'qty': qty, 'unit_net': _money((gross - vat) / qty),
                    'vat': vat, 'total': gross})
    return out


def _zatca_lines(data):
    arabic = {l['en']: l['ar'] for l in data['lines']}
    return [{'en': l['name'], 'ar': arabic.get(l['name'], ''), 'qty': l['qty'], 'unit_net': Decimal(l['unit_net']),
             'vat': Decimal(l['vat']), 'total': Decimal(l['net']) + Decimal(l['vat'])} for l in data['zatca']['lines']]


def _row(en, value, ar, cls=''):
    """One 'English label · value · Arabic label' line (the value may be a mix of both languages)."""
    return (f"<div class='row {cls}'><span class='en'>{escape(en)}</span><span class='v'>{value}</span>"
            f"<span class='arl' dir='rtl'>{escape(ar)}</span></div>")


def _both(en, ar):
    """'English / Arabic' with each part in its own direction."""
    if not ar or ar == en:
        return f"<bdi>{escape(en)}</bdi>"
    return f"<bdi dir='ltr'>{escape(en)}</bdi> / <bdi dir='rtl'>{escape(ar)}</bdi>"


def render_html(data):
    e = escape
    z = data.get('zatca')
    h = _header(data)
    when = timezone.localtime(data['when'])
    if z:
        title_en, title_ar = (STANDARD_TITLES if z.get('standard') else TAX_TITLES)[z['doc_type']]
        lines, rate = _zatca_lines(data), z['rate']
        net, vat, rounding, total = z['net'], z['vat'], z['rounding'], z['total']
        qr, test = z['qr'], z['test']
    else:
        title_en, title_ar = RECEIPT_TITLE
        lines = _plain_lines(data)
        total, vat = data['total'], data['tax']
        net, rounding = total - vat, Decimal(0)
        rate = (vat * 100 / net).quantize(Decimal('1')) if net and vat else Decimal(0)
        qr, test = zatca_qr(data), False

    banner = ("<div class='test'><b>TEST / <span dir='rtl'>تجريبي</span></b>"
              "<small>Not a legally valid invoice · <span dir='rtl'>ليست فاتورة ضريبية</span></small></div>"
              "<div class='wm' aria-hidden='true'>TEST · تجريبي</div>" if test else '')
    pending = ("<div class='note'>Pending clearance by ZATCA — final once cleared.<br>"
               "<span dir='rtl'>بانتظار اعتماد زاتكا — تصبح الفاتورة نهائية بعد الاعتماد.</span></div>"
               if z and z.get('standard') and z.get('status') == 'pending' else '')
    logo = f"<img class='logo' src='{e(h['logo'])}' alt=''>" if h['logo'] else ''
    names = ''.join(f"<div class='name' dir='{d}'>{e(n)}</div>" for n, d in ((h['name_ar'], 'rtl'), (h['name_en'], 'ltr'))
                    if n and not (d == 'ltr' and n == h['name_ar']))
    address = (f"<div class='addr'>{e(h['address'])}</div>" if h['address'] else '') + (
        f"<div class='addr' dir='rtl'>{e(h['address_ar'])}</div>" if h['address_ar'] else '')
    ids = (_row('VAT No.', f"<b class='num'>{e(h['vat'])}</b>", 'الرقم الضريبي') if h['vat'] else '') + (
        _row('CR No.', f"<b class='num'>{e(h['crn'])}</b>", 'السجل التجاري') if h['crn'] else '')

    meta = [_row('Invoice No.' if z else 'Receipt No.', f"<b class='num'>{e(data['number'])}</b>",
                 'رقم الفاتورة' if z else 'رقم الإيصال'),
            _row('Date', f"<b class='num'>{when:%Y-%m-%d}</b>", 'التاريخ'),
            _row('Time', f"<b class='num'>{when:%H:%M:%S}</b>", 'الوقت'),
            _row('Order type', f"<b>{_both(data['type_en'], data['type_ar'])}</b>", 'نوع الطلب')]
    if data.get('order'):
        meta.append(_row('Order', f"<b class='num'>{e(data['order'])}</b>", 'الطلب'))
    elif data.get('reference') and data['reference'] != data['number']:
        meta.append(_row('Reference', f"<b class='num'>{e(data['reference'])}</b>", 'المرجع'))
    buyer = (z or {}).get('buyer') or {}
    if buyer:
        place = ' '.join(str(buyer.get(k) or '') for k in ('building_number', 'street', 'district', 'city', 'postal_code'))
        meta += [_row('Buyer', f"<b>{e(buyer.get('name'))}</b>", 'المشتري'),
                 _row('Buyer VAT No.', f"<b class='num'>{e(buyer.get('vat_number'))}</b>", 'الرقم الضريبي للمشتري'),
                 _row('Address', e(' '.join(place.split())), 'العنوان')]
        if buyer.get('crn'):
            meta.append(_row('Buyer CR No.', f"<b class='num'>{e(buyer['crn'])}</b>", 'السجل التجاري للمشتري'))
    elif data.get('customer'):
        meta.append(_row('Customer', f"<b>{e(data['customer'])}</b>", 'العميل'))
    if z and z['original']:
        meta.append(_row('Original invoice', f"<b class='num'>{e(z['original'])}</b>", 'الفاتورة الأصلية'))
        if z['reason']:
            meta.append(_row('Reason', e(z['reason']), 'السبب'))

    items = ''.join(
        "<tr><td class='item'>"
        + (f"<div dir='rtl'>{e(l['ar'])}</div>" if l['ar'] and l['ar'] != l['en'] else '')
        + f"<div dir='ltr'>{e(l['en'])}</div></td><td class='c num'>{l['qty']}</td>"
        f"<td class='num'>{l['unit_net']:.2f}</td><td class='num'>{l['vat']:.2f}</td>"
        f"<td class='num'>{l['total']:.2f}</td></tr>" for l in lines)

    def total_row(en, ar, amount, cls=''):
        return (f"<div class='tot {cls}'><span class='num'>{amount:.2f}</span>"
                f"<span class='lbl'><span dir='rtl'>{escape(ar)}</span><small>{escape(en)}</small></span></div>")
    totals = (total_row('Total taxable amount', 'إجمالي المبلغ الخاضع للضريبة', net)
              + total_row(f'VAT ({rate:.0f}%)', f'ضريبة القيمة المضافة ({rate:.0f}%)', vat)
              + (total_row('Rounding', 'التقريب', rounding) if rounding else '')
              + total_row('Total incl. VAT', 'المجموع مع الضريبة', total, 'grand'))
    method = data.get('method') or ''
    paid = '<div class="dash"></div>' + total_row(method, method, total) if method and method != '—' else ''
    qr_block = f"<div id='zqr' data-v='{e(qr)}'></div>" if qr else ''
    qr_script = ("<script src='https://cdnjs.cloudflare.com/ajax/libs/qrcodejs/1.0.0/qrcode.min.js'></script>"
                 "<script>var z=document.getElementById('zqr');if(z&&window.QRCode)new QRCode(z,{text:z.dataset.v,"
                 "width:150,height:150,correctLevel:QRCode.CorrectLevel.M});</script>" if qr else '')

    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{e(title_en.title())} {e(data['number'])}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Noto+Sans+Arabic:wght@400;700&family=Poppins:wght@400;600;700&display=swap" rel="stylesheet">
<style>
*{{box-sizing:border-box}}
body{{font-family:Poppins,'Noto Sans Arabic',system-ui,'Segoe UI',Tahoma,sans-serif;background:#f4f4f5;margin:0;padding:24px 12px;color:#111;font-size:12px}}
[dir=rtl]{{font-family:'Noto Sans Arabic',Tahoma,'Segoe UI',sans-serif}}
.r{{position:relative;overflow:hidden;max-width:400px;margin:auto;background:#fff;padding:18px 16px 22px;box-shadow:0 2px 12px rgba(0,0,0,.08)}}
.test{{border:2px solid #111;text-align:center;padding:4px 6px 6px;margin-bottom:12px}}
.test b{{display:block;font-size:17px}} .test small{{font-size:10px;color:#333}}
.wm{{position:absolute;top:42%;left:50%;transform:translate(-50%,-50%) rotate(-30deg);font-size:64px;font-weight:700;color:rgba(0,0,0,.05);white-space:nowrap;pointer-events:none}}
.note{{border:1px dashed #b07a00;color:#7a5200;text-align:center;padding:6px;margin-bottom:10px;font-size:11px}}
.head{{text-align:center}} .logo{{display:block;max-width:90px;max-height:90px;margin:0 auto 6px;object-fit:contain}}
.name{{font-weight:700;font-size:15px;line-height:1.35}} .addr{{color:#333;line-height:1.4}}
.row{{display:grid;grid-template-columns:auto 1fr auto;gap:8px;align-items:baseline;line-height:1.6}}
.row .en{{color:#333}} .row .v{{text-align:center;overflow-wrap:anywhere}} .row .arl{{color:#333;font-size:11px;text-align:right}}
.ids{{margin-top:6px}} .title{{text-align:center;margin:12px 0 6px}} .title div{{font-weight:700;font-size:15px;line-height:1.35}}
.dash{{border-top:1px dashed #555;margin:6px 0}}
.num{{font-family:'Courier New',ui-monospace,monospace;font-variant-numeric:tabular-nums}}
table{{width:100%;border-collapse:collapse;table-layout:fixed}}
th{{font-size:9px;font-weight:700;vertical-align:bottom;padding:4px 2px;text-align:right;line-height:1.3}}
th span{{display:block;font-weight:400}} th.item{{width:40%;text-align:left}} th.c{{width:9%;text-align:center}}
td{{padding:5px 2px;vertical-align:top;text-align:right;font-size:12px}} td.item{{text-align:left;overflow-wrap:anywhere}}
td.item div[dir=rtl]{{text-align:right;color:#333}} td.c{{text-align:center}}
.cur{{text-align:center;font-size:10px;color:#444;margin:4px 0}}
.tot{{display:flex;justify-content:space-between;align-items:flex-start;gap:10px;margin:4px 0}}
.tot .lbl{{text-align:right}} .tot .lbl small{{display:block;font-size:10px;color:#444}}
.tot.grand .num{{font-size:17px;font-weight:700}} .tot.grand .lbl span{{font-weight:700;font-size:14px}} .tot.grand .lbl small{{font-weight:700;color:#111}}
#zqr{{display:flex;justify-content:center;margin:12px 0 4px}}
.end{{text-align:center;font-weight:700;margin-top:8px}}
.btn{{display:block;margin:16px auto 0;padding:9px 16px;background:#e51937;color:#fff;border:0;border-radius:6px;font-weight:700;cursor:pointer;font-family:inherit}}
@media print{{body{{background:#fff;padding:0}}.r{{box-shadow:none;max-width:none}}.btn{{display:none}}@page{{margin:8mm}}}}
</style></head><body><div class="r">
{banner}{pending}
<div class="head">{logo}{names}{address}</div>
<div class="ids">{ids}</div>
<div class="title"><div dir="rtl">{e(title_ar)}</div><div>{e(title_en)}</div></div>
<div class="dash"></div>
{''.join(meta)}
<div class="dash"></div>
<table><thead><tr>
<th class="item"><span dir="rtl">المنتجات</span>Item</th><th class="c"><span dir="rtl">الكمية</span>Qty</th>
<th><span dir="rtl">السعر</span>Price (excl. VAT)</th><th><span dir="rtl">ضريبة القيمة المضافة</span>VAT</th>
<th><span dir="rtl">السعر شامل الضريبة</span>Total incl. VAT</th></tr></thead>
<tbody>{items}</tbody></table>
<div class="dash"></div>
<div class="cur">Amounts in SAR · <span dir="rtl">المبالغ بالريال السعودي</span></div>
{totals}
{paid}
<div class="dash"></div>
{qr_block}
<div class="end">&lt;&lt;&lt;&lt; End of invoice / <span dir="rtl">نهاية الفاتورة</span> &gt;&gt;&gt;&gt;</div>
<button class="btn" onclick="window.print()">Print / Save PDF · <span dir="rtl">طباعة</span></button>
</div>
{qr_script}
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
