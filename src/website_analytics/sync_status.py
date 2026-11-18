"""Local run status and coverage of the selected V3 batch, not remote history."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Mapping, Sequence


def daily_coverage(planned: Sequence[Mapping[str, Any]], generated: Sequence[str]) -> dict[str, Any]:
    expected = sorted({str(row['start']) for row in planned if row.get('kind') == 'day' and row.get('isFinal') is True})
    observed = sorted(set(generated))
    if not set(observed) <= set(expected):
        raise ValueError('generated dates are outside the selected finalized days')
    # Include holes in the selected interval, rather than claiming a missing
    # selected row means there was no website traffic.
    interval = []
    if expected:
        current, end = date.fromisoformat(expected[0]), date.fromisoformat(expected[-1])
        while current <= end:
            interval.append(current.isoformat())
            current += timedelta(days=1)
    return {
        'scope': 'selected_v3_batch',
        'selected_dates': expected,
        'generated_dates': observed,
        'missing_dates': sorted(set(interval) - set(observed)),
        'expected_days': len(interval),
        'complete_days': len(observed),
        'start': expected[0] if expected else None,
        'end': expected[-1] if expected else None,
        'remote_history_coverage': 'not_verified',
        'finality_basis': 'configured_source_lag_estimate',
    }


def save_status(output_dir: Path, run_dir: Path, status: Mapping[str, Any]) -> None:
    """Persist diagnostics without source payloads, URLs or exception messages."""
    text = json.dumps(status, ensure_ascii=False, indent=2, allow_nan=False) + '\n'
    for path in (run_dir / 'run-status.json', output_dir / 'latest-run-status.json'):
        temporary = path.with_suffix('.json.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)


def previous_success(output_dir: Path) -> str | None:
    try:
        value = json.loads((output_dir / 'latest-run-status.json').read_text(encoding='utf-8'))
    except (OSError, ValueError):
        return None
    result = value.get('last_successful_check_at') if isinstance(value, dict) else None
    if not isinstance(result, str):
        return None
    try:
        parsed = datetime.fromisoformat(result.replace("Z", "+00:00"))
    except ValueError:
        return None
    return result if parsed.tzinfo is not None else None
