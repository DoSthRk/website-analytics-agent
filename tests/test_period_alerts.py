import json

import pytest

from website_analytics.period_alerts import AlertConfig, load_alert_config, send_weekly_alert, weekly_digest

URL = "https://example.feishu.cn/base/x?table=blk"


def _row(scope_id, scope_name, level, order, item, current, baseline, change, *, anomaly=True, mode="前8周均值",
         alert=True, volume=True):
    return {
        "period": "2026-W46", "period_start": "2026-11-09", "period_end": "2026-11-15",
        "scope_id": scope_id, "scope_name": scope_name, "scope_level": level, "comparison_mode": mode,
        "item_order": order, "item": item, "current": current, "baseline": baseline, "change": change,
        "is_anomaly": anomaly, "alert_enabled": alert, "volume_ok": volume,
    }


ROWS = [
    _row("ALL", "全部产品", "全部产品", 1, "1. Google 搜索曝光", 48308, 36000, 0.3419, anomaly=True),
    _row("ALL", "全部产品", "全部产品", 9, "9. 官网表单入库数（非隔离）", 20, 21, -0.05, anomaly=False),
    _row("AAV", "AAV 纯化", "末级产品", 2, "2. 搜索点击率（%）", 1.8, 3.2, -0.4375),
    _row("AAV", "AAV 纯化", "末级产品", 3, "3. Google 自然搜索点击", 9, 25, -0.64),
    _row("L1:病毒", "病毒载体", "一级分类", 3, "3. Google 自然搜索点击", 40, 70, -0.4286),
    # Never in the digest: other baselines, items outside alerts, volume below the minimum.
    _row("ALL", "全部产品", "全部产品", 1, "1. Google 搜索曝光", 48308, 20000, 1.4, mode="前12周均值"),
    _row("ALL", "全部产品", "全部产品", 5, "5. 产品落地会话（全部渠道）", 900, 300, 2.0, alert=False),
    _row("TINY", "小产品", "末级产品", 9, "9. 官网表单入库数（非隔离）", 3, 1, 2.0, volume=False),
]


def test_digest_lists_alerting_anomalies_by_level_then_size():
    digest = weekly_digest(ROWS, URL)
    assert digest.period == "2026-W46" and digest.anomalies == 4 and digest.scopes == 4
    text = digest.markdown
    assert "11/09–11/15" in text and "对照前8周均值" in text
    order = [text.index(title) for title in ("**全部产品**", "**病毒载体（一级分类）**", "**AAV 纯化（末级产品）**")]
    assert order == sorted(order)
    assert "- 1. Google 搜索曝光：48,308，基准 36,000（↑34%）" in text
    assert "- 2. 搜索点击率：1.8%，基准 3.2%（↓44%）" in text
    assert text.index("2. 搜索点击率") < text.index("3. Google 自然搜索点击：9")
    assert "产品落地会话" not in text.split("规则：")[1].split("\n", 1)[1] and "小产品" not in text
    assert text.endswith(f"[打开产品诊断看板]({URL})（范围名称选上面的范围、对照方式选前8周均值）")


def test_quiet_week_still_reports_and_no_week_returns_none():
    quiet = [dict(row, is_anomaly=False) for row in ROWS]
    digest = weekly_digest(quiet, URL)
    assert digest.anomalies == 0 and "本周 4 个产品范围都没有异常项。" in digest.markdown
    assert weekly_digest([], URL) is None


def test_long_lists_are_cut_with_a_pointer_to_the_dashboard(monkeypatch):
    import website_analytics.period_alerts as module

    monkeypatch.setattr(module, "MAX_ITEMS", 2)
    text = weekly_digest(ROWS, URL).markdown
    assert "另有 2 项未列出，见看板。" in text


def test_sends_once_per_week_with_an_idempotency_key(tmp_path):
    config = AlertConfig("oc_test", URL)
    state = tmp_path / "state.json"
    sent = []
    kwargs = dict(site="genemedi-net", change_rows=ROWS, config=config, state_path=state,
                  send=lambda command: sent.append(command) or {"ok": True}, runtime=["lark-cli"])

    assert send_weekly_alert(apply=False, **kwargs)["status"] == "dry_run"
    assert not sent and not state.exists()
    assert send_weekly_alert(apply=True, **kwargs) == {"status": "sent", "period": "2026-W46", "anomalies": 4, "scopes": 4}
    command = sent[0]
    assert command[:5] == ["lark-cli", "im", "+messages-send", "--as", "bot"]
    assert command[command.index("--chat-id") + 1] == "oc_test"
    assert command[command.index("--idempotency-key") + 1] == "wa-period-alert-genemedi-net-2026-W46"
    assert json.loads(state.read_text())["sent"] == {"genemedi-net": "2026-W46"}
    assert send_weekly_alert(apply=True, **kwargs)["status"] == "already_sent" and len(sent) == 1


def test_failed_send_leaves_the_week_unsent(tmp_path):
    def fail(command):
        raise RuntimeError("bot is not in the group")

    state = tmp_path / "state.json"
    with pytest.raises(RuntimeError):
        send_weekly_alert(site="s", change_rows=ROWS, config=AlertConfig("oc_x", URL), state_path=state,
                          send=fail, runtime=[], apply=True)
    assert not state.exists()


@pytest.mark.parametrize("raw,message", [
    ({"chat_id": "123", "dashboard_url": URL}, "chat_id"),
    ({"chat_id": "oc_1", "dashboard_url": "http://x"}, "dashboard_url"),
    ({"chat_id": "oc_1", "dashboard_url": URL, "identity": "admin"}, "identity"),
])
def test_alert_config_is_validated(tmp_path, raw, message):
    path = tmp_path / "alerts.json"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_alert_config(path)
