"""
Export helpers for Nexus AI runs.

Formats: Word (.docx), PDF, HTML, Markdown, plain text, JSON, and a ZIP of everything.
Extra dependencies:  pip install python-docx reportlab
"""

import html as _html
import io
import json
import re
import textwrap
import zipfile
from datetime import datetime

FORMATS = {
    "Word (.docx)": ("docx", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    "PDF (.pdf)": ("pdf", "application/pdf"),
    "HTML (.html)": ("html", "text/html"),
    "Markdown (.md)": ("md", "text/markdown"),
    "Plain text (.txt)": ("txt", "text/plain"),
    "JSON data (.json)": ("json", "application/json"),
}

# --------------------------------------------------------------------------- #
# Markdown parsing (small, dependency-free)
# --------------------------------------------------------------------------- #
# Order matters: bold first, then code, then *italic*, then _italic_ (word-boundary guarded
# so snake_case is left alone).
_INLINE = re.compile(
    r"(\*\*[^*]+\*\*|__[^_]+__|`[^`]+`|\*[^*\n]+\*|(?<!\w)_[^_\n]+_(?!\w))"
)


def inline_tokens(text):
    """Split text into (text, style) tokens. style: '', 'b', 'i', 'c'."""
    out = []
    for part in _INLINE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**") and len(part) > 4:
            out.append((part[2:-2], "b"))
        elif part.startswith("__") and part.endswith("__") and len(part) > 4:
            out.append((part[2:-2], "b"))
        elif part.startswith("`") and part.endswith("`") and len(part) > 2:
            out.append((part[1:-1], "c"))
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            out.append((part[1:-1], "i"))
        elif part.startswith("_") and part.endswith("_") and len(part) > 2:
            out.append((part[1:-1], "i"))
        else:
            out.append((part, ""))
    return out


def plain_inline(text):
    return "".join(t for t, _ in inline_tokens(text))


def _is_block_start(s):
    return bool(
        s.startswith("```")
        or re.match(r"^#{1,6}\s", s)
        or re.match(r"^[-*+]\s+", s)
        or re.match(r"^\d+[.)]\s+", s)
        or s.startswith(">")
        or s.startswith("|")
        or re.match(r"^(-{3,}|\*{3,}|_{3,})$", s)
    )


def parse_blocks(md):
    """Return a list of blocks: ('h', level, text), ('p', text), ('ul', [..]), ('ol', [..]),
    ('table', rows), ('code', text), ('quote', text), ('hr',)."""
    lines = (md or "").splitlines()
    blocks, i = [], 0
    while i < len(lines):
        s = lines[i].strip()
        if not s:
            i += 1
            continue
        if s.startswith("```"):
            i += 1
            code = []
            while i < len(lines) and not lines[i].strip().startswith("```"):
                code.append(lines[i])
                i += 1
            i += 1
            blocks.append(("code", "\n".join(code)))
            continue
        m = re.match(r"^(#{1,6})\s+(.*)$", s)
        if m:
            blocks.append(("h", len(m.group(1)), m.group(2).strip()))
            i += 1
            continue
        if re.match(r"^(-{3,}|\*{3,}|_{3,})$", s):
            blocks.append(("hr",))
            i += 1
            continue
        if s.startswith("|") and i + 1 < len(lines):
            nxt = lines[i + 1].strip()
            if re.match(r"^\|?[\s:|-]+\|?$", nxt) and "-" in nxt:
                rows = []
                while i < len(lines) and lines[i].strip().startswith("|"):
                    rows.append([c.strip() for c in lines[i].strip().strip("|").split("|")])
                    i += 1
                rows = [rows[0]] + rows[2:]
                width = max(len(r) for r in rows)
                rows = [r + [""] * (width - len(r)) for r in rows]
                blocks.append(("table", rows))
                continue
        if s.startswith(">"):
            quote = []
            while i < len(lines) and lines[i].strip().startswith(">"):
                quote.append(lines[i].strip().lstrip(">").strip())
                i += 1
            blocks.append(("quote", " ".join(q for q in quote if q)))
            continue
        if re.match(r"^[-*+]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\s*[-*+]\s+", lines[i]):
                items.append(re.sub(r"^\s*[-*+]\s+", "", lines[i]).strip())
                i += 1
            blocks.append(("ul", items))
            continue
        if re.match(r"^\d+[.)]\s+", s):
            items = []
            while i < len(lines) and re.match(r"^\s*\d+[.)]\s+", lines[i]):
                items.append(re.sub(r"^\s*\d+[.)]\s+", "", lines[i]).strip())
                i += 1
            blocks.append(("ol", items))
            continue
        para = [s]
        i += 1
        while i < len(lines) and lines[i].strip() and not _is_block_start(lines[i].strip()):
            para.append(lines[i].strip())
            i += 1
        blocks.append(("p", " ".join(para)))
    # Re-base headings so the shallowest one in this text is level 1 (models often start at ###).
    levels = [b[1] for b in blocks if b[0] == "h"]
    if levels:
        base = min(levels)
        blocks = [("h", b[1] - base + 1, b[2]) if b[0] == "h" else b for b in blocks]
    return blocks


# --------------------------------------------------------------------------- #
# Document model
# --------------------------------------------------------------------------- #
def _slug(text):
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:40] or "report"


def build_document(task, names, full):
    meta = [
        ("Mission", task["prompt"]),
        ("Created", task["created"]),
        ("Model", task["model"]),
        ("Agents", ", ".join(names.get(a, a) for a in task["agents"])),
        ("Duration", f"{task['duration']}s"),
        ("Approx. tokens", task["tokens"]),
        ("Quality score", (
            f"{task['score']}/100"
            + (f" (was {task['score_before']}/100 before auto-revision)" if task.get("score_before") is not None else "")
            if task.get("score") is not None else "n/a"
        )),
    ]
    sections = [{"heading": "Final report", "markdown": task["report"]}]
    if full:
        for s in task["subtasks"]:
            heading = s.get("label") or f"{names.get(s['agent'], s['agent'])} output"
            sections.append({"heading": heading, "markdown": s["output"]})
        skill_lines = [
            f"- **{names.get(a, a)}**: {', '.join(sk) if sk else 'no skills active'}"
            for a, sk in task.get("skills", {}).items()
        ]
        if skill_lines:
            sections.append({"heading": "Skills used", "markdown": "\n".join(skill_lines)})
    return {"title": task["title"], "meta": meta, "sections": sections}


# --------------------------------------------------------------------------- #
# Markdown / text
# --------------------------------------------------------------------------- #
def build_markdown(doc):
    out = [f"# {doc['title']}", ""]
    out += [f"- **{k}:** {v}" for k, v in doc["meta"]]
    for sec in doc["sections"]:
        out += ["", "---", "", f"## {sec['heading']}", "", sec["markdown"].strip()]
    return ("\n".join(out) + "\n").encode("utf-8")


def build_text(doc):
    out = [doc["title"].upper(), "=" * min(len(doc["title"]), 78), ""]
    out += [f"{k}: {v}" for k, v in doc["meta"]]
    for sec in doc["sections"]:
        out += ["", "", sec["heading"].upper(), "-" * len(sec["heading"]), ""]
        for b in parse_blocks(sec["markdown"]):
            kind = b[0]
            if kind == "h":
                out += ["", plain_inline(b[2]).upper(), ""]
            elif kind == "p":
                out += [textwrap.fill(plain_inline(b[1]), 88), ""]
            elif kind == "ul":
                out += [textwrap.fill(plain_inline(x), 88, initial_indent="  - ", subsequent_indent="    ") for x in b[1]] + [""]
            elif kind == "ol":
                out += [
                    textwrap.fill(plain_inline(x), 88, initial_indent=f"  {n}. ", subsequent_indent="     ")
                    for n, x in enumerate(b[1], 1)
                ] + [""]
            elif kind == "table":
                out += ["  " + " | ".join(plain_inline(c) for c in row) for row in b[1]] + [""]
            elif kind == "code":
                out += ["    " + ln for ln in b[1].splitlines()] + [""]
            elif kind == "quote":
                out += [textwrap.fill(plain_inline(b[1]), 86, initial_indent="> ", subsequent_indent="> "), ""]
            elif kind == "hr":
                out += ["-" * 40, ""]
    return ("\n".join(out).rstrip() + "\n").encode("utf-8")


# --------------------------------------------------------------------------- #
# HTML
# --------------------------------------------------------------------------- #
_HTML_CSS = """
:root{--bg:#ffffff;--fg:#1e293b;--muted:#64748b;--line:#e2e8f0;--accent:#4f46e5;--soft:#f1f5f9}
@media (prefers-color-scheme:dark){:root{--bg:#0b1020;--fg:#e2e8f0;--muted:#94a3b8;--line:#25304f;--accent:#818cf8;--soft:#111a33}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:16px/1.65 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
main{max-width:860px;margin:0 auto;padding:48px 24px 80px}
header{background:linear-gradient(135deg,#1e1b4b,#312e81 45%,#581c87);color:#fff;border-radius:18px;padding:28px 32px;margin-bottom:28px}
header h1{margin:0 0 6px;font-size:1.9rem;line-height:1.25;color:#fff}
header p{margin:0;color:#c7d2fe;font-size:.95rem}
dl{display:grid;grid-template-columns:max-content 1fr;gap:6px 18px;background:var(--soft);border:1px solid var(--line);border-radius:14px;padding:16px 20px;font-size:.9rem}
dt{color:var(--muted);font-weight:600} dd{margin:0}
section{margin-top:44px} section>h2{border-bottom:2px solid var(--accent);padding-bottom:6px}
h1,h2,h3,h4{line-height:1.3;letter-spacing:-.01em} h3{margin-top:1.8em} h4{margin-top:1.4em}
table{border-collapse:collapse;width:100%;margin:1.2em 0;font-size:.92rem}
th,td{border:1px solid var(--line);padding:8px 12px;text-align:left;vertical-align:top}
th{background:var(--soft)}
code{background:var(--soft);padding:2px 6px;border-radius:5px;font-size:.9em}
pre{background:var(--soft);border:1px solid var(--line);border-radius:10px;padding:14px;overflow:auto}
pre code{background:none;padding:0}
blockquote{margin:1.2em 0;padding:8px 18px;border-left:4px solid var(--accent);background:var(--soft);border-radius:0 10px 10px 0}
hr{border:0;border-top:1px solid var(--line);margin:2em 0}
footer{margin-top:60px;color:var(--muted);font-size:.8rem;text-align:center}
@media print{header{background:#312e81!important;-webkit-print-color-adjust:exact;print-color-adjust:exact}main{padding:0}}
"""


def _inline_html(text):
    out = []
    for tok, style in inline_tokens(text):
        e = _html.escape(tok)
        out.append({"b": f"<strong>{e}</strong>", "i": f"<em>{e}</em>", "c": f"<code>{e}</code>"}.get(style, e))
    return "".join(out)


def build_html(doc):
    body = []
    for sec in doc["sections"]:
        body.append(f"<section><h2>{_html.escape(sec['heading'])}</h2>")
        for b in parse_blocks(sec["markdown"]):
            kind = b[0]
            if kind == "h":
                lvl = min(b[1] + 2, 5)
                body.append(f"<h{lvl}>{_inline_html(b[2])}</h{lvl}>")
            elif kind == "p":
                body.append(f"<p>{_inline_html(b[1])}</p>")
            elif kind == "ul":
                body.append("<ul>" + "".join(f"<li>{_inline_html(x)}</li>" for x in b[1]) + "</ul>")
            elif kind == "ol":
                body.append("<ol>" + "".join(f"<li>{_inline_html(x)}</li>" for x in b[1]) + "</ol>")
            elif kind == "table":
                head, rest = b[1][0], b[1][1:]
                t = "<table><thead><tr>" + "".join(f"<th>{_inline_html(c)}</th>" for c in head) + "</tr></thead><tbody>"
                t += "".join("<tr>" + "".join(f"<td>{_inline_html(c)}</td>" for c in r) + "</tr>" for r in rest)
                body.append(t + "</tbody></table>")
            elif kind == "code":
                body.append(f"<pre><code>{_html.escape(b[1])}</code></pre>")
            elif kind == "quote":
                body.append(f"<blockquote>{_inline_html(b[1])}</blockquote>")
            elif kind == "hr":
                body.append("<hr>")
        body.append("</section>")
    meta = "".join(f"<dt>{_html.escape(str(k))}</dt><dd>{_html.escape(str(v))}</dd>" for k, v in doc["meta"])
    page = (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        f"<title>{_html.escape(doc['title'])}</title><style>{_HTML_CSS}</style></head><body><main>"
        f"<header><h1>{_html.escape(doc['title'])}</h1><p>Generated by Nexus AI</p></header>"
        f"<dl>{meta}</dl>{''.join(body)}"
        f"<footer>Nexus AI · exported {datetime.now():%Y-%m-%d %H:%M}</footer></main></body></html>"
    )
    return page.encode("utf-8")


# --------------------------------------------------------------------------- #
# Word (.docx)
# --------------------------------------------------------------------------- #
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _clean(text):
    return _CTRL.sub("", str(text))


def _docx_runs(paragraph, text, bold=False):
    from docx.shared import Pt

    for tok, style in inline_tokens(_clean(text)):
        run = paragraph.add_run(tok)
        run.bold = bold or style == "b"
        run.italic = style == "i"
        if style == "c":
            run.font.name = "Courier New"
            run.font.size = Pt(9.5)


def build_docx(doc):
    from docx import Document
    from docx.shared import Pt, RGBColor

    d = Document()
    d.styles["Normal"].font.name = "Calibri"
    d.styles["Normal"].font.size = Pt(11)
    d.add_heading(_clean(doc["title"]), 0)
    for k, v in doc["meta"]:
        p = d.add_paragraph()
        p.paragraph_format.space_after = Pt(2)
        r = p.add_run(f"{k}: ")
        r.bold = True
        r.font.color.rgb = RGBColor(0x4F, 0x46, 0xE5)
        p.add_run(_clean(v))

    for sec in doc["sections"]:
        d.add_heading(_clean(sec["heading"]), 1)
        for b in parse_blocks(sec["markdown"]):
            kind = b[0]
            if kind == "h":
                d.add_heading(_clean(plain_inline(b[2])), min(b[1] + 1, 4))
            elif kind == "p":
                _docx_runs(d.add_paragraph(), b[1])
            elif kind == "ul":
                for item in b[1]:
                    _docx_runs(d.add_paragraph(style="List Bullet"), item)
            elif kind == "ol":
                # Manual numbering: Word's "List Number" style shares one counter across the
                # whole document, so a second list would continue from the first.
                for n, item in enumerate(b[1], 1):
                    p = d.add_paragraph()
                    p.paragraph_format.left_indent = Pt(18)
                    p.paragraph_format.first_line_indent = Pt(-18)
                    _docx_runs(p, f"{n}. {item}")
            elif kind == "table":
                rows = b[1]
                table = d.add_table(rows=len(rows), cols=len(rows[0]))
                table.style = "Table Grid"
                for ri, row in enumerate(rows):
                    for ci, cell_text in enumerate(row):
                        cell = table.cell(ri, ci)
                        _docx_runs(cell.paragraphs[0], cell_text, bold=(ri == 0))
                d.add_paragraph()
            elif kind == "code":
                p = d.add_paragraph()
                run = p.add_run(_clean(b[1]))
                run.font.name = "Courier New"
                run.font.size = Pt(9)
            elif kind == "quote":
                _docx_runs(d.add_paragraph(style="Quote"), b[1])
            elif kind == "hr":
                d.add_paragraph("_" * 40)

    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# PDF
# --------------------------------------------------------------------------- #
_PDF_MAP = {
    "→": "->", "←": "<-", "↔": "<->", "≈": "~", "≥": ">=", "≤": "<=", "×": "x", "−": "-",
    "²": "2", "³": "3", "✓": "v", "✔": "v", "✅": "[x]", "⚠️": "(!)", "⚠": "(!)",
    "‑": "-", "\u00a0": " ",
}


def _pdf_text(text):
    text = str(text)
    for k, v in _PDF_MAP.items():
        text = text.replace(k, v)
    # Built-in PDF fonts cover Latin-1 / cp1252 only; drop anything else (emoji, CJK, ...).
    return text.encode("cp1252", "ignore").decode("cp1252")


def _xml(s):
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _pdf_inline(text):
    out = []
    for tok, style in inline_tokens(_pdf_text(text)):
        e = _xml(tok)
        out.append({"b": f"<b>{e}</b>", "i": f"<i>{e}</i>", "c": f'<font name="Courier">{e}</font>'}.get(style, e))
    return "".join(out)


def build_pdf(doc):
    from reportlab.lib import colors
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
    from reportlab.lib.units import cm
    from reportlab.platypus import (
        HRFlowable, ListFlowable, ListItem, Paragraph, Preformatted, SimpleDocTemplate, Spacer, Table, TableStyle,
    )

    base = getSampleStyleSheet()
    accent = colors.HexColor("#4F46E5")
    body = ParagraphStyle("body", parent=base["BodyText"], fontSize=10, leading=14.5, spaceAfter=6)
    cell = ParagraphStyle("cell", parent=body, fontSize=8.8, leading=11.5, spaceAfter=0)
    title_st = ParagraphStyle("title", parent=base["Title"], fontSize=23, leading=28, textColor=accent, alignment=0, spaceAfter=10)
    meta_st = ParagraphStyle("meta", parent=body, fontSize=9, leading=12.5, textColor=colors.HexColor("#475569"), spaceAfter=2)
    sec_st = ParagraphStyle("sec", parent=base["Heading1"], fontSize=17, leading=21, textColor=colors.HexColor("#1e1b4b"), spaceBefore=18, spaceAfter=8)
    h_sizes = {1: 14, 2: 12.5, 3: 11.5, 4: 10.5, 5: 10, 6: 10}
    h_st = {
        n: ParagraphStyle(f"h{n}", parent=base["Heading3"], fontName="Helvetica-Bold", fontSize=sz, leading=sz + 4, textColor=colors.HexColor("#312e81"), spaceBefore=10, spaceAfter=4)
        for n, sz in h_sizes.items()
    }
    quote_st = ParagraphStyle("quote", parent=body, leftIndent=14, textColor=colors.HexColor("#334155"), borderPadding=(4, 4, 4, 10))
    code_st = ParagraphStyle("code", parent=base["Code"], fontSize=8, leading=10.5, backColor=colors.HexColor("#f1f5f9"), borderPadding=6, spaceAfter=8)

    buf = io.BytesIO()
    avail = A4[0] - 4 * cm
    pdf = SimpleDocTemplate(
        buf, pagesize=A4, leftMargin=2 * cm, rightMargin=2 * cm, topMargin=2 * cm, bottomMargin=2 * cm,
        title=_pdf_text(doc["title"]), author="Nexus AI",
    )

    story = [Paragraph(_xml(_pdf_text(doc["title"])), title_st)]
    for k, v in doc["meta"]:
        story.append(Paragraph(f"<b>{_xml(_pdf_text(k))}:</b> {_xml(_pdf_text(v))}", meta_st))

    for sec in doc["sections"]:
        story.append(Paragraph(_xml(_pdf_text(sec["heading"])), sec_st))
        story.append(HRFlowable(width="100%", thickness=1.2, color=accent, spaceAfter=8))
        for b in parse_blocks(sec["markdown"]):
            kind = b[0]
            if kind == "h":
                story.append(Paragraph(_pdf_inline(b[2]), h_st[min(b[1], 6)]))
            elif kind == "p":
                story.append(Paragraph(_pdf_inline(b[1]), body))
            elif kind in ("ul", "ol"):
                items = [ListItem(Paragraph(_pdf_inline(x), body), leftIndent=16) for x in b[1]]
                if kind == "ul":
                    story.append(ListFlowable(items, bulletType="bullet", start="•", leftIndent=16, bulletFontSize=9))
                else:
                    story.append(ListFlowable(items, bulletType="1", leftIndent=18, bulletFontSize=9))
                story.append(Spacer(1, 4))
            elif kind == "table":
                rows = b[1]
                n = len(rows[0])
                data = [
                    [Paragraph(("<b>%s</b>" if ri == 0 else "%s") % _pdf_inline(c), cell) for c in row]
                    for ri, row in enumerate(rows)
                ]
                t = Table(data, colWidths=[avail / n] * n, repeatRows=1)
                t.setStyle(TableStyle([
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e0e7ff")),
                    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#cbd5e1")),
                    ("VALIGN", (0, 0), (-1, -1), "TOP"),
                    ("LEFTPADDING", (0, 0), (-1, -1), 6), ("RIGHTPADDING", (0, 0), (-1, -1), 6),
                    ("TOPPADDING", (0, 0), (-1, -1), 4), ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
                ]))
                story += [t, Spacer(1, 10)]
            elif kind == "code":
                wrapped = "\n".join(
                    "\n".join(textwrap.wrap(ln, 92, replace_whitespace=False, drop_whitespace=False)) or ""
                    for ln in _pdf_text(b[1]).splitlines()
                )
                story.append(Preformatted(wrapped, code_st))
            elif kind == "quote":
                story.append(Paragraph(_pdf_inline(b[1]), quote_st))
            elif kind == "hr":
                story.append(HRFlowable(width="100%", thickness=0.6, color=colors.HexColor("#cbd5e1"), spaceBefore=6, spaceAfter=6))

    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("Helvetica", 8)
        canvas.setFillGray(0.5)
        canvas.drawString(2 * cm, 1.1 * cm, "Nexus AI")
        canvas.drawRightString(A4[0] - 2 * cm, 1.1 * cm, f"Page {canvas.getPageNumber()}")
        canvas.restoreState()

    pdf.build(story, onFirstPage=footer, onLaterPages=footer)
    return buf.getvalue()


# --------------------------------------------------------------------------- #
# JSON / entry points
# --------------------------------------------------------------------------- #
def build_json(task, names):
    payload = dict(task)
    payload["agent_names"] = {a: names.get(a, a) for a in task["agents"]}
    payload["exported_at"] = datetime.now().isoformat(timespec="seconds")
    return json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8")


def export_task(task, names, fmt_label, full=False):
    """Return (bytes, filename, mime) for one task in the chosen format."""
    ext, mime = FORMATS[fmt_label]
    stem = f"{task['id']}_{_slug(task['title'])}" + ("_full" if full and ext != "json" else "")
    if ext == "json":
        data = build_json(task, names)
    else:
        doc = build_document(task, names, full)
        data = {
            "docx": build_docx, "pdf": build_pdf, "html": build_html, "md": build_markdown, "txt": build_text,
        }[ext](doc)
    return data, f"{stem}.{ext}", mime


def export_zip(task, names, full=False):
    """Return (bytes, filename, mime) for a ZIP holding every format that can be built."""
    buf = io.BytesIO()
    skipped = []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for label in FORMATS:
            try:
                data, fname, _ = export_task(task, names, label, full)
                zf.writestr(fname, data)
            except ImportError:
                skipped.append(f"{label} (missing package: pip install python-docx reportlab)")
            except Exception as exc:  # noqa: BLE001
                skipped.append(f"{label} ({exc})")
        if skipped:
            zf.writestr("README.txt", "Skipped formats:\n" + "\n".join(skipped))
    return buf.getvalue(), f"{task['id']}_{_slug(task['title'])}_all-formats.zip", "application/zip"