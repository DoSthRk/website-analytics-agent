"""Select the stable automatic proxy group used by server-side Feishu calls."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


def _arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--controller", default="http://127.0.0.1:9091")
    parser.add_argument("--group", default="GLOBAL")
    parser.add_argument("--choice", default="自动选择")
    return parser.parse_args()


def _request(request: Request) -> bytes:
    with urlopen(request, timeout=5) as response:  # noqa: S310 - fixed local controller
        return response.read()


def select_proxy(controller: str, group: str, choice: str) -> dict[str, Any]:
    endpoint = f"{controller.rstrip('/')}/proxies/{quote(group, safe='')}"
    payload = json.dumps({"name": choice}, ensure_ascii=False).encode("utf-8")
    last_error: Exception | None = None
    for attempt in range(5):
        try:
            _request(
                Request(
                    endpoint,
                    data=payload,
                    method="PUT",
                    headers={"Content-Type": "application/json"},
                )
            )
            state = json.loads(_request(Request(endpoint)))
            if not isinstance(state, dict) or state.get("now") != choice:
                raise RuntimeError("proxy controller did not confirm the requested selection")
            return {"group": group, "choice": choice}
        except (OSError, URLError, json.JSONDecodeError, RuntimeError) as error:
            last_error = error
            if attempt < 4:
                time.sleep(attempt + 1)
    raise RuntimeError("unable to configure the server proxy") from last_error


def main() -> int:
    args = _arguments()
    print(json.dumps(select_proxy(args.controller, args.group, args.choice), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
