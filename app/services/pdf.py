"""GST tax-invoice PDF (ReportLab)."""
import io
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from ..money import fmt_inr, fmt_qty
from . import sales
from .exports import pdf_safe

_ONES = ["", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen",
         "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen"]
_TENS = ["", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety"]


def _below_1000(n):
    out = []
    if n >= 100:
        out.append(_ONES[n // 100] + " Hundred")
        n %= 100
    if n >= 20:
        out.append(_TENS[n // 10] + ((" " + _ONES[n % 10]) if n % 10 else ""))
    elif n:
        out.append(_ONES[n])
    return " ".join(out)


def amount_in_words(paise: int) -> str:
    """Indian numbering (lakh / crore) in words, e.g. 787500 -> 'Rupees Seven Thousand Eight Hundred Seventy Five Only'."""
    rupees, ps = divmod(int(paise), 100)
    parts = []
    for div, name in ((10_000_000, "Crore"), (100_000, "Lakh"), (1000, "Thousand")):
        q, rupees = divmod(rupees, div)
        if q:
            parts.append(f"{_below_1000(q)} {name}")
    if rupees:
        parts.append(_below_1000(rupees))
    words = " ".join(parts) or "Zero"
    s = f"Rupees {words}"
    if ps:
        s += f" and {_below_1000(ps)} Paise"
    return s + " Only"


def invoice_pdf(conn, invoice_id) -> bytes:
    inv = sales.get_invoice(conn, invoice_id)
    lines = sales.invoice_lines(conn, invoice_id)
    s = sales.seller(conn)
    intra = inv["tax_type"] == "INTRA"
    ss = getSampleStyleSheet()
    n = ParagraphStyle("n", parent=ss["BodyText"], fontSize=8.5, leading=11)
    b = ParagraphStyle("b", parent=n, fontName="Helvetica-Bold")
    small = ParagraphStyle("s", parent=n, fontSize=7.5, leading=9.5, textColor=colors.HexColor("#444444"))
    P = lambda t, st=n: Paragraph(pdf_safe(t), st)
    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=A4, leftMargin=12 * mm, rightMargin=12 * mm, topMargin=12 * mm, bottomMargin=12 * mm,
                            title=pdf_safe(f"Tax Invoice {inv['invoice_no']}"))
    els = [Paragraph(pdf_safe("TAX INVOICE" + ("  (CANCELLED)" if inv["status"] == "cancelled" else "")),
                     ParagraphStyle("t", parent=ss["Title"], fontSize=15, textColor=colors.HexColor("#1F4E5F"))),
           P(f"<b>{s['company_name']}</b>", ParagraphStyle("c", parent=n, fontSize=11, leading=14)),
           P(s["company_address"]), P(f"GSTIN: <b>{s['company_gstin']}</b> &nbsp; State: {s['company_state']} ({s['company_state_code']})"),
           P(f"Phone: {s['company_phone']} &nbsp; Email: {s['company_email']}"), Spacer(1, 4 * mm)]

    meta = Table([[P(f"<b>Invoice No:</b> {inv['invoice_no']}"), P(f"<b>Invoice Date:</b> {inv['invoice_date']}")],
                  [P(f"<b>Financial Year:</b> {inv['fy']}"), P(f"<b>Place of Supply:</b> {inv['place_of_supply']} ({inv['place_of_supply_code']})")]],
                 colWidths=[91 * mm, 91 * mm])
    meta.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey)]))
    bill = Table([[P("<b>Bill To</b>"), ],
                  [P(f"<b>{inv['customer_name']}</b><br/>{inv['customer_address'] or ''}<br/>"
                     f"GSTIN: {inv['customer_gstin'] or 'Unregistered (B2C)'} &nbsp; State: {inv['place_of_supply']} ({inv['place_of_supply_code']})")]],
                 colWidths=[182 * mm])
    bill.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EFF1"))]))
    els += [meta, Spacer(1, 2 * mm), bill, Spacer(1, 4 * mm)]

    hdr = ["#", "Description", "HSN", "Qty", "Rate", "Taxable"] + (["CGST %", "CGST", "SGST %", "SGST"] if intra else ["IGST %", "IGST"]) + ["Total"]
    rows = [[P(f"<b>{h}</b>", small) for h in hdr]]
    for i, l in enumerate(lines, 1):
        rate = f"{Decimal(l['gst_rate_bp']) / 100:.2f}" if not intra else f"{Decimal(l['gst_rate_bp']) / 200:.2f}"
        row = [str(i), l["description"], l["hsn"], fmt_qty(l["qty_m"]), fmt_inr(l["price_paise"], False), fmt_inr(l["taxable_paise"], False)]
        row += ([rate, fmt_inr(l["cgst_paise"], False), rate, fmt_inr(l["sgst_paise"], False)] if intra
                else [rate, fmt_inr(l["igst_paise"], False)])
        row.append(fmt_inr(l["total_paise"], False))
        rows.append([P(c, small) for c in row])
    tot = ["", "Total", "", "", "", fmt_inr(inv["taxable_paise"], False)] + (
        ["", fmt_inr(inv["cgst_paise"], False), "", fmt_inr(inv["sgst_paise"], False)] if intra else ["", fmt_inr(inv["igst_paise"], False)]) + [
        fmt_inr(inv["total_paise"], False)]
    rows.append([P(f"<b>{c}</b>", small) for c in tot])
    widths = [6, 42, 13, 11, 14, 19, 14, 17, 14, 17, 21] if intra else [7, 62, 16, 14, 18, 24, 14, 22, 28]
    scale = 182 / sum(widths)
    t = Table(rows, colWidths=[w * scale * mm for w in widths], repeatRows=1)
    t.setStyle(TableStyle([("GRID", (0, 0), (-1, -1), 0.4, colors.grey), ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#E8EFF1")),
                           ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#F3F6F7")), ("VALIGN", (0, 0), (-1, -1), "MIDDLE")]))
    els += [t, Spacer(1, 4 * mm)]

    summ = [[P("Taxable value"), P(fmt_inr(inv["taxable_paise"], False))]]
    if intra:
        summ += [[P("CGST"), P(fmt_inr(inv["cgst_paise"], False))], [P("SGST"), P(fmt_inr(inv["sgst_paise"], False))]]
    else:
        summ += [[P("IGST"), P(fmt_inr(inv["igst_paise"], False))]]
    summ += [[P("<b>Invoice total (Rs.)</b>"), P(f"<b>{fmt_inr(inv['total_paise'], False)}</b>")]]
    st = Table(summ, colWidths=[40 * mm, 35 * mm], hAlign="RIGHT")
    st.setStyle(TableStyle([("BOX", (0, 0), (-1, -1), 0.5, colors.grey), ("ALIGN", (1, 0), (1, -1), "RIGHT"),
                            ("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#E8EFF1"))]))
    els += [st, Spacer(1, 3 * mm), P(f"<b>Amount in words:</b> {amount_in_words(inv['total_paise'])}"), Spacer(1, 3 * mm),
            P(f"<b>Bank details:</b> {s['company_bank']}"), Spacer(1, 2 * mm),
            P("Supply type: " + ("Intra-state - CGST + SGST" if intra else "Inter-state - IGST"), small),
            P("Declaration: We declare that this invoice shows the actual price of the goods described and that all particulars are "
              "true and correct. Subject to Tiruchirappalli jurisdiction. This is a computer generated invoice.", small),
            Spacer(1, 12 * mm), P(f"For <b>{s['company_name']}</b> - Authorised Signatory", ParagraphStyle("r", parent=n, alignment=2))]
    if inv["status"] == "cancelled":
        els.insert(1, P(f"<font color='red'><b>CANCELLED: {inv['cancel_reason'] or ''}</b></font>"))
    doc.build(els)
    return bio.getvalue()
