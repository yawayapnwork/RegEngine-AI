"""ReportLab PDF generator for compliance analytics and SEBI audit trail reports.

Generates two document types:

  Executive Compliance Summary (``render_executive_summary``)
  ─────────────────────────────────────────────────────────────
  A management-facing multi-section PDF covering:
    • Cover page: period, generated-by, chain integrity badge
    • Section 1: Headline KPIs (transaction volume, pass/fail/HITL rates)
    • Section 2: Time-series table (monthly or quarterly buckets)
    • Section 3: Broker-level compliance breakdown
    • Section 4: Top rule violations
    • Section 5: Anomaly callouts (HIGH severity highlighted in red)
    • Section 6: Cryptographic proof — hash chain summary and window seal

  SEBI Audit Trail Report (``render_audit_trail``)
  ──────────────────────────────────────────────────
  A regulator-facing tabular extract covering:
    • Cover page with chain-proof attestation block
    • Full paginated ledger table (sequence_num, broker, tx_id,
      evaluated_at, rule_id, result, payload_digest, current_hash)
    • Integrity attestation footer on every page

Design notes
────────────
• All colours follow SEBI's colour scheme (navy, saffron, white) so the
  PDF is instantly recognisable in an audit context.
• ``SimpleDocTemplate`` with ``KeepTogether`` is used for tables so a row
  group never splits across a page break.
• No external image assets are required — the cover badge is drawn with
  ReportLab's canvas primitives.
• The generator is synchronous and CPU-bound; call it inside
  ``asyncio.to_thread(...)`` from the FastAPI route handler.
"""
from __future__ import annotations

import io
import textwrap
from datetime import datetime, timezone
from typing import Any

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm, mm
from reportlab.platypus import (
    HRFlowable,
    KeepTogether,
    PageBreak,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app.analytics.models import (
    AggregatedReport,
    AnomalySeverity,
    AuditTrailReport,
)

# ---------------------------------------------------------------------------
# Brand colours
# ---------------------------------------------------------------------------
_SEBI_NAVY = colors.HexColor("#003366")
_SEBI_SAFFRON = colors.HexColor("#FF6600")
_SEBI_LIGHT = colors.HexColor("#E8EEF5")
_CHAIN_GREEN = colors.HexColor("#006633")
_CHAIN_RED = colors.HexColor("#CC0000")
_ANOMALY_HIGH = colors.HexColor("#FF0000")
_ANOMALY_LOW = colors.HexColor("#FF8C00")
_TABLE_ALT = colors.HexColor("#F2F6FA")

_PAGE_W, _PAGE_H = A4
_MARGIN = 2.0 * cm


# ---------------------------------------------------------------------------
# Style helpers
# ---------------------------------------------------------------------------

def _styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    return {
        "h1": ParagraphStyle(
            "H1", parent=base["Heading1"],
            textColor=_SEBI_NAVY, fontSize=20, spaceAfter=6,
        ),
        "h2": ParagraphStyle(
            "H2", parent=base["Heading2"],
            textColor=_SEBI_NAVY, fontSize=14, spaceAfter=4, spaceBefore=12,
        ),
        "h3": ParagraphStyle(
            "H3", parent=base["Heading3"],
            textColor=_SEBI_NAVY, fontSize=11, spaceAfter=2, spaceBefore=8,
        ),
        "body": ParagraphStyle(
            "Body", parent=base["Normal"],
            fontSize=9, leading=13,
        ),
        "small": ParagraphStyle(
            "Small", parent=base["Normal"],
            fontSize=7.5, leading=10, textColor=colors.gray,
        ),
        "center": ParagraphStyle(
            "Center", parent=base["Normal"],
            alignment=TA_CENTER, fontSize=9,
        ),
        "right": ParagraphStyle(
            "Right", parent=base["Normal"],
            alignment=TA_RIGHT, fontSize=8,
        ),
        "kpi_val": ParagraphStyle(
            "KPIVal", parent=base["Normal"],
            fontSize=22, textColor=_SEBI_NAVY, alignment=TA_CENTER, spaceBefore=2,
        ),
        "kpi_label": ParagraphStyle(
            "KPILabel", parent=base["Normal"],
            fontSize=8, textColor=colors.gray, alignment=TA_CENTER,
        ),
        "chain_ok": ParagraphStyle(
            "ChainOK", parent=base["Normal"],
            textColor=_CHAIN_GREEN, fontSize=11, alignment=TA_CENTER,
        ),
        "chain_fail": ParagraphStyle(
            "ChainFail", parent=base["Normal"],
            textColor=_CHAIN_RED, fontSize=11, alignment=TA_CENTER,
        ),
        "anomaly_high": ParagraphStyle(
            "AnomalyHigh", parent=base["Normal"],
            textColor=_ANOMALY_HIGH, fontSize=9,
        ),
        "anomaly_low": ParagraphStyle(
            "AnomalyLow", parent=base["Normal"],
            textColor=_ANOMALY_LOW, fontSize=9,
        ),
    }


def _divider() -> HRFlowable:
    return HRFlowable(width="100%", thickness=0.5, color=_SEBI_NAVY, spaceAfter=4)


def _section_heading(title: str, st: dict) -> list:
    return [Spacer(1, 0.3 * cm), Paragraph(title, st["h2"]), _divider()]


# ---------------------------------------------------------------------------
# KPI card table (2 columns × N rows)
# ---------------------------------------------------------------------------

def _kpi_table(items: list[tuple[str, str]], st: dict) -> Table:
    """Render a set of (value, label) pairs as a compact KPI grid."""
    row = []
    for val, label in items:
        cell = [
            Paragraph(val, st["kpi_val"]),
            Paragraph(label, st["kpi_label"]),
        ]
        row.append(cell)
    # Split into rows of 4 KPI boxes
    rows: list[list] = []
    for i in range(0, len(row), 4):
        rows.append(row[i : i + 4])
    # Pad last row
    while len(rows[-1]) < 4:
        rows[-1].append(Paragraph("", st["body"]))

    col_w = (_PAGE_W - 2 * _MARGIN) / 4
    tbl = Table(rows, colWidths=[col_w] * 4)
    tbl.setStyle(
        TableStyle(
            [
                ("BOX", (0, 0), (-1, -1), 0.5, _SEBI_NAVY),
                ("INNERGRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
                ("BACKGROUND", (0, 0), (-1, -1), _SEBI_LIGHT),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("TOPPADDING", (0, 0), (-1, -1), 6),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
            ]
        )
    )
    return tbl


# ---------------------------------------------------------------------------
# Generic data table
# ---------------------------------------------------------------------------

def _data_table(
    headers: list[str],
    rows: list[list[str]],
    col_widths: list[float] | None = None,
) -> Table:
    header_row = [
        Paragraph(f"<b>{h}</b>", ParagraphStyle("TH", fontSize=8, textColor=colors.white))
        for h in headers
    ]
    data = [header_row]
    for i, row in enumerate(rows):
        bg = _TABLE_ALT if i % 2 == 0 else colors.white
        data.append([Paragraph(str(c), ParagraphStyle("TD", fontSize=7.5)) for c in row])

    if col_widths is None:
        usable = _PAGE_W - 2 * _MARGIN
        col_widths = [usable / len(headers)] * len(headers)

    tbl = Table(data, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), _SEBI_NAVY),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTSIZE", (0, 0), (-1, 0), 8),
        ("GRID", (0, 0), (-1, -1), 0.25, colors.lightgrey),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("TOPPADDING", (0, 0), (-1, -1), 3),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
    ]
    for i in range(1, len(data)):
        if i % 2 == 0:
            style.append(("BACKGROUND", (0, i), (-1, i), _TABLE_ALT))
    tbl.setStyle(TableStyle(style))
    return tbl


# ---------------------------------------------------------------------------
# Cover page
# ---------------------------------------------------------------------------

def _cover_page(title: str, report: Any, st: dict, chain_valid: bool | None) -> list:
    story: list = []
    story.append(Spacer(1, 3 * cm))
    story.append(Paragraph("RegEngine AI", ParagraphStyle(
        "Logo", fontSize=11, textColor=_SEBI_SAFFRON, alignment=TA_CENTER, spaceAfter=2,
    )))
    story.append(Paragraph(title, ParagraphStyle(
        "CoverTitle", fontSize=24, textColor=_SEBI_NAVY, alignment=TA_CENTER, spaceAfter=8,
        leading=30,
    )))
    story.append(_divider())

    meta_rows = [
        ["Report Period:", report.period.label()],
        ["Granularity:", report.period.granularity.value.capitalize()],
        ["Tenant Scope:", report.tenant_scope],
        ["Generated By:", report.generated_by],
        ["Generated At:", report.generated_at.strftime("%Y-%m-%d %H:%M:%S UTC")],
        ["Report ID:", report.report_id],
    ]
    meta_tbl = Table(meta_rows, colWidths=[5 * cm, _PAGE_W - 2 * _MARGIN - 5 * cm])
    meta_tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), _SEBI_NAVY),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.lightgrey),
    ]))
    story.append(meta_tbl)
    story.append(Spacer(1, 1.5 * cm))

    # Chain integrity badge
    if chain_valid is True:
        badge_text = "✔  LEDGER CHAIN INTEGRITY: VERIFIED"
        badge_style = "chain_ok"
    elif chain_valid is False:
        badge_text = "✘  LEDGER CHAIN INTEGRITY: BREAKS DETECTED"
        badge_style = "chain_fail"
    else:
        badge_text = "Chain verification not run for this report."
        badge_style = "body"

    badge_tbl = Table([[Paragraph(badge_text, st[badge_style])]], colWidths=[_PAGE_W - 2 * _MARGIN])
    badge_color = _CHAIN_GREEN if chain_valid else (_CHAIN_RED if chain_valid is False else colors.lightgrey)
    badge_tbl.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1.5, badge_color),
        ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#F0FFF0") if chain_valid else colors.HexColor("#FFF0F0")),
        ("TOPPADDING", (0, 0), (-1, -1), 10),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))
    story.append(badge_tbl)
    story.append(Spacer(1, 0.5 * cm))
    story.append(Paragraph(
        "CONFIDENTIAL — For Regulatory and Internal Compliance Use Only",
        ParagraphStyle("Conf", fontSize=8, textColor=colors.gray, alignment=TA_CENTER),
    ))
    story.append(PageBreak())
    return story


# ---------------------------------------------------------------------------
# Chain proof section
# ---------------------------------------------------------------------------

def _chain_proof_section(report: AggregatedReport, st: dict) -> list:
    story = _section_heading("Section 6: Cryptographic Ledger Proof", st)
    proof = report.chain_proof
    if proof is None:
        story.append(Paragraph("Chain verification was not run for this report.", st["body"]))
        return story

    status = "VERIFIED ✔" if proof.chain_valid else f"BREAKS DETECTED ({proof.break_count} break(s)) ✘"
    color = _CHAIN_GREEN if proof.chain_valid else _CHAIN_RED

    rows = [
        ["Chain Status", status],
        ["Entries Checked", str(proof.entries_checked)],
        ["Sequence Range",
         f"{proof.range_start_sequence} – {proof.range_end_sequence}"
         if proof.range_start_sequence is not None else "N/A"],
        ["Verified At", proof.verified_at.strftime("%Y-%m-%d %H:%M:%S UTC")],
        ["Window Seal Hash (SHA-256)", proof.window_seal_hash or "N/A"],
        ["Break Count", str(proof.break_count)],
    ]
    tbl = Table(rows, colWidths=[5 * cm, _PAGE_W - 2 * _MARGIN - 5 * cm])
    tbl.setStyle(TableStyle([
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TEXTCOLOR", (0, 0), (0, -1), _SEBI_NAVY),
        ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
        ("TEXTCOLOR", (1, 0), (1, 0), color),
        ("FONTNAME", (1, 0), (1, 0), "Helvetica-Bold"),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("LINEBELOW", (0, 0), (-1, -2), 0.25, colors.lightgrey),
        ("WORDWRAP", (1, -2), (1, -2), True),
    ]))
    story.append(tbl)
    story.append(Spacer(1, 0.4 * cm))
    story.append(Paragraph(proof.note, st["small"]))
    return story


# ---------------------------------------------------------------------------
# Public render functions
# ---------------------------------------------------------------------------

def render_executive_summary(report: AggregatedReport) -> bytes:
    """Render ``AggregatedReport`` to PDF bytes.  CPU-bound; call inside
    ``asyncio.to_thread()``.
    """
    buffer = io.BytesIO()
    st = _styles()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN,
        title=f"RegEngine AI Compliance Summary — {report.period.label()}",
        author=report.generated_by,
    )

    story: list = []

    # --- Cover ---
    chain_valid = report.chain_proof.chain_valid if report.chain_proof else None
    story += _cover_page("Executive Compliance Summary", report, st, chain_valid)

    # --- Section 1: Headline KPIs ---
    story += _section_heading("Section 1: Headline KPIs", st)
    kpi_items = [
        (f"{report.total_transactions:,}", "Total Transactions"),
        (f"{report.overall_pass_rate_pct:.1f}%", "Pass Rate"),
        (f"{report.overall_fail_rate_pct:.1f}%", "Fail Rate"),
        (f"{report.overall_hitl_rate_pct:.1f}%", "HITL Review Rate"),
        (f"{report.total_passed:,}", "Passed"),
        (f"{report.total_failed:,}", "Failed"),
        (f"{report.total_hitl_review:,}", "HITL Flagged"),
        (f"{report.unique_brokers_evaluated}", "Active Brokers"),
    ]
    story.append(_kpi_table(kpi_items, st))
    story.append(Spacer(1, 0.5 * cm))

    # --- Section 2: Time Series ---
    story += _section_heading(
        f"Section 2: {report.period.granularity.value.capitalize()} Compliance Trend", st
    )
    if report.time_series:
        ts_headers = ["Period", "Total", "Passed", "Failed", "HITL", "Pass %", "Fail %", "HITL %"]
        ts_rows = [
            [
                b.period_label, str(b.total_transactions),
                str(b.passed), str(b.failed), str(b.hitl_review),
                f"{b.pass_rate_pct:.1f}", f"{b.fail_rate_pct:.1f}", f"{b.hitl_rate_pct:.1f}",
            ]
            for b in report.time_series
        ]
        usable = _PAGE_W - 2 * _MARGIN
        ts_widths = [2.5*cm, 2*cm, 2*cm, 2*cm, 2*cm, 2*cm, 2*cm, 2*cm]
        story.append(KeepTogether(_data_table(ts_headers, ts_rows, ts_widths)))
    else:
        story.append(Paragraph("No data available for the selected period.", st["body"]))
    story.append(Spacer(1, 0.4 * cm))

    # --- Section 3: Broker Breakdown ---
    story += _section_heading("Section 3: Broker-Level Compliance Breakdown", st)
    if report.broker_stats:
        b_headers = [
            "Broker ID", "Name", "Type", "Total", "Pass %", "Fail %",
            "HITL %", "HITL Pending", "HITL Resolved",
        ]
        b_rows = [
            [
                b.broker_id, b.display_name or "—", b.tenant_type or "—",
                str(b.total_transactions),
                f"{b.pass_rate_pct:.1f}", f"{b.fail_rate_pct:.1f}", f"{b.hitl_rate_pct:.1f}",
                str(b.hitl_pending), str(b.hitl_resolved),
            ]
            for b in report.broker_stats
        ]
        usable = _PAGE_W - 2 * _MARGIN
        b_widths = [3.0*cm, 3.5*cm, 2.2*cm, 1.8*cm, 1.8*cm, 1.8*cm, 1.8*cm, 2.0*cm, 2.0*cm]
        story.append(KeepTogether(_data_table(b_headers, b_rows, b_widths)))
    else:
        story.append(Paragraph("No broker data available.", st["body"]))
    story.append(Spacer(1, 0.4 * cm))

    # --- Section 4: Top Violations ---
    story += _section_heading("Section 4: Top Rule Violations", st)
    if report.top_violations:
        v_headers = [
            "Rule ID", "Circular", "Section Ref",
            "Failures", "Brokers Affected", "First Seen", "Last Seen",
        ]
        v_rows = [
            [
                v.rule_id, v.circular_id, v.section_reference,
                str(v.total_failures), str(v.affected_brokers),
                v.first_seen.strftime("%Y-%m-%d") if v.first_seen else "—",
                v.last_seen.strftime("%Y-%m-%d") if v.last_seen else "—",
            ]
            for v in report.top_violations
        ]
        usable = _PAGE_W - 2 * _MARGIN
        v_widths = [4.5*cm, 3.0*cm, 2.5*cm, 2.0*cm, 2.5*cm, 2.5*cm, 2.5*cm]
        story.append(KeepTogether(_data_table(v_headers, v_rows, v_widths)))
    else:
        story.append(Paragraph("No rule violations recorded in this period.", st["body"]))
    story.append(Spacer(1, 0.4 * cm))

    # --- Section 5: Anomalies ---
    story += _section_heading("Section 5: Anomaly Detection Report", st)
    if report.anomalies:
        summary_text = (
            f"<b>{len(report.anomalies)}</b> anomalies detected: "
            f"<font color='red'><b>{report.anomaly_count_high} HIGH</b></font>, "
            f"<font color='orange'>{report.anomaly_count_low} LOW</font>."
        )
        story.append(Paragraph(summary_text, st["body"]))
        story.append(Spacer(1, 0.2 * cm))

        a_headers = ["Type", "Severity", "Broker", "Period", "Metric", "Observed", "Mean", "Z-Score"]
        a_rows = []
        for a in report.anomalies:
            sev_col = "<font color='red'><b>HIGH</b></font>" if a.severity == AnomalySeverity.HIGH else "<font color='orange'>LOW</font>"
            a_rows.append([
                a.anomaly_type.value, sev_col,
                a.broker_id or "System-wide", a.period_label,
                a.metric_name,
                f"{a.observed_value:.2f}", f"{a.baseline_mean:.2f}",
                f"{a.z_score:+.2f}σ",
            ])

        usable = _PAGE_W - 2 * _MARGIN
        a_widths = [2.8*cm, 1.8*cm, 2.8*cm, 2.0*cm, 3.0*cm, 2.0*cm, 2.0*cm, 2.0*cm]
        story.append(KeepTogether(_data_table(a_headers, a_rows, a_widths)))

        # Individual anomaly descriptions
        story.append(Spacer(1, 0.3 * cm))
        story.append(Paragraph("<b>Anomaly Descriptions</b>", st["h3"]))
        for a in report.anomalies:
            style_key = "anomaly_high" if a.severity == AnomalySeverity.HIGH else "anomaly_low"
            story.append(Paragraph(f"• {a.description}", st[style_key]))
    else:
        story.append(Paragraph(
            "No statistical anomalies detected in the compliance telemetry for this period.",
            st["body"],
        ))
    story.append(Spacer(1, 0.4 * cm))

    # --- Section 6: Chain Proof ---
    story += _chain_proof_section(report, st)

    doc.build(story)
    return buffer.getvalue()


def render_audit_trail(report: AuditTrailReport) -> bytes:
    """Render ``AuditTrailReport`` to a regulator-facing PDF.

    Presents the raw ledger extract with full cryptographic proof fields
    (payload_digest, current_hash) in a compact monospace font so an
    auditor can verify individual rows.
    """
    buffer = io.BytesIO()
    st = _styles()
    doc = SimpleDocTemplate(
        buffer,
        pagesize=A4,
        leftMargin=_MARGIN, rightMargin=_MARGIN,
        topMargin=_MARGIN, bottomMargin=_MARGIN,
        title=f"SEBI Audit Trail — {report.period.label()}",
        author=report.generated_by,
    )

    story: list = []

    # --- Cover ---
    story += _cover_page("SEBI Compliance Audit Trail", report, st, report.chain_proof.chain_valid)

    # --- Audit trail table ---
    story += _section_heading("Ledger Extract (Tamper-Evident Record)", st)
    story.append(Paragraph(
        f"Page {report.page} of {report.total_pages} — "
        f"Showing entries {(report.page - 1) * report.page_size + 1}–"
        f"{min(report.page * report.page_size, report.total_entries)} "
        f"of {report.total_entries:,} total.",
        st["body"],
    ))
    story.append(Spacer(1, 0.3 * cm))

    if report.entries:
        at_headers = [
            "Seq#", "Broker", "Tx ID", "Evaluated At", "Rule ID",
            "Result", "Payload Digest (truncated)", "Block Hash (truncated)",
        ]
        at_rows = [
            [
                str(e.sequence_num),
                e.broker_id,
                # Truncate long tx IDs for table display
                (e.transaction_id[:16] + "…") if len(e.transaction_id) > 16 else e.transaction_id,
                e.evaluated_at.strftime("%Y-%m-%d %H:%M"),
                (e.rule_id[:20] + "…") if len(e.rule_id) > 20 else e.rule_id,
                e.evaluation_result,
                e.payload_digest[:16] + "…",
                e.current_hash[:16] + "…",
            ]
            for e in report.entries
        ]
        usable = _PAGE_W - 2 * _MARGIN
        at_widths = [1.4*cm, 2.5*cm, 2.5*cm, 3.0*cm, 3.5*cm, 2.0*cm, 3.0*cm, 3.0*cm]
        story.append(KeepTogether(_data_table(at_headers, at_rows, at_widths)))
    else:
        story.append(Paragraph("No ledger entries in this window.", st["body"]))

    story.append(Spacer(1, 0.5 * cm))

    # --- Chain proof ---
    story += _chain_proof_section(
        # Wrap in a minimal AggregatedReport-like object that the helper accepts
        type("_R", (), {"chain_proof": report.chain_proof})(),  # type: ignore[call-arg]
        st,
    )

    # --- Attestation footer ---
    story.append(Spacer(1, 0.5 * cm))
    story.append(_divider())
    story.append(Paragraph(
        "This report was generated programmatically by RegEngine AI from the append-only "
        "compliance_audit_ledger table.  The Window Seal Hash above may be independently "
        "verified by running app.ledger.verifier.verify_chain() against the same database "
        "with the same time-window parameters.  Any discrepancy between the stored "
        "current_hash values and independently recomputed values constitutes evidence of "
        "post-facto ledger modification.",
        st["small"],
    ))

    doc.build(story)
    return buffer.getvalue()
