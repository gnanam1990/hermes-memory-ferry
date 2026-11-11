"""Accessible standalone HTML evidence report generated from authoritative SQLite state."""

from __future__ import annotations

from html import escape
from pathlib import Path
import json

from .store import FerryStore


STYLE = """
:root{color-scheme:dark;--bg:#07111f;--panel:#0d1b2e;--line:#223652;--text:#f4f7fb;--muted:#9fb0c7;--a:#62e6c8;--warn:#ffd166;--bad:#ff7d8d}
*{box-sizing:border-box}body{margin:0;background:radial-gradient(circle at 10% 0,#123054 0,transparent 36%),var(--bg);color:var(--text);font:16px/1.55 Inter,ui-sans-serif,system-ui,sans-serif}
main{width:min(1120px,calc(100% - 32px));margin:0 auto;padding:48px 0 80px}header{display:flex;gap:24px;align-items:flex-end;justify-content:space-between;flex-wrap:wrap;margin-bottom:28px}
h1{font-size:clamp(2rem,5vw,4.5rem);line-height:.95;letter-spacing:-.06em;margin:0}.eyebrow{color:var(--a);text-transform:uppercase;letter-spacing:.14em;font-weight:800;font-size:.76rem}
.state{border:1px solid var(--line);border-radius:999px;padding:8px 14px;font-weight:800}.grid{display:grid;grid-template-columns:repeat(12,1fr);gap:16px}.card{grid-column:span 4;background:color-mix(in srgb,var(--panel) 88%,transparent);border:1px solid var(--line);border-radius:20px;padding:22px;box-shadow:0 18px 55px #0005}.wide{grid-column:span 12}.value{font-size:2.1rem;font-weight:850;letter-spacing:-.04em}.label,.muted{color:var(--muted)}table{width:100%;border-collapse:collapse}th,td{text-align:left;padding:12px 8px;border-bottom:1px solid var(--line);vertical-align:top}th{color:var(--muted);font-size:.78rem;text-transform:uppercase;letter-spacing:.08em}code{font-family:ui-monospace,SFMono-Regular,monospace;color:var(--a)}.disclosure{border-left:3px solid var(--warn);padding-left:14px;color:var(--muted)}
@media(max-width:760px){.card{grid-column:span 12}main{width:min(100% - 24px,1120px);padding-top:28px}th:nth-child(3),td:nth-child(3){display:none}}
@media(prefers-reduced-motion:no-preference){.card{animation:rise .45s ease-out both}.card:nth-child(2){animation-delay:.05s}.card:nth-child(3){animation-delay:.1s}@keyframes rise{from{opacity:0;transform:translateY(10px)}}}
"""


def generate_report(store: FerryStore, migration_id: str, output: Path | None = None) -> Path:
    status = store.status(migration_id)
    if not status.get("found"):
        raise ValueError("migration not found")
    migration = status["migration"]
    parity = status.get("latest_parity")
    operations = store.operations(migration_id)
    output = output or (store.artifacts_dir / f"{migration_id}.report.html")
    rows = "".join(
        "<tr><td><code>{}</code></td><td>{}</td><td>{}</td></tr>".format(
            escape(str(row["operation"])), escape(str(row["state"])), escape(str(row["updated_at"]))
        )
        for row in operations[-25:]
    ) or '<tr><td colspan="3" class="muted">No operations recorded.</td></tr>'
    parity_status = parity["report"]["status"] if parity else "NOT_RUN"
    html = f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Memory Ferry evidence</title><style>{STYLE}</style></head>
<body><main><header><div><div class="eyebrow">Migration evidence · local only</div><h1>Memory Ferry</h1></div><div class="state">{escape(migration['state'])}</div></header>
<section class="grid" aria-label="Migration summary">
<article class="card"><div class="label">Plan records</div><div class="value">{sum(status['plan_counts'].values())}</div><div class="muted">{escape(json.dumps(status['plan_counts'], sort_keys=True))}</div></article>
<article class="card"><div class="label">Operations</div><div class="value">{sum(status['operation_counts'].values())}</div><div class="muted">{escape(json.dumps(status['operation_counts'], sort_keys=True))}</div></article>
<article class="card"><div class="label">Retrieval parity</div><div class="value">{escape(parity_status)}</div><div class="muted">Deterministic corpus result</div></article>
<article class="card wide"><h2>Bound identity</h2><p><span class="label">Migration</span><br><code>{escape(migration_id)}</code></p><p><span class="label">Plan digest</span><br><code>{escape(migration['plan_digest'])}</code></p><p><span class="label">Source snapshot</span><br><code>{escape(migration['source_snapshot'])}</code></p></article>
<article class="card wide"><h2>Recent durable operations</h2><div style="overflow:auto"><table><thead><tr><th>Operation</th><th>State</th><th>Observed</th></tr></thead><tbody>{rows}</tbody></table></div></article>
<article class="card wide"><h2>Disclosure</h2><p class="disclosure">This report proves the local records and deterministic checks shown at the bound revision. It does not prove semantic equivalence, business correctness, external-provider portability, or production security.</p></article>
</section></main></body></html>"""
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(html, encoding="utf-8")
    output.chmod(0o600)
    return output

