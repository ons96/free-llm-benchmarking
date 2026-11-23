"""Core speed-measurement primitives shared by all benchmark runners.

This module is the single source of truth for how TTFT, TPS and total
response time are measured in this repo. Both the main runner
(``runner.py``) and the standalone speedtest scripts consume it, so the
math is identical everywhere and unit-testable without any network.

Conventions
-----------
- TTFT  = seconds between request start and the *first streamed content
  chunk* (not the connection, not the role-only first chunk).
- TPS   = output tokens / generation window, where the window is the time
  between the first and last token for real streams. Buffered/fake
  streams (whole body in one burst) are detected and degrade to the
  conservative whole-call rate with ``ttft_sec = 0`` so they cannot
  fabricate millions of TPS.
- Tokens are counted from the provider ``usage`` block when present
  (``token_source="usage"``), falling back to a ~4-chars-per-token
  estimate (``token_source="estimated"``).
- Every measurement becomes a :class:`BenchmarkRecord` with an ISO-8601
  UTC timestamp, serializable to JSON and CSV.
"""

from __future__ import annotations

import csv
import json
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional

# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

CHARS_PER_TOKEN = 4


def now_iso() -> str:
    """Current UTC time as an ISO-8601 string with Z suffix."""
    return datetime.now(timezone.utc).isoformat()


def approx_tokens(text: str) -> int:
    """Quick fallback estimate: ~4 chars per token, minimum 1 for non-empty."""
    if not text:
        return 0
    return max(1, len(text) // CHARS_PER_TOKEN)


def tokens_from_usage(usage: Optional[dict]) -> Optional[int]:
    """Extract completion-token count from an OpenAI-compat usage block.

    Tries the field names seen across providers (OpenAI/Anthropic/groq/
    xinjianya style) and only returns a count when a positive integer is
    found. Returns ``None`` when the block is absent or unusable so the
    caller can fall back to the chars/4 estimate.
    """
    if not isinstance(usage, dict):
        return None
    for key in ("completion_tokens", "output_tokens", "outputTokens", "tokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    # OpenAI reasoning models: total minus prompt
    total = usage.get("total_tokens")
    prompt = usage.get("prompt_tokens") or usage.get("input_tokens")
    if isinstance(total, (int, float)) and isinstance(prompt, (int, float)) and total > prompt >= 0:
        return int(total - prompt)
    return None


def prompt_tokens_from_usage(usage: Optional[dict]) -> Optional[int]:
    """Extract prompt-token count from a usage block (None if absent)."""
    if not isinstance(usage, dict):
        return None
    for key in ("prompt_tokens", "input_tokens", "inputTokens"):
        value = usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            return int(value)
    return None


# ---------------------------------------------------------------------------
# SSE stream parsing
# ---------------------------------------------------------------------------

def parse_sse_line(line: str) -> Optional[dict]:
    """Parse one SSE line into a JSON payload dict.

    Returns ``None`` for anything that is not a ``data:`` line, the
    ``[DONE]`` sentinel, comments/keepalives, or malformed JSON (some
    providers emit those mid-stream; skipping is correct).
    """
    if not line or not line.startswith("data:"):
        return None
    payload = line[5:].strip()
    if not payload or payload == "[DONE]":
        return None
    try:
        return json.loads(payload)
    except json.JSONDecodeError:
        return None


def extract_delta_text(chunk: dict) -> str:
    """Return the generated text carried by a streaming chunk.

    Handles both ``content`` and ``reasoning`` deltas (reasoning-only
    providers stream reasoning first) and plain-text chunks.
    """
    choices = chunk.get("choices") or []
    if choices:
        delta = choices[0].get("delta") or {}
        text = delta.get("content") or delta.get("reasoning") or ""
        return text if isinstance(text, str) else ""
    return ""


# ---------------------------------------------------------------------------
# Stream timeline collection
# ---------------------------------------------------------------------------

@dataclass
class StreamStats:
    """Raw timeline facts collected while consuming one stream."""

    first_token_at: Optional[float] = None
    last_token_at: Optional[float] = None
    approx_token_count: int = 0
    collected_text: str = ""
    usage: Optional[dict] = None

    @property
    def saw_tokens(self) -> bool:
        return self.first_token_at is not None


class StreamCollector:
    """Feed parsed SSE chunks in; get the raw timeline out.

    ``clock`` is injectable so tests can simulate arbitrary inter-chunk
    delays deterministically (default: ``time.monotonic``).
    """

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.stats = StreamStats()
        self._clock = clock

    def observe(self, chunk: dict) -> None:
        """Record one parsed SSE chunk against the timeline."""
        text = extract_delta_text(chunk)
        if text:
            now = self._clock()
            stats = self.stats
            if stats.first_token_at is None:
                stats.first_token_at = now
            stats.last_token_at = now
            stats.approx_token_count += approx_tokens(text)
            stats.collected_text += text
        usage = chunk.get("usage")
        if isinstance(usage, dict) and usage:
            # Later usage chunks win (final chunk is authoritative).
            self.stats.usage = usage

    def observe_line(self, line: str) -> Optional[dict]:
        """Parse + observe one raw SSE line. Returns the payload if any."""
        payload = parse_sse_line(line)
        if payload is not None:
            self.observe(payload)
        return payload


@dataclass
class StreamMetrics:
    """Finalized measurement for a single call."""

    ttft_sec: Optional[float] = None
    tps: Optional[float] = None
    output_tokens: int = 0
    prompt_tokens: Optional[int] = None
    token_source: str = "estimated"  # "usage" | "estimated"


def finalize_metrics(
    stats: StreamStats,
    t_start: float,
    t_end: float,
) -> StreamMetrics:
    """Compute TTFT / TPS / totals from a collected timeline.

    Policy (kept identical to the battle-tested ``runner.py`` logic):

    - token count prefers the provider ``usage`` block, falling back to
      the chars/4 estimate accumulated while streaming;
    - TPS uses the first->last token window for real streams;
    - burst / fake-streaming detection: the entire response arriving in
      one SSE chunk (``stream_s < 0.5`` and generation time ~ 0 or TTFT
      consuming >95% of the call) degrades to ``tokens / total_time`` and
      zeroes TTFT, marking the measurement as non-streamed;
    - ``token_count <= 1`` yields TPS None (not enough signal).
    """
    usage_count = tokens_from_usage(stats.usage)
    token_count = usage_count if usage_count is not None else stats.approx_token_count
    token_source = "usage" if usage_count is not None else "estimated"

    metrics = StreamMetrics(
        output_tokens=token_count,
        prompt_tokens=prompt_tokens_from_usage(stats.usage),
        token_source=token_source,
    )

    if not stats.saw_tokens:
        return metrics

    total_time_sec = t_end - t_start
    ttft_sec = (stats.first_token_at - t_start) if stats.first_token_at is not None else None

    if ttft_sec is not None and token_count > 1:
        first, last = stats.first_token_at, stats.last_token_at
        stream_s = (last - first) if (last is not None and last > first) else 0.0
        gen_time_s = total_time_sec - ttft_sec

        burst_detected = stream_s < 0.5 and (
            gen_time_s < 0.05 or (total_time_sec > 0 and ttft_sec / total_time_sec > 0.95)
        )
        tps: Optional[float] = None
        if burst_detected:
            if total_time_sec > 0:
                tps = token_count / total_time_sec
            ttft_sec = 0.0
        elif stream_s >= 0.5:
            tps = token_count / stream_s
        elif gen_time_s > 0:
            tps = token_count / gen_time_s

        metrics.ttft_sec = ttft_sec
        metrics.tps = tps
    else:
        metrics.ttft_sec = ttft_sec

    return metrics


# ---------------------------------------------------------------------------
# Result records + JSON/CSV output
# ---------------------------------------------------------------------------

RECORD_FIELDS = [
    "run_id", "timestamp", "provider", "model", "base_url",
    "status", "ttft_sec", "tps", "output_tokens", "prompt_tokens",
    "total_time_sec", "token_source", "streaming", "reasoning_effort",
    "error_message",
]


@dataclass
class BenchmarkRecord:
    """One measured API call, provider-tagged and timestamped."""

    provider: str
    model: str
    base_url: str = ""
    status: str = "success"
    ttft_sec: Optional[float] = None
    tps: Optional[float] = None
    output_tokens: int = 0
    prompt_tokens: Optional[int] = None
    total_time_sec: Optional[float] = None
    token_source: str = "estimated"
    streaming: bool = True
    run_id: str = ""
    reasoning_effort: Optional[str] = None
    error_message: Optional[str] = None
    timestamp: str = field(default_factory=now_iso)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict) -> "BenchmarkRecord":
        known = {f for f in RECORD_FIELDS}
        return cls(**{k: v for k, v in data.items() if k in known})


def write_json(records: list[BenchmarkRecord], path: str | Path) -> Path:
    """Write records to a JSON array file (parent dirs auto-created)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([r.to_dict() for r in records], indent=2) + "\n")
    return path


def write_csv(records: list[BenchmarkRecord], path: str | Path, append: bool = False) -> Path:
    """Write records to CSV with a stable column order (append supported).

    On append, the existing file's header is validated against
    RECORD_FIELDS: after a schema change the stale header would silently
    misalign appended rows under the old column order (DictWriter writes
    in the new order). On mismatch the old file is rotated to
    ``<name>.stale-<timestamp>`` and a fresh file with the current header
    is started — data is never corrupted and never silently lost.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if append and path.exists() and path.stat().st_size > 0:
        with open(path, "r", newline="", encoding="utf-8") as f:
            existing_header = next(csv.reader(f), [])
        if existing_header != RECORD_FIELDS:
            rotated = path.with_name(
                f"{path.name}.stale-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            path.replace(rotated)
            append = False  # fresh file with the current header
    mode = "a" if append else "w"
    write_header = not (append and path.exists() and path.stat().st_size > 0)
    with open(path, mode, newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=RECORD_FIELDS, extrasaction="ignore")
        if write_header:
            writer.writeheader()
        for record in records:
            writer.writerow(record.to_dict())
    return path


def load_json(path: str | Path) -> list[BenchmarkRecord]:
    """Load records written by :func:`write_json`."""
    data = json.loads(Path(path).read_text())
    return [BenchmarkRecord.from_dict(d) for d in data]


# ---------------------------------------------------------------------------
# Transport-level measurement (httpx) — used by speedtest scripts + tests
# ---------------------------------------------------------------------------

async def measure_openai_stream(
    client: Any,
    url: str,
    body: dict,
    headers: Optional[dict] = None,
    timeout: float = 60.0,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Run one streaming OpenAI-compat chat completion and measure it.

    ``client`` is an ``httpx.AsyncClient`` (tests inject one backed by
    ``httpx.MockTransport`` so this runs fully offline). Returns a dict
    with the finalized metrics plus status/error information — the same
    vocabulary as ``runner.TestResult`` statuses: ``success``, ``empty``,
    ``http_error``, ``timeout``, ``error``.
    """
    import httpx

    stream_body = dict(body, stream=True)
    t_start = clock()
    collector = StreamCollector(clock=clock)
    try:
        async with client.stream(
            "POST", url, json=stream_body, headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code != 200:
                body_bytes = b""
                async for chunk in resp.aiter_bytes():
                    body_bytes += chunk
                    if len(body_bytes) > 500:
                        break
                return {
                    "status": "http_error",
                    "error_message": f"HTTP {resp.status_code}: {body_bytes[:300].decode(errors='replace')}",
                    "total_time_sec": clock() - t_start,
                }

            async for line in resp.aiter_lines():
                collector.observe_line(line)

    except httpx.TimeoutException:
        return {
            "status": "timeout",
            "error_message": f"Timeout after {timeout}s",
            "total_time_sec": clock() - t_start,
        }
    except httpx.HTTPError as e:
        return {
            "status": "http_error",
            "error_message": str(e)[:300],
            "total_time_sec": clock() - t_start,
        }

    t_end = clock()
    stats = collector.stats
    if not stats.saw_tokens:
        return {
            "status": "empty",
            "error_message": "No content tokens received",
            "total_time_sec": t_end - t_start,
        }

    metrics = finalize_metrics(stats, t_start, t_end)
    return {
        "status": "success",
        "ttft_sec": metrics.ttft_sec,
        "tps": metrics.tps,
        "output_tokens": metrics.output_tokens,
        "prompt_tokens": metrics.prompt_tokens,
        "token_source": metrics.token_source,
        "total_time_sec": t_end - t_start,
        "raw_sample": stats.collected_text[:200],
        "error_message": None,
    }
