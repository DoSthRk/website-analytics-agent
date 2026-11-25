"""Weekly anomaly digest for the Feishu ops group.

Built from the 产品变化定位 rows the period refresh already computed: the
latest complete week, compared with the 前8周均值 baseline. Only items that
take part in alerts and reached their minimum baseline volume can be anomalous
(see ``period_facts._assess``). One message per site and week: a state file
remembers the last week sent, and the same week is never sent twice.

The chat ID and dashboard link live in a server-only JSON config, never in
this public repository.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from website_analytics.period_facts import ALERT_MODE, WEEK

LEVEL_ORDER = {"全部产品": 0, "一级分类": 1, "二级分类": 2, "末级产品": 3}
MAX_ITEMS = 25


@dataclass(frozen=True)
class AlertConfig:
    chat_id: str
    dashboard_url: str
    identity: str = "bot"


@dataclass(frozen=True)
class Digest:
    period: str
    start: date
    end: date
    anomalies: int
    scopes: int
    markdown: str


def load_alert_config(path: Path) -> AlertConfig:
    raw = json.loads(path.read_text(encoding="utf-8"))
    chat_id = str(raw.get("chat_id") or "")
    if not chat_id.startswith("oc_"):
        raise ValueError("alert config needs a group chat_id (oc_...)")
    url = str(raw.get("dashboard_url") or "")
    if not url.startswith("https://"):
        raise ValueError("alert config needs an https dashboard_url")
    identity = str(raw.get("identity") or "bot")
    if identity not in ("bot", "user"):
        raise ValueError("alert identity must be bot or user")
    return AlertConfig(chat_id, url, identity)


def _number(value: float, percent: bool) -> str:
    return f"{value:.1f}%" if percent else f"{value:,.0f}"


def _line(row: Mapping[str, Any]) -> str:
    item = str(row["item"])
    percent = item.endswith("（%）")
    label = item.removesuffix("（%）")
    change = float(row["change"])
    arrow = "↑" if change > 0 else "↓"
    return (
        f"- {label}：{_number(float(row['current']), percent)}，"
        f"基准 {_number(float(row['baseline']), percent)}（{arrow}{abs(change):.0%}）"
    )


def weekly_digest(change_rows: Sequence[Mapping[str, Any]], dashboard_url: str) -> Digest | None:
    """The message for the latest complete week, or None when there is no week yet."""
    rows = [row for row in change_rows if row.get("comparison_mode") == ALERT_MODE[WEEK]]
    if not rows:
        return None
    periods = {str(row["period"]) for row in rows}
    if len(periods) != 1:
        raise ValueError(f"change rows cover more than one week: {sorted(periods)}")
    period = periods.pop()
    start = date.fromisoformat(str(rows[0]["period_start"])[:10])
    end = date.fromisoformat(str(rows[0]["period_end"])[:10])
    scopes_total = len({row["scope_id"] for row in rows})

    by_scope: dict[str, list[Mapping[str, Any]]] = {}
    for row in rows:
        if row.get("is_anomaly") and row.get("alert_enabled") and row.get("volume_ok") and row.get("change") is not None:
            by_scope.setdefault(str(row["scope_id"]), []).append(row)
    ordered = sorted(
        by_scope.values(),
        key=lambda items: (LEVEL_ORDER.get(str(items[0]["scope_level"]), 9),
                           -max(abs(float(item["change"])) for item in items),
                           str(items[0]["scope_name"])),
    )
    anomalies = sum(len(items) for items in ordered)

    lines = [
        f"**官网产品诊断 · {period} 周提醒**",
        f"最近完整周 {start:%m/%d}–{end:%m/%d}，对照{ALERT_MODE[WEEK]}。",
        "规则：偏离基准 30% 以上且基准量达标；落地会话、到站转化率、有效会话率受爬虫影响，不参与提醒。",
        "",
    ]
    if not ordered:
        lines.append(f"本周 {scopes_total} 个产品范围都没有异常项。")
    else:
        lines.append(f"共 {len(ordered)} 个范围、{anomalies} 项异常：")
        shown = 0
        for items in ordered:
            if shown >= MAX_ITEMS:
                break
            first = items[0]
            level = str(first["scope_level"])
            title = str(first["scope_name"]) if level == "全部产品" else f"{first['scope_name']}（{level}）"
            lines += ["", f"**{title}**"]
            for item in sorted(items, key=lambda row: int(row["item_order"])):
                lines.append(_line(item))
                shown += 1
        if anomalies > shown:
            lines += ["", f"另有 {anomalies - shown} 项未列出，见看板。"]
    lines += ["", f"[打开产品诊断看板]({dashboard_url})（范围名称选上面的范围、对照方式选{ALERT_MODE[WEEK]}）"]
    return Digest(period, start, end, anomalies, scopes_total, "\n".join(lines))


def _sent_week(state_path: Path, site: str) -> str | None:
    if not state_path.exists():
        return None
    return json.loads(state_path.read_text(encoding="utf-8")).get("sent", {}).get(site)


def _record_sent(state_path: Path, site: str, period: str) -> None:
    state = json.loads(state_path.read_text(encoding="utf-8")) if state_path.exists() else {}
    state.setdefault("sent", {})[site] = period
    temporary = state_path.with_suffix(".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(state_path)


def send_weekly_alert(
    *,
    site: str,
    change_rows: Sequence[Mapping[str, Any]],
    config: AlertConfig,
    state_path: Path,
    send: Callable[[list[str]], dict[str, Any]],
    runtime: Sequence[str],
    apply: bool,
) -> dict[str, Any]:
    """Send this week's digest once. ``send`` runs a lark-cli command."""
    digest = weekly_digest(change_rows, config.dashboard_url)
    if digest is None:
        return {"status": "no_complete_week"}
    summary: dict[str, Any] = {"period": digest.period, "anomalies": digest.anomalies, "scopes": digest.scopes}
    if _sent_week(state_path, site) == digest.period:
        return {"status": "already_sent", **summary}
    command = [
        *runtime, "im", "+messages-send", "--as", config.identity, "--chat-id", config.chat_id,
        "--markdown", digest.markdown, "--idempotency-key", f"wa-period-alert-{site}-{digest.period}",
    ]
    if not apply:
        return {"status": "dry_run", **summary}
    send(command)
    _record_sent(state_path, site, digest.period)
    return {"status": "sent", **summary}
