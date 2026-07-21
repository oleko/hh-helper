"""Сборка дайджеста: топ вакансий по оценке в читаемый markdown-файл."""
from __future__ import annotations

import json
from datetime import date

from .storage import Storage

RECOMMEND_LABEL = {
    "respond": "🟢 откликаться",
    "consider": "🟡 подумать",
    "skip": "⚪ пропустить",
}


def build_digest(storage: Storage, min_score: int, top_n: int) -> str:
    rows = storage.top_for_digest(min_score, top_n)
    lines = [f"# Дайджест вакансий — {date.today().isoformat()}", ""]
    if not rows:
        lines.append("Новых вакансий с оценкой выше порога нет.")
        return "\n".join(lines)

    for r in rows:
        red_flags = json.loads(r["red_flags"] or "[]")
        flags_str = f" | ⚠ {', '.join(red_flags)}" if red_flags else ""
        lines.append(f"## [{r['score']}] {r['name']} — {r['employer'] or '—'}")
        lines.append(
            f"Трек {r['track'] or '?'} · ЗП {r['salary_fit'] or 'не указана'} · "
            f"{RECOMMEND_LABEL.get(r['recommend'], r['recommend'])}{flags_str}"
        )
        lines.append(f"{r['rationale'] or ''}")
        lines.append(f"{r['alternate_url'] or r['url']}")
        lines.append(f"`id: {r['id']}` — чтобы подготовить резюме/письмо: `python -m src.main tailor {r['id']}`")
        lines.append("")

    storage.mark_digested([r["id"] for r in rows])
    return "\n".join(lines)
