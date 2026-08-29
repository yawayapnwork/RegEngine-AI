"""Appends a "Digital Signature & Certification" page to the executive
summary PDF (app.analytics.pdf_report.render_executive_summary), rather
than modifying that module to know about signing -- the analytics PDF
renderer has no business knowing about RSA keys or the audit-binder
manifest concept; this module composes the two via `pypdf`, keeping each
concern in its own place.
"""
from __future__ import annotations

import io

from pypdf import PdfReader, PdfWriter
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import cm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.reporting.signing import DigitalSignature

_MARGIN = 2 * cm


def _build_signature_page_pdf(signature: DigitalSignature, manifest_filename: str = "manifest.json") -> bytes:
    buffer = io.BytesIO()
    doc = SimpleDocTemplate(buffer, pagesize=A4, leftMargin=_MARGIN, rightMargin=_MARGIN, topMargin=_MARGIN, bottomMargin=_MARGIN)
    base_styles = getSampleStyleSheet()
    title_style = ParagraphStyle("SigTitle", parent=base_styles["Heading1"], spaceAfter=12)
    body_style = ParagraphStyle("SigBody", parent=base_styles["BodyText"], fontSize=9, leading=13)
    mono_style = ParagraphStyle("SigMono", parent=base_styles["Code"], fontSize=7, leading=10, wordWrap="CJK")

    story = [
        Paragraph("Digital Signature &amp; Certification", title_style),
        Paragraph(
            "This audit binder package is cryptographically signed to certify its integrity as issued. "
            "The signature below covers the SHA-256 digest of every file listed in "
            f"<b>{manifest_filename}</b> (bundled in this ZIP package). Any modification to any file in this "
            "package after issuance will cause signature verification to FAIL.",
            body_style,
        ),
        Spacer(1, 0.4 * cm),
        Table(
            [
                ["Signing Algorithm", signature.algorithm],
                ["Signed By", signature.signer_id],
                ["Signed At (UTC)", signature.signed_at.isoformat()],
                ["Manifest SHA-256", signature.manifest_sha256],
            ],
            colWidths=[4.5 * cm, 12 * cm],
            style=TableStyle(
                [
                    ("FONTSIZE", (0, 0), (-1, -1), 9),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
                    ("FONTNAME", (0, 0), (0, -1), "Helvetica-Bold"),
                ]
            ),
        ),
        Spacer(1, 0.4 * cm),
        Paragraph("RSA Signature (base64, PSS padding, SHA-256):", body_style),
        Paragraph(signature.signature_b64, mono_style),
        Spacer(1, 0.4 * cm),
        Paragraph("Public Key (PEM), for independent verification:", body_style),
        Paragraph((signature.public_key_pem or "").replace("\n", "<br/>"), mono_style),
        Spacer(1, 0.4 * cm),
        Paragraph(
            "Verification: recompute SHA-256 over manifest.json's exact bytes, then verify the signature above "
            "against that digest using the embedded public key (RSA-PSS, MGF1-SHA256, SHA256 digest, max salt "
            "length) -- see app.reporting.signing.verify_signature for a reference implementation.",
            body_style,
        ),
    ]
    doc.build(story)
    return buffer.getvalue()


def append_signature_page(base_pdf_bytes: bytes, signature: DigitalSignature, manifest_filename: str = "manifest.json") -> bytes:
    signature_page_bytes = _build_signature_page_pdf(signature, manifest_filename)

    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(base_pdf_bytes)).pages:
        writer.add_page(page)
    for page in PdfReader(io.BytesIO(signature_page_bytes)).pages:
        writer.add_page(page)

    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()
