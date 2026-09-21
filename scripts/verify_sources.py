# 用真实数据快照验证只读查询，并对比访问前后的文件摘要。
"""Verify registered reads against the real dataset without changing it or calling a model."""

import json
import sqlite3
from pathlib import Path
from typing import cast

from flowpay.core.data_sources import DataSources
from flowpay.core.json import require_object
from flowpay.dataset import require_dataset

root = Path(__file__).resolve().parents[1]
dataset = require_dataset() / "data"
sources = DataSources.from_config(root / "backend/policies/sources.toml", dataset)
before = sources.verify()
path = dataset / "generated/canonical/flowpay.sqlite"
with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
    row = cast(
        tuple[str, str, str],
        db.execute(
            "SELECT account_id,environment,payment_id FROM payments LIMIT 1"
        ).fetchone(),
    )
account_id, environment, payment_id = row
result = sources.sql(
    "payments",
    {"account_id": account_id, "environment": environment},
    {"payment_id": payment_id},
)
assert len(result["rows"]) == 1
for name in sources.policy.get("logs", {}):
    result = sources.logs(name, limit=2)
    assert result["rows"]
    assert all(
        "scenario_id" not in require_object(item["record"]) for item in result["rows"]
    )
try:
    sources.logs("audit/event_journal.jsonl")
except ValueError:
    pass
else:
    raise AssertionError("Unregistered audit source was accepted")
assert sources.verify() == before
print(
    json.dumps(
        {
            "status": "passed",
            "release_id": before["dataset_release_id"],
            "sql_operations": len(sources.policy.get("sql", {})),
            "log_sources": len(sources.policy.get("logs", {})),
            "source_hashes_unchanged": True,
        },
        ensure_ascii=False,
    )
)
