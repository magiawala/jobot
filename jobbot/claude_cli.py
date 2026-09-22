"""The ONLY module that shells out to the `claude` binary."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from typing import Any

from . import config, db, log

logger = log.get("claude")

DEFAULT_TIMEOUT = 180


class ClaudeUnavailable(Exception):
    """Usage limit, auth problem, or binary missing. Callers must fall back, not crash."""


class ClaudeBadOutput(Exception):
    """The model answered but not with the JSON we asked for."""


def _clean_env() -> dict[str, str]:
    env = dict(os.environ)
    env.pop("ANTHROPIC_API_KEY", None)
    env.pop("ANTHROPIC_AUTH_TOKEN", None)
    env.setdefault("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    return env


def binary() -> str | None:
    return shutil.which("claude") or (os.path.expanduser("~/.local/bin/claude") if os.path.exists(os.path.expanduser("~/.local/bin/claude")) else None)


def check_cli() -> str:
    b = binary()
    if not b:
        return "NOT FOUND"
    try:
        out = subprocess.run([b, "--version"], capture_output=True, text=True, timeout=20, env=_clean_env())
        return f"ok ({out.stdout.strip() or out.stderr.strip()})"
    except Exception as e:  # noqa: BLE001
        return f"error: {e}"


_LIMIT_PATTERNS = re.compile(
    r"(usage limit|rate limit|limit reached|too many requests|out of (?:credits|quota)|not logged in|login required|"
    r"please run /login|authentication|unauthorized|invalid api key|subscription|overloaded|529|429)", re.I)


def call(prompt: str, purpose: str, job_id: int | None = None, timeout: int = DEFAULT_TIMEOUT,
         system: str | None = None, model: str | None = None) -> str:
    """Run `claude -p` with the prompt on stdin. Returns the model's text. Logs to claude_calls."""
    b = binary()
    if not b:
        raise ClaudeUnavailable("claude CLI not found on PATH")
    limits = config.limits()
    with db.session() as conn:
        used = db.claude_calls_today(conn)
    if used >= limits["max_claude_calls_per_day"]:
        raise ClaudeUnavailable(f"daily Claude call cap reached ({used}/{limits['max_claude_calls_per_day']})")

    cmd = [b, "-p", "--output-format", "json", "--no-session-persistence"]
    if system:
        cmd += ["--append-system-prompt", system]
    if model:
        cmd += ["--model", model]
    t0 = time.time()
    err: str | None = None
    text = ""
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout,
                              env=_clean_env(), cwd=str(config.ROOT))
        raw = proc.stdout.strip()
        if proc.returncode != 0 or not raw:
            err = (proc.stderr or raw or f"exit {proc.returncode}").strip()[:500]
            if _LIMIT_PATTERNS.search(err):
                raise ClaudeUnavailable(err)
            raise ClaudeBadOutput(err)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            data = None
        if isinstance(data, dict):
            if data.get("is_error") or data.get("subtype", "").startswith("error"):
                msg = str(data.get("result") or data.get("error") or data)[:500]
                err = msg
                if _LIMIT_PATTERNS.search(msg):
                    raise ClaudeUnavailable(msg)
                raise ClaudeBadOutput(msg)
            text = str(data.get("result") or "")
        else:
            text = raw
        if _LIMIT_PATTERNS.search(text[:300]) and len(text) < 400:
            err = text[:500]
            raise ClaudeUnavailable(text[:500])
        return text
    except subprocess.TimeoutExpired:
        err = f"timeout after {timeout}s"
        raise ClaudeUnavailable(err)
    finally:
        dur = time.time() - t0
        with db.session() as conn:
            db.log_claude_call(conn, purpose, job_id, err is None, round(dur, 2), err)
        logger.info("claude %s job=%s ok=%s %.1fs%s", purpose, job_id, err is None, dur, f" err={err[:120]}" if err else "")


def extract_json(text: str) -> Any:
    """Pull the first JSON object/array out of model text (handles code fences and chatter)."""
    t = text.strip()
    m = re.search(r"```(?:json)?\s*(.*?)```", t, re.S)
    if m:
        t = m.group(1).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        i = t.find(open_c)
        j = t.rfind(close_c)
        if i != -1 and j > i:
            try:
                return json.loads(t[i:j + 1])
            except json.JSONDecodeError:
                continue
    raise ClaudeBadOutput("no JSON found in model output: " + t[:200])


def call_json(prompt: str, purpose: str, job_id: int | None = None, **kw: Any) -> Any:
    return extract_json(call(prompt, purpose, job_id, **kw))
