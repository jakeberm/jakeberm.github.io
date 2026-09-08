"""Render the hygiene report as HTML (for email/browser) and a short text summary."""

from __future__ import annotations

import html
from datetime import datetime

import config

CSS = """
body{font-family:Aptos,Calibri,-apple-system,Segoe UI,sans-serif;font-size:14px;color:#1b1b1b;
 background:#f6f7f9;margin:0;padding:24px}
.wrap{max-width:860px;margin:0 auto;background:#fff;border-radius:10px;padding:28px;
 box-shadow:0 1px 3px rgba(0,0,0,.08)}
h1{font-size:20px;margin:0 0 4px}h2{font-size:15px;margin:28px 0 8px;color:#444}
.sub{color:#777;font-size:12px;margin-bottom:20px}
.cards{display:flex;gap:12px;flex-wrap:wrap;margin-bottom:8px}
.card{flex:1;min-width:120px;background:#f2f5f9;border-radius:8px;padding:12px 14px}
.card .n{font-size:22px;font-weight:600}.card .l{font-size:11px;color:#666;text-transform:uppercase}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:#666;font-weight:600;font-size:11px;text-transform:uppercase;
 border-bottom:1px solid #e3e6ea;padding:6px 8px}
td{padding:6px 8px;border-bottom:1px solid #f0f2f4;vertical-align:top}
.num{text-align:right;font-variant-numeric:tabular-nums;white-space:nowrap}
.pill{display:inline-block;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:600}
.hi{background:#fde7e9;color:#a4262c}.mid{background:#fff4ce;color:#8a6100}
.lo{background:#dff6dd;color:#0b6a0b}
a{color:#0f6cbd}.muted{color:#888;font-size:12px}
.foot{margin-top:24px;color:#999;font-size:11px;border-top:1px solid #eee;padding-top:12px}
"""


def _pill(score: float) -> str:
    cls = "hi" if score >= 75 else ("mid" if score >= 55 else "lo")
    return f'<span class="pill {cls}">{score:g}</span>'


def _esc(value) -> str:
    return html.escape(str(value or ""))


def render_html(analysis: dict, cleanup_summary: dict, rules_summary: dict) -> str:
    candidates = analysis.get("unsubscribe_candidates", [])
    top = analysis.get("top_senders", [])[:15]

    rows = []
    for c in candidates[:25]:
        links = []
        if c.get("url"):
            links.append(f'<a href="{_esc(c["url"])}">unsubscribe link</a>')
        if c.get("mailto"):
            links.append(f'<a href="mailto:{_esc(c["mailto"])}?subject=unsubscribe">email opt-out</a>')
        rows.append(
            "<tr>"
            f"<td>{_pill(c['score'])}</td>"
            f"<td><b>{_esc(c['name'] or c['address'])}</b><br><span class='muted'>{_esc(c['address'])}</span></td>"
            f"<td class='num'>{c['count']}</td>"
            f"<td class='num'>{c['per_week']}/wk</td>"
            f"<td class='num'>{c['read_rate'] * 100:.0f}%</td>"
            f"<td>{' &middot; '.join(links) or '<span class=muted>no opt-out header</span>'}</td>"
            "</tr>"
        )

    top_rows = "".join(
        "<tr>"
        f"<td>{_esc(s['name'] or s['address'])}<br><span class='muted'>{_esc(s['address'])}</span></td>"
        f"<td class='num'>{s['count']}</td>"
        f"<td class='num'>{s['read_rate'] * 100:.0f}%</td>"
        f"<td class='num'>{s['per_week']}/wk</td>"
        "</tr>"
        for s in top
    )

    return f"""<!doctype html><html><head><meta charset="utf-8">
<title>Inbox hygiene report</title><style>{CSS}</style></head><body><div class="wrap">
<h1>Inbox hygiene report</h1>
<div class="sub">{_esc(analysis.get('account'))} &middot; last {analysis.get('lookback_days')} days
 &middot; generated {_esc(analysis.get('generated_at'))}</div>

<div class="cards">
  <div class="card"><div class="n">{analysis.get('total_messages', 0)}</div><div class="l">Messages</div></div>
  <div class="card"><div class="n">{analysis.get('total_senders', 0)}</div><div class="l">Senders</div></div>
  <div class="card"><div class="n">{len(candidates)}</div><div class="l">Unsub candidates</div></div>
  <div class="card"><div class="n">{cleanup_summary.get('moved', 0) + cleanup_summary.get('archived', 0)}</div>
   <div class="l">Filed this run</div></div>
  <div class="card"><div class="n">{rules_summary.get('created', 0) + rules_summary.get('updated', 0)}</div>
   <div class="l">Rules changed</div></div>
</div>

<h2>Recommended unsubscribes</h2>
{"<table><tr><th>Score</th><th>Sender</th><th class=num>Msgs</th><th class=num>Rate</th>"
 "<th class=num>Read</th><th>Opt out</th></tr>" + "".join(rows) + "</table>"
 if rows else "<p class='muted'>Nothing worth unsubscribing from right now.</p>"}

<h2>Most frequent senders</h2>
<table><tr><th>Sender</th><th class="num">Msgs</th><th class="num">Read</th><th class="num">Rate</th></tr>
{top_rows}</table>

<div class="foot">Decide by editing <code>~/.email-hygiene/decisions.json</code> &mdash; set each
 sender's <code>decision</code> to <code>unsubscribe</code>, <code>block</code>, or <code>keep</code>,
 then re-run with <code>--apply</code>. Unsubscribe links are never clicked automatically.</div>
</div></body></html>"""


def render_text(analysis: dict, cleanup_summary: dict, rules_summary: dict) -> str:
    candidates = analysis.get("unsubscribe_candidates", [])
    lines = [
        f"Inbox hygiene — {analysis.get('account')}",
        f"{analysis.get('total_messages', 0)} messages / {analysis.get('total_senders', 0)} senders "
        f"over {analysis.get('lookback_days')}d",
        f"filed={cleanup_summary.get('moved', 0) + cleanup_summary.get('archived', 0)} "
        f"rules_changed={rules_summary.get('created', 0) + rules_summary.get('updated', 0)}",
        "",
        f"Top unsubscribe candidates ({len(candidates)}):",
    ]
    for c in candidates[:10]:
        lines.append(
            f"  [{c['score']:>5}] {c['count']:>4}x  {c['read_rate'] * 100:>3.0f}% read  {c['address']}"
        )
    if not candidates:
        lines.append("  (none)")
    return "\n".join(lines)


def write_report(analysis: dict, cleanup_summary: dict, rules_summary: dict):
    config.ensure_dirs()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    path = config.REPORT_DIR / f"hygiene-{stamp}.html"
    body = render_html(analysis, cleanup_summary, rules_summary)
    path.write_text(body, encoding="utf-8")
    _prune(config.load_profile().get("report", {}).get("keep_reports", 30))
    return path, body


def _prune(keep: int) -> None:
    reports = sorted(config.REPORT_DIR.glob("hygiene-*.html"))
    for stale in reports[:-keep] if keep > 0 else []:
        try:
            stale.unlink()
        except OSError:
            pass
