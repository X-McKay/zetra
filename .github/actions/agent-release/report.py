"""Tell the Agent Registry what the release published (contract/delivery.md section 3.3).

Run as the last step of the action when `registry-url` is set. It asks GitHub for an identity
token for the registry's audience, posts the published Agent and AgentRelease (and the
evaluation suite, when the agent has one), and exits by the table:

  200               exit 0, prints deployment, release and revision
  409 wrong-path    exit 0, says the agent is on the pull request path
  anything else     exit 1, prints the code and the message

The release is already published; a failure here does not withdraw it. The token is never
printed. HTTP and the clock are arguments of `run`, so a test can pass fakes.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import yaml

PATH = "/v1/reports/releases"
# Five retries over about a minute: 2 + 4 + 8 + 16 + 30 seconds.
DELAYS = (2, 4, 8, 16, 30)
TIMEOUT_SECONDS = 20

# (method, url, headers, body) -> (status, body). A network error raises OSError.
Http = Callable[[str, str, Mapping[str, str], bytes | None], tuple[int, bytes]]


def urllib_http(
    method: str, url: str, headers: Mapping[str, str], body: bytes | None
) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)  # noqa: S310
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as answer:  # noqa: S310
            return answer.status, answer.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()
    except urllib.error.URLError as error:
        raise OSError(str(error.reason)) from None


def _retrying(call: Callable[[], tuple[int, bytes]], sleep: Callable[[float], None]):
    """Call until it answers below 500, retrying a network error or a 5xx after each delay."""
    for delay in (*DELAYS, None):
        try:
            status, body = call()
        except OSError:
            status, body = 0, b""
        if 0 < status < 500 or delay is None:
            return status, body
        sleep(delay)
    raise AssertionError("unreachable")


def _error(body: bytes, status: int) -> tuple[str, str]:
    """The code and message of the registry's error body, or what a stranger's answer was."""
    try:
        error = json.loads(body)["error"]
        return str(error["code"]), str(error["message"])
    except (ValueError, KeyError, TypeError):
        return f"http-{status}" if status else "no-answer", "the registry gave no usable answer"


def build_body(agent: str, out: Path, suite_path: Path) -> dict[str, Any]:
    """The report from the files the release published and the agent's suite, if it has one."""
    files = {name: (out / name).read_bytes() for name in ("agent.yaml", "agentrelease.yaml")}
    body: dict[str, Any] = {
        "agent": yaml.safe_load(files["agent.yaml"]),
        "agentRelease": yaml.safe_load(files["agentrelease.yaml"]),
        "lineage": {
            "checksums": {
                name: "sha256:" + hashlib.sha256(data).hexdigest() for name, data in files.items()
            }
        },
    }
    if suite_path.is_file():
        body["evaluation"] = {"suite": yaml.safe_load(suite_path.read_text())}
    return body


def run(
    env: Mapping[str, str],
    *,
    http: Http = urllib_http,
    sleep: Callable[[float], None] = time.sleep,
    out=sys.stdout,
    err=sys.stderr,
) -> int:
    registry = env.get("REGISTRY_URL", "").rstrip("/")
    if not registry:
        print("registry-url is empty: nothing to report", file=out)
        return 0
    agent = env["AGENT"]
    request_url, request_token = (
        env.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""),
        env.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN", ""),
    )
    if not request_url or not request_token:
        print(
            "no identity token is offered: the job needs `permissions: id-token: write`", file=err
        )
        return 1
    try:
        body = build_body(
            agent,
            Path(env.get("OUT") or f"dist/{agent}"),
            Path(f"agents/{agent}/evaluation/suite.yaml"),
        )
    except (OSError, yaml.YAMLError) as error:
        print(f"cannot build the report: {type(error).__name__}", file=err)
        return 1

    audience = env.get("REGISTRY_AUDIENCE", "")
    separator = "&" if "?" in request_url else "?"
    asked = f"{request_url}{separator}audience={urllib.parse.quote(audience, safe='')}"
    status, answer = _retrying(
        lambda: http("GET", asked, {"Authorization": f"bearer {request_token}"}, None), sleep
    )
    try:
        token = json.loads(answer)["value"] if status == 200 else ""
    except (ValueError, KeyError, TypeError):
        token = ""
    if not token:
        print(f"GitHub gave no identity token (status {status})", file=err)
        return 1

    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    payload = json.dumps(body).encode()
    status, answer = _retrying(lambda: http("POST", registry + PATH, headers, payload), sleep)
    if status == 200:
        try:
            done = json.loads(answer)
            print(
                f"reported: deployment {done['deployment']}, release {done['release']}, "
                f"revision {done['revision']}" + ("" if done.get("created") else " (already)"),
                file=out,
            )
            return 0
        except (ValueError, KeyError, TypeError):
            print("the registry answered 200 with an answer this step cannot read", file=err)
            return 1
    code, message = _error(answer, status)
    if status == 409 and code == "wrong-path":
        print(f"not reported: {agent} is on the pull request path ({message})", file=out)
        return 0
    print(f"the registry refused the report: {code}: {message}", file=err)
    return 1


if __name__ == "__main__":
    sys.exit(run(os.environ))
