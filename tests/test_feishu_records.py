import json
from types import SimpleNamespace

import pytest

from website_analytics.feishu_records import (
    FeishuTarget,
    LarkCLIRecordClient,
    bitable_v1_fields,
    is_retryable_lark_failure,
    lark_cli_environment,
    load_feishu_target,
    sync_record_sets,
)


def test_lark_qps_limit_is_retryable() -> None:
    assert is_retryable_lark_failure(
        code=1,
        message="GetChartLatestSnapshot onOverQPSLimit cluster_limits_123",
        raw="",
    )


class FakeClient:
    def __init__(self) -> None:
        self.tables = {
            "overview": [("rec-overview", {"周期键": "site|week|old"})],
            "product": [("rec-product", {"产品周期键": "site|week|old|GMP"})],
        }
        self.created = []
        self.updated = []

    def list_records(self, table_id):
        return self.tables[table_id]

    def create_records(self, table_id, records):
        self.created.append((table_id, list(records)))
        return len(records)

    def update_record(self, table_id, record_id, fields):
        self.updated.append((table_id, record_id, dict(fields)))


def test_sync_record_sets_updates_existing_keys_and_creates_missing_keys() -> None:
    client = FakeClient()
    target = FeishuTarget("base", "overview", "product", "user")
    result = sync_record_sets(
        client,
        target,
        overview=[
            {"周期键": "site|week|old", "官网访问次数": 10},
            {"周期键": "site|week|new", "官网访问次数": 20},
        ],
        products=[
            {"产品周期键": "site|week|old|GMP", "官网访问次数": 4},
            {"产品周期键": "site|week|new|GMP", "官网访问次数": 6},
        ],
    )

    assert result == {
        "overview_created": 1,
        "overview_updated": 1,
        "product_created": 1,
        "product_updated": 1,
    }
    assert [item[0] for item in client.created] == ["overview", "product"]
    assert [item[1] for item in client.updated] == ["rec-overview", "rec-product"]


def test_sync_record_sets_rejects_duplicate_existing_keys_before_writing() -> None:
    client = FakeClient()
    client.tables["overview"].append(("rec-duplicate", {"周期键": "site|week|old"}))
    target = FeishuTarget("base", "overview", "product", "user")

    with pytest.raises(ValueError, match="duplicate"):
        sync_record_sets(
            client,
            target,
            overview=[{"周期键": "site|week|old"}],
            products=[],
        )

    assert client.created == []
    assert client.updated == []


def test_load_feishu_target_rejects_unexpected_configuration(tmp_path) -> None:
    path = tmp_path / "target.json"
    path.write_text(
        json.dumps(
            {
                "base_token": "base",
                "overview_table": "overview",
                "product_table": "product",
                "identity": "user",
                "secret": "not-allowed",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unexpected"):
        load_feishu_target(path)


def test_lark_cli_client_reads_every_record_page(monkeypatch) -> None:
    monkeypatch.setattr("website_analytics.feishu_records._runtime", lambda: ["lark-cli"])
    commands = []

    def runner(command):
        commands.append(command)
        offset = int(command[command.index("--offset") + 1])
        if offset == 0:
            return {
                "ok": True,
                "data": {
                    "fields": ["周期键"],
                    "data": [["one"], ["two"]],
                    "record_id_list": ["rec-one", "rec-two"],
                    "has_more": True,
                },
            }
        return {
            "ok": True,
            "data": {
                "fields": ["周期键"],
                "data": [["three"]],
                "record_id_list": ["rec-three"],
                "has_more": False,
            },
        }

    client = LarkCLIRecordClient(
        FeishuTarget("base", "overview", "product", "user"), runner=runner
    )

    assert client.list_records("overview") == [
        ("rec-one", {"周期键": "one"}),
        ("rec-two", {"周期键": "two"}),
        ("rec-three", {"周期键": "three"}),
    ]
    assert [command[command.index("--offset") + 1] for command in commands] == ["0", "2"]
    assert all(command[command.index("--limit") + 1] == "200" for command in commands)


def test_lark_cli_client_creates_records_in_200_row_batches(monkeypatch) -> None:
    monkeypatch.setattr("website_analytics.feishu_records._runtime", lambda: ["lark-cli"])
    batch_sizes = []

    def runner(command):
        payload = json.loads(command[command.index("--json") + 1])
        batch_sizes.append(len(payload["rows"]))
        return {
            "ok": True,
            "data": {"record_id_list": [f"rec-{index}" for index in range(len(payload["rows"]))]},
        }

    client = LarkCLIRecordClient(
        FeishuTarget("base", "overview", "product", "user"), runner=runner
    )
    records = [{"周期键": f"key-{index}"} for index in range(401)]

    assert client.create_records("overview", records) == 401
    assert batch_sizes == [200, 200, 1]


def test_lark_cli_client_retries_transient_transport_failures(monkeypatch) -> None:
    monkeypatch.setattr("website_analytics.feishu_records._runtime", lambda: ["lark-cli"])
    responses = [
        SimpleNamespace(
            returncode=1,
            stdout=json.dumps({"ok": False, "error": {"message": "Patch request: EOF"}}),
            stderr="",
        ),
        SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {"ok": False, "error": {"message": "connection reset by peer"}}
            ),
            stderr="",
        ),
        SimpleNamespace(returncode=0, stdout=json.dumps({"ok": True, "data": {}}), stderr=""),
    ]
    sleeps = []
    monkeypatch.setattr(
        "website_analytics.feishu_records.subprocess.run", lambda *args, **kwargs: responses.pop(0)
    )
    monkeypatch.setattr("website_analytics.feishu_records.time.sleep", sleeps.append)
    client = LarkCLIRecordClient(FeishuTarget("base", "overview", "product", "user"))

    assert client._run(["lark-cli", "base", "+record-upsert"])["ok"] is True
    assert sleeps == [3, 6]
    assert responses == []


def test_lark_cli_client_does_not_retry_permission_failures(monkeypatch) -> None:
    monkeypatch.setattr("website_analytics.feishu_records._runtime", lambda: ["lark-cli"])
    calls = []

    def run(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(
            returncode=1,
            stdout=json.dumps(
                {"ok": False, "error": {"code": 99991672, "message": "permission denied"}}
            ),
            stderr="",
        )

    monkeypatch.setattr("website_analytics.feishu_records.subprocess.run", run)
    monkeypatch.setattr(
        "website_analytics.feishu_records.time.sleep",
        lambda seconds: pytest.fail(f"unexpected retry sleep: {seconds}"),
    )
    client = LarkCLIRecordClient(FeishuTarget("base", "overview", "product", "user"))

    with pytest.raises(RuntimeError, match="permission denied"):
        client._run(["lark-cli", "base", "+record-upsert"])

    assert len(calls) == 1


def test_lark_cli_environment_scopes_dedicated_proxy_to_subprocess(monkeypatch) -> None:
    monkeypatch.setenv("LARK_CLI_HTTP_PROXY", "http://127.0.0.1:8890")
    monkeypatch.setenv("LARK_CLI_HTTPS_PROXY", "http://127.0.0.1:8890")
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)

    environment = lark_cli_environment()

    assert environment["HTTP_PROXY"] == "http://127.0.0.1:8890"
    assert environment["HTTPS_PROXY"] == "http://127.0.0.1:8890"
    assert environment["http_proxy"] == "http://127.0.0.1:8890"
    assert environment["https_proxy"] == "http://127.0.0.1:8890"


def test_bitable_v1_fields_turns_base_datetimes_into_china_epoch_millis() -> None:
    fields = {"数据日期": "2026-11-16 00:00:00", "数据状态": "完整", "官网访问次数": 7, "备注": None}

    assert bitable_v1_fields(fields, {"数据日期", "备注"}) == {
        "数据日期": 1794758400000,
        "数据状态": "完整",
        "官网访问次数": 7,
        "备注": None,
    }
    with pytest.raises(ValueError, match="unexpected format"):
        bitable_v1_fields({"数据日期": "2026-11-16"}, {"数据日期"})


def _batch_client(monkeypatch, responses):
    monkeypatch.setattr("website_analytics.feishu_records._runtime", lambda: ["lark-cli"])
    calls = []

    def runner(command):
        calls.append(command)
        body = json.loads(command[command.index("--data") + 1])
        return responses(body)

    client = LarkCLIRecordClient(
        FeishuTarget("bascnBASE", "tblO", "tblP", "bot"),
        runner=runner,
        update_delay_seconds=0,
    )
    return client, calls


def test_update_records_sends_per_row_values_in_bounded_v1_batches(monkeypatch) -> None:
    monkeypatch.setattr("website_analytics.feishu_records._UPDATE_BATCH_RECORDS", 2)
    client, calls = _batch_client(
        monkeypatch, lambda body: {"ok": True, "data": {"records": list(reversed(body["records"]))}}
    )
    updates = [(f"rec{index}", {"日期": "2026-11-16 08:00:00", "次数": index}) for index in range(5)]

    assert client.update_records("tblP", updates, datetime_fields={"日期"}) == 5
    assert [command[1:4] for command in calls] == [
        ["api", "POST", "/open-apis/bitable/v1/apps/bascnBASE/tables/tblP/records/batch_update"]
    ] * 3
    assert calls[0][4:6] == ["--as", "bot"]
    sent = [record for command in calls for record in json.loads(command[-1])["records"]]
    assert [record["record_id"] for record in sent] == [f"rec{index}" for index in range(5)]
    assert [record["fields"]["次数"] for record in sent] == list(range(5))
    assert {record["fields"]["日期"] for record in sent} == {1794787200000}


def test_update_records_splits_by_payload_size(monkeypatch) -> None:
    monkeypatch.setattr("website_analytics.feishu_records._UPDATE_BATCH_BYTES", 120)
    client, calls = _batch_client(monkeypatch, lambda body: {"ok": True, "data": {"records": body["records"]}})
    updates = [(f"rec{index}", {"说明": "x" * 40}) for index in range(3)]

    assert client.update_records("tblP", updates) == 3
    assert [len(json.loads(command[-1])["records"]) for command in calls] == [1, 1, 1]


def test_update_records_fails_when_response_does_not_confirm_every_row(monkeypatch) -> None:
    client, _ = _batch_client(monkeypatch, lambda body: {"ok": True, "data": {"records": body["records"][:1]}})

    with pytest.raises(RuntimeError, match="does not confirm"):
        client.update_records("tblP", [("rec1", {"次数": 1}), ("rec2", {"次数": 2})])


def test_update_records_rejects_duplicate_record_ids_before_calling(monkeypatch) -> None:
    client, calls = _batch_client(monkeypatch, lambda body: pytest.fail("must not call"))

    with pytest.raises(ValueError, match="duplicate record IDs"):
        client.update_records("tblP", [("rec1", {"次数": 1}), ("rec1", {"次数": 2})])
    assert calls == []
