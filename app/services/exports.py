"""CSV / Excel / PDF export of any report dict produced by services.reports."""
import csv
import io
import os
from decimal import Decimal

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4, landscape
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

from ..money import fmt_inr, fmt_pct, fmt_qty, plain_money


def display(kind, v):
    if v is None or v == "":
        return ""
    if kind == "money":
        return fmt_inr(v)
    if kind == "qty":
        return fmt_qty(v)
    if kind == "dec":
        return f"{Decimal(v):,.2f}"
    if kind == "pct":
        return fmt_pct(v)
    return str(v)


def plain(kind, v):
    if v is None or v == "":
        return ""
    if kind == "money":
        return plain_money(v)
    if kind == "qty":
        return fmt_qty(v)
    if kind in ("dec", "pct"):
        return f"{Decimal(v):.2f}"
    return str(v)


def excel_value(kind, v):
    if v is None or v == "":
        return None
    if kind == "money":
        return Decimal(v) / 100
    if kind == "qty":
        return Decimal(v) / 1000
    if kind in ("dec", "pct"):
        return Decimal(v)
    if kind == "int":
        return int(v)
    return str(v)


def _all_rows(rep):
    return rep["rows"] + ([rep["totals"]] if rep["totals"] else [])


def to_csv(rep, start=None, end=None) -> bytes:
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow([rep["title"]] + ([f"{start} to {end}"] if start else []))
    w.writerow([h for h, _ in rep["columns"]])
    for r in _all_rows(rep):
        w.writerow([plain(k, v) for (_, k), v in zip(rep["columns"], r)])
    return ("﻿" + out.getvalue()).encode("utf-8")      # BOM so Excel opens ₹ / unicode correctly


def to_xlsx(rep, start=None, end=None) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = rep["slug"][:31]
    ws.append([rep["title"]])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append([f"Period: {start} to {end}" if start else ""])
    ws.append([])
    ws.append([h for h, _ in rep["columns"]])
    for c in ws[4]:
        c.font = Font(bold=True, color="FFFFFF")
        c.fill = PatternFill("solid", fgColor="1F4E5F")
        c.alignment = Alignment(wrap_text=True, vertical="center")
    for r in rep["rows"]:
        ws.append([excel_value(k, v) for (_, k), v in zip(rep["columns"], r)])
    if rep["totals"]:
        ws.append([excel_value(k, v) for (_, k), v in zip(rep["columns"], rep["totals"])])
        for c in ws[ws.max_row]:
            c.font = Font(bold=True)
    for j, (_, k) in enumerate(rep["columns"], start=1):
        for row in ws.iter_rows(min_row=5, min_col=j, max_col=j):
            for c in row:
                if k in ("money", "dec"):
                    c.number_format = '#,##0.00'
                elif k == "qty":
                    c.number_format = '#,##0.###'
                elif k == "pct":
                    c.number_format = '0.00"%"'
        width = max([len(str(rep["columns"][j - 1][0]))] + [len(display(k, r[j - 1])) for r in _all_rows(rep)] + [8])
        ws.column_dimensions[get_column_letter(j)].width = min(width + 2, 45)
    ws.freeze_panes = "A5"
    if rep.get("note"):
        ws.append([])
        ws.append([rep["note"]])
    bio = io.BytesIO()
    wb.save(bio)
    return bio.getvalue()


_PDF_MAP = {"₹": "Rs.", "−": "-", "–": "-", "→": "->", "≤": "<=", "≥": ">=", "÷": "/", "×": "x"}


def pdf_safe(s) -> str:
    s = str(s)
    for a, b in _PDF_MAP.items():
        s = s.replace(a, b)
    return s.encode("latin-1", "replace").decode("latin-1")


def to_pdf(rep, start=None, end=None, company="") -> bytes:
    bio = io.BytesIO()
    doc = SimpleDocTemplate(bio, pagesize=landscape(A4), leftMargin=10 * mm, rightMargin=10 * mm, topMargin=12 * mm,
                            bottomMargin=12 * mm, title=pdf_safe(rep["title"]))
    ss = getSampleStyleSheet()
    cell = ParagraphStyle("cell", parent=ss["BodyText"], fontSize=7.5, leading=9)
    cellr = ParagraphStyle("cellr", parent=cell, alignment=2)
    head = ParagraphStyle("head", parent=cell, textColor=colors.white, fontName="Helvetica-Bold")
    els = [Paragraph(pdf_safe(company), ss["Heading4"]), Paragraph(pdf_safe(rep["title"]), ss["Title"])]
    if start:
        els.append(Paragraph(f"Period: {start} to {end}", ss["Normal"]))
    els.append(Spacer(1, 5 * mm))
    data = [[Paragraph(pdf_safe(h), head) for h, _ in rep["columns"]]]
    numeric = {"money", "qty", "dec", "pct", "int"}
    for r in _all_rows(rep):
        data.append([Paragraph(pdf_safe(display(k, v).replace("<", "&lt;")), cellr if k in numeric else cell)
                     for (_, k), v in zip(rep["columns"], r)])
    t = Table(data, repeatRows=1)
    style = [("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1F4E5F")),
             ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#B8C4CA")),
             ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
             ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#F3F6F7")])]
    if rep["totals"]:
        style += [("BACKGROUND", (0, -1), (-1, -1), colors.HexColor("#DDE7EA"))]
    t.setStyle(TableStyle(style))
    els.append(t)
    if not rep["rows"]:
        els.append(Spacer(1, 4 * mm))
        els.append(Paragraph("No records in this period.", ss["Italic"]))
    if rep.get("note"):
        els += [Spacer(1, 4 * mm), Paragraph(pdf_safe(rep["note"]), ss["Italic"])]
    doc.build(els)
    return bio.getvalue()


def export_all(conn, outdir, start=None, end=None):
    """Write every report as CSV + XLSX + PDF (used by `manage.py benchmark --export`)."""
    from . import reports, sales
    os.makedirs(outdir, exist_ok=True)
    company = sales.seller(conn)["company_name"]
    n = 0
    for slug, _, _, _ in reports.REPORTS:
        rep = reports.run_report(conn, slug, start, end)
        s, e = (start, end) if start else reports.default_range()
        for ext, data in (("csv", to_csv(rep, s, e)), ("xlsx", to_xlsx(rep, s, e)), ("pdf", to_pdf(rep, s, e, company))):
            with open(os.path.join(outdir, f"{slug}.{ext}"), "wb") as f:
                f.write(data)
            n += 1
    return n
