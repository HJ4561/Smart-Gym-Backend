# ZATCA e-invoicing — owner's runbook

A plain guide for the gym owner and the admin team: what the system does with tax invoices, what the
screens mean, and what to do when something needs attention. Everything here happens in the admin
portal under **Finance → ZATCA e-invoicing** (only admins can change things; managers can look).

---

## 1. What happens automatically

Every time a member pays (card, STC Pay, a desk payment), the system:

1. creates a **tax invoice** with the next number in the chain (for example `SG3-INV-000124`),
2. **signs** it with the gym's ZATCA certificate and adds the QR code,
3. **sends** it to ZATCA:
   * normal customers (simplified invoices) are **reported** — the customer gets the invoice right away;
   * company customers (B2B, the buyer's VAT number is on it) are **cleared** — ZATCA approves it first
     and stamps its own QR code on it.
4. prints it on the member's **receipt** (the link in the payment email): English and Arabic side by side,
   with the QR code.

Refunds get a **credit note** automatically. Nobody needs to press anything for normal days.

Top-ups of locker credit are not invoiced when bought; what the credit buys is invoiced when it is spent.
(Locker credit is switched off for now — see `LOCKER_CREDIT_ENABLED`.)

---

## 2. What the statuses mean

| Status on the invoice list | Meaning | What to do |
|---|---|---|
| **Waiting to be sent** | Signed, not yet accepted by ZATCA (ZATCA was busy, the internet dropped…). | Nothing. It is retried automatically: after 5 min, 15 min, 1 h, 3 h, 6 h, 12 h, then every 12 hours. |
| **Waiting — late** (red "late") | Still not sent after 12 hours. ZATCA expects simplified invoices within 24 hours. | Open it and press **Send now**. If it keeps failing, read *Last error* and see section 6. |
| **Reported** | ZATCA accepted a normal (simplified) invoice. | Nothing. |
| **Cleared** | ZATCA approved a company (B2B) invoice and stamped it. | Nothing. Give the customer the printed copy (it carries ZATCA's QR). |
| **Rejected** | ZATCA refused it — usually wrong seller or buyer details. | See section 6. A rejected invoice stays in the list (the numbering never has gaps). |
| **warnings** (yellow) | Accepted, but ZATCA flagged something to improve. | Read the warnings; usually a seller detail to fix in **Business details**. |

A company (B2B) invoice shows **"Pending clearance"** on its receipt until ZATCA clears it — it is final only
after that.

---

## 3. The environments

| Environment | Used for | Legal invoices? |
|---|---|---|
| **Sandbox** | Trying the system. ZATCA's test VAT number, fixed OTP `123345`. | No — receipts say **TEST / تجريبي** |
| **Simulation** | A full rehearsal with the gym's real VAT number on ZATCA's simulation portal. | No — receipts say **TEST / تجريبي** |
| **Production** | The real thing. | **Yes** |

Switching environment = going live again with a new device (section 4). Nothing in the code changes.

---

## 4. Going live, and renewing the certificate

The gym's "device" (the system's identity at ZATCA) has a **certificate that expires** (usually after about a
year). The ZATCA page shows the expiry date, and from **30 days before** it shows a warning:
*"The device certificate expires in N days. Go live again with a new OTP before then."*

To go live — or to **renew** before the certificate expires:

1. Log in to the **Fatoora portal** (fatoora.zatca.gov.sa) with the gym's account and create an **OTP**
   (valid for about one hour). For Simulation, use the simulation portal.
2. In the admin portal: **ZATCA → Go live** → **Choose where to go live** → **Create device**.
3. Enter the **OTP** → **Get compliance certificate**.
4. Press **Run compliance checks**. The system sends ZATCA sample invoices, credit notes and debit notes
   (and company ones if the device does B2B). All must pass.
5. Press **Get production certificate**.
6. Press **Activate this device**. From this moment the new device issues every invoice; the old one is archived
   (its invoices stay valid and anything still waiting is still sent).

Before step 2, check **Business details**: seller name (Arabic is required), VAT number (15 digits),
CR number, the full national address (building number 4 digits, postal code 5 digits). While a
Simulation/Production device is live the VAT and CR numbers are locked, because the certificate carries
them — to change them, go live again.

**Renew at least a week early.** If the certificate expires, ZATCA refuses new invoices until a new device is
live; they wait in "Waiting to be sent" and go out once you have renewed.

---

## 5. Correcting an invoice

* **Customer paid too much / refund** → refund the payment normally (Finance → payments). A **credit note**
  is issued and reported automatically.
* **Customer paid too little** → open the invoice (ZATCA → Invoices → click it) → **Undercharged? Issue a
  debit note**:
  * **Extra amount incl. VAT** — only the difference, VAT included;
  * **Reason** — goes to ZATCA (e.g. "Session ran 30 minutes over");
  * **Extra money taken at the desk** — choose *Cash* or *Card terminal* if you collected it now, so it also
    appears in the member's payments and the finance numbers; or *Not yet*.
  The debit note gets its own number (`…-DBN-…`), is linked to the original invoice, signed and reported
  (or cleared for a company). Press **Print** to give the customer a copy.
* Never edit or delete an invoice: corrections are always a credit or debit note.

---

## 6. When something goes wrong

| You see | Likely cause | Fix |
|---|---|---|
| Many invoices **late**, *Last error* says the connection failed / timed out | ZATCA or the internet was down | Wait, or press **Send waiting now** on the Overview. Make sure the retry job runs (section 7). |
| *Last error* mentions the **certificate** / 401 | Certificate expired or revoked | Renew (section 4). |
| **Rejected**, errors about the seller (VAT number, address, CR) | Business details don't match ZATCA's records | Fix **Business details**. If the VAT/CR number is wrong, go live again. Then issue the corrected invoice: refund the rejected sale (credit note) and charge again, or ask your accountant. |
| **Rejected**, errors about the buyer (company invoices) | The company's VAT number or address is wrong | Fix the company (ZATCA → Companies) and issue a new invoice; the member can also correct their saved company. |
| "ZATCA e-invoicing is off" | The switch on the Overview is off | Turn it on (only possible with an active device). |

Every action (settings changed, device activated, documents rejected, debit notes issued) is in
**ZATCA → Activity**, with who did it and when.

Download the **signed XML** of any invoice from its drawer if the accountant or ZATCA asks for it; company
invoices also have **ZATCA's cleared copy**.

---

## 7. For the developer / hosting (once)

* Environment variables on the backend:
  * `ZATCA_ENCRYPTION_KEY` — encrypts the device's private key and ZATCA tokens. **Back it up**; without it the
    device can't sign and you must go live again. Never change it while a device is live.
  * `ZATCA_CRON_SECRET` — protects the retry endpoint.
* A scheduler must call the retry job **every 5 minutes**:
  `POST https://<backend>/api/admin/zatca/cron/` with header `X-Cron-Secret: <ZATCA_CRON_SECRET>`
  (or run `python manage.py zatca_retry_queue` from cron / Task Scheduler).
* Receipt details printed for the gym (logo, Arabic address) are set in **ZATCA → Business details**.
* Database migrations are run by the owner before each backend deploy.
