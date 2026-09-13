"""Submit one paper through the live API and wait for its resumable job."""

from __future__ import annotations

import argparse
import json
import time
import urllib.error
import urllib.request

from mouseion.config import get_settings


def _request(url: str, *, token: str, body: dict[str, str] | None = None) -> dict:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        url,
        data=data,
        headers={
            "Authorization": f"Bearer {token}",
            **({"Content-Type": "application/json"} if data is not None else {}),
        },
        method="POST" if data is not None else "GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.loads(response.read())
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:500]
        raise RuntimeError(f"Mouseion returned HTTP {exc.code}: {detail}") from exc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("url", nargs="?", default="https://arxiv.org/abs/1706.03762")
    parser.add_argument("--api-base", default="http://127.0.0.1:8000")
    parser.add_argument("--timeout", type=int, default=900)
    args = parser.parse_args()
    token = get_settings().api_token
    if not token:
        raise SystemExit("API_TOKEN is not configured")
    accepted = _request(f"{args.api_base}/api/papers", token=token, body={"url": args.url})
    job_id = str(accepted["job_id"])
    print(f"queued {job_id}")
    deadline = time.monotonic() + args.timeout
    previous = ""
    while time.monotonic() < deadline:
        job = _request(f"{args.api_base}/api/jobs/{job_id}", token=token)
        state = str(job["state"])
        if state != previous:
            print(state)
            previous = state
        if state == "done":
            print(f"paper ready: /papers/{job['paper_id']}")
            return 0
        if state == "failed":
            raise SystemExit(f"ingest failed: {job.get('error') or 'unknown error'}")
        time.sleep(2)
    raise SystemExit(f"timed out waiting for {job_id}")


if __name__ == "__main__":
    raise SystemExit(main())
