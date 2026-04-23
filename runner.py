"""Async streaming speed test runner."""

import asyncio
import json
import time
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Optional

import httpx

from config import Target, RATE_LIMITS
import db

# Minimal prompts for token-efficient testing
SYSTEM_PROMPT = "You are a concise coding assistant."
USER_PROMPT = "Write a Python merge sort function with type hints."
# ~20 input tokens total

MAX_TOKENS = 100  # Reduced for faster testing
TIMEOUT_NORMAL = 30
TIMEOUT_REASONING = 120
RETRY_COUNT = 2
RUNS_PER_TARGET = 3
MAX_CONCURRENT = 2
INTER_CALL_DELAY = 2.0  # seconds between calls to same provider

# Track provider call times for rate limiting
_call_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
# Track providers with exhausted credits (skip all their models)
_credit_exhausted: set[str] = set()


def _check_rate_limit(provider_name: str) -> float:
    limit = RATE_LIMITS.get(provider_name)
    if not limit:
        return 0.0

    now = time.time()
    history = _call_history[provider_name]

    while history and now - history[0] >= 60:
        history.popleft()

    if len(history) >= limit:
        oldest = history[0]
        return max(0.0, 60.0 - (now - oldest))

    return 0.0


def _record_call(provider_name: str) -> None:
    _call_history[provider_name].append(time.time())


def parse_error_for_wait_time(err: str) -> float:
    """Parse retry-after or quota reset time from error message. Returns wait in seconds."""
    import re

    err_lower = err.lower()

    # Check for retry-after
    m = re.search(r"retry[- ]after[:\s]*(\d+)", err_lower)
    if m:
        return float(m.group(1))

    # Check for seconds
    m = re.search(r"(\d+)\s*sec(?:ond)?s?", err_lower)
    if m:
        return float(m.group(1))

    # Check for quota reset time like "at 2024-01-01T00:00:00Z"
    m = re.search(r"\d{4}-\d{2}-\d{2}T(\d{2}):(\d{2}):(\d{2})", err)
    if m:
        from datetime import datetime

        try:
            reset_time = datetime.fromisoformat(m.group(0).replace("Z", "+00:00"))
            wait = (reset_time - datetime.now()).total_seconds()
            return max(1.0, wait)
        except Exception:
            pass

    return 0.0


def _record_call(provider_name: str):
    """Record a call for rate limiting."""
    _call_history[provider_name].append(time.time())


def set_credit_exhausted(provider_name: str):
    """Mark a provider as having exhausted credits."""
    _credit_exhausted.add(provider_name)


def is_credit_exhausted(provider_name: str) -> bool:
    """Check if a provider has exhausted credits."""
    return provider_name in _credit_exhausted


@dataclass
class TestResult:
    ttft_ms: Optional[float] = None
    tps: Optional[float] = None
    output_tokens: int = 0
    total_time_ms: Optional[float] = None
    status: str = "error"
    error_message: Optional[str] = None
    raw_sample: str = ""


async def test_single_call(
    client: httpx.AsyncClient,
    target: Target,
    reasoning_effort: Optional[str] = None,
) -> TestResult:
    """Make a single streaming API call and measure TTFT + TPS."""
    from config import NO_STREAM_PROVIDERS, STREAM_MODEL_OVERRIDES

    stream_key = f"{target.provider_name}/{target.model_name}"
    use_stream = (
        target.provider_name not in NO_STREAM_PROVIDERS
        or stream_key in STREAM_MODEL_OVERRIDES
    )

    url = f"{target.base_url.rstrip('/')}/chat/completions"

    body: dict = {
        "model": target.model_name,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": USER_PROMPT},
        ],
        "max_tokens": MAX_TOKENS,
        "stream": use_stream,
        "temperature": 0.0,
    }

    if reasoning_effort and target.supports_reasoning:
        body["reasoning_effort"] = reasoning_effort

    headers = {"Content-Type": "application/json"}
    if target.api_key:
        headers["Authorization"] = f"Bearer {target.api_key}"

    timeout = (
        TIMEOUT_REASONING
        if (reasoning_effort and target.supports_reasoning)
        else TIMEOUT_NORMAL
    )

    t_start = time.monotonic()
    first_token_time: Optional[float] = None
    collected_text = ""
    token_count = 0

    NON_STREAM_TOKENS = 500  # More tokens for non-streaming TPS measurement
    body_stream = dict(body, stream=True)
    body_nonstream = dict(body, stream=False, max_tokens=NON_STREAM_TOKENS)

    if not use_stream:
        try:
            resp = await client.post(
                url, json=body_nonstream, headers=headers, timeout=timeout
            )
            if resp.status_code != 200:
                return TestResult(
                    status="http_error",
                    error_message=f"HTTP {resp.status_code}: {resp.text[:300]}",
                    total_time_ms=(time.monotonic() - t_start) * 1000,
                )
            data = resp.json()
            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            reasoning = (
                data.get("choices", [{}])[0].get("message", {}).get("reasoning", "")
            )
            full_text = content or reasoning or ""
            collected_text = full_text
            usage = data.get("usage", {})
            token_count = (
                usage.get("completion_tokens")
                or usage.get("output_tokens")
                or usage.get("tokens")
                or _approx_tokens(full_text)
            )
            total_ms = (time.monotonic() - t_start) * 1000
            gen_s = total_ms / 1000
            tps = float(token_count) / gen_s if token_count and gen_s > 0 else None
            return TestResult(
                ttft_ms=0,
                tps=tps,
                output_tokens=token_count,
                total_time_ms=total_ms,
                status="success" if token_count else "empty",
                raw_sample=collected_text[:200],
            )
        except Exception as e:
            return TestResult(
                status="error",
                error_message=str(e)[:200],
                total_time_ms=(time.monotonic() - t_start) * 1000,
            )

    try:
        async with client.stream(
            "POST", url, json=body_stream, headers=headers, timeout=timeout
        ) as resp:
            if resp.status_code != 200:
                body_bytes = b""
                async for chunk in resp.aiter_bytes():
                    body_bytes += chunk
                    if len(body_bytes) > 500:
                        break
                return TestResult(
                    status="http_error",
                    error_message=f"HTTP {resp.status_code}: {body_bytes[:300].decode(errors='replace')}",
                    total_time_ms=(time.monotonic() - t_start) * 1000,
                )

            async for line in resp.aiter_lines():
                if not line.startswith("data: "):
                    continue
                data_str = line[6:].strip()
                if data_str == "[DONE]":
                    break

                try:
                    chunk_data = json.loads(data_str)
                except json.JSONDecodeError:
                    continue

                # Extract content delta (handle both content and reasoning fields)
                choices = chunk_data.get("choices", [])
                if not choices:
                    continue
                delta = choices[0].get("delta", {})
                content = delta.get("content", "") or delta.get("reasoning", "")

                if content:
                    now = time.monotonic()
                    if first_token_time is None:
                        first_token_time = now
                    last_token_time = now
                    token_count += _approx_tokens(content)
                    collected_text += content

                # Check for usage in final chunk (various API formats)
                usage = chunk_data.get("usage", {})
                if usage:
                    # Try different field names
                    token_count = (
                        usage.get("completion_tokens")
                        or usage.get("output_tokens")
                        or usage.get("tokens")
                        or token_count  # fallback to approximation
                    )

    except httpx.TimeoutException:
        return TestResult(
            status="timeout",
            error_message=f"Timeout after {timeout}s",
            total_time_ms=(time.monotonic() - t_start) * 1000,
        )
    except httpx.HTTPError as e:
        return TestResult(
            status="http_error",
            error_message=str(e)[:300],
            total_time_ms=(time.monotonic() - t_start) * 1000,
        )
    except Exception as e:
        return TestResult(
            status="error",
            error_message=f"{type(e).__name__}: {str(e)[:200]}",
            total_time_ms=(time.monotonic() - t_start) * 1000,
        )

    t_end = time.monotonic()
    total_ms = (t_end - t_start) * 1000

    if first_token_time is None:
        return TestResult(
            status="empty",
            error_message="No content tokens received",
            total_time_ms=total_ms,
        )

    ttft_ms = (first_token_time - t_start) * 1000

    # TPS: tokens generated / generation time
    # Use time between first and last token if stream is long enough (>0.5s).
    # Otherwise fall back to total_time - ttft to avoid absurd values from
    # buffered/chunked responses where all content arrives in one burst.
    tps = None
    if token_count > 1:
        stream_s = (
            (last_token_time - first_token_time)
            if last_token_time and last_token_time > first_token_time
            else 0
        )
        gen_time_s = (total_ms - ttft_ms) / 1000  # fallback: wall-clock minus TTFT

        if stream_s >= 0.5:
            # Genuine streaming — use inter-token timing
            tps = token_count / stream_s
        elif gen_time_s > 0.05:
            # Buffered response — use wall-clock generation window
            tps = token_count / gen_time_s
        # else: too fast to measure reliably, leave as None

    return TestResult(
        ttft_ms=ttft_ms,
        tps=tps,
        output_tokens=token_count,
        total_time_ms=total_ms,
        status="success",
        raw_sample=collected_text[:200],
    )


def _approx_tokens(text: str) -> int:
    """Quick approximation: ~4 chars per token."""
    return max(1, len(text) // 4)


async def run_target(
    target: Target,
    run_id: str,
    num_runs: int = RUNS_PER_TARGET,
    reasoning_effort: Optional[str] = None,
    conn: Optional[object] = None,
) -> list[TestResult]:
    """Run num_runs tests against a single target, saving results to DB."""
    # Skip if credits exhausted
    if is_credit_exhausted(target.provider_name):
        return []

    # Check rate limit before making call
    wait_time = _check_rate_limit(target.provider_name)
    if wait_time > 0:
        await asyncio.sleep(wait_time)

    results: list[TestResult] = []
    effort = reasoning_effort if target.supports_reasoning else None

    async with httpx.AsyncClient(follow_redirects=True) as client:
        for i in range(num_runs):
            for attempt in range(1 + RETRY_COUNT):
                result = await test_single_call(client, target, effort)
                if result.status == "success" or result.status in ("timeout", "empty"):
                    break
                if attempt < RETRY_COUNT and result.status == "http_error":
                    err = result.error_message or ""
                    # Check for credit exhaustion (403)
                    if "403" in err and "credit" in err.lower():
                        set_credit_exhausted(target.provider_name)
                        break
                    if (
                        "429" in err
                        or "500" in err
                        or "502" in err
                        or "503" in err
                        or "504" in err
                    ):
                        # Parse wait time from error, default 5s for 429, 2s for others
                        wait = parse_error_for_wait_time(err)
                        if wait == 0.0:
                            wait = 5 if "429" in err else 2
                        await asyncio.sleep(wait)
                        continue
                    break  # 4xx non-429, don't retry

            # Check final status and mark credit exhaustion
            if result.status == "http_error":
                err = result.error_message or ""
                if "403" in err and "credit" in err.lower():
                    set_credit_exhausted(target.provider_name)

            # Record call for rate limiting
            _record_call(target.provider_name)

            # Save to DB
            if conn:
                db.insert_test(
                    conn,
                    run_id=run_id,
                    timestamp=db.now_iso(),
                    provider_name=target.provider_name,
                    provider_url=target.base_url,
                    model_name=target.model_name,
                    source=target.source,
                    reasoning_effort=effort,
                    ttft_ms=result.ttft_ms,
                    tps=result.tps,
                    output_tokens=result.output_tokens,
                    total_time_ms=result.total_time_ms,
                    status=result.status,
                    error_message=result.error_message,
                    run_number=i + 1,
                    raw_sample=result.raw_sample,
                )
                conn.commit()

            if i < num_runs - 1:
                await asyncio.sleep(INTER_CALL_DELAY)

    return results


def compute_summary(
    results: list[TestResult],
    target: Target,
    run_id: str,
    reasoning_effort: Optional[str],
    conn,
) -> None:
    """Compute and store aggregate summary from individual test results."""
    successful = [r for r in results if r.status == "success"]
    if not successful:
        db.insert_summary(
            conn,
            run_id=run_id,
            timestamp=db.now_iso(),
            provider_name=target.provider_name,
            provider_url=target.base_url,
            model_name=target.model_name,
            source=target.source,
            reasoning_effort=reasoning_effort if target.supports_reasoning else None,
            avg_ttft_ms=None,
            avg_tps=None,
            avg_total_time_ms=None,
            est_10k_total_s=None,
            num_runs=len(results),
            num_success=0,
        )
        conn.commit()
        return

    ttfts = [r.ttft_ms for r in successful if r.ttft_ms is not None]
    tps_vals = [r.tps for r in successful if r.tps is not None and r.tps > 0]
    totals = [r.total_time_ms for r in successful if r.total_time_ms is not None]

    avg_ttft = sum(ttfts) / len(ttfts) if ttfts else None
    avg_tps = sum(tps_vals) / len(tps_vals) if tps_vals else None
    avg_total = sum(totals) / len(totals) if totals else None

    # Estimated time to stream 10K tokens (agentic call proxy)
    est_10k = None
    if avg_ttft is not None and avg_tps and avg_tps > 0:
        est_10k = (avg_ttft / 1000) + (10000 / avg_tps)

    db.insert_summary(
        conn,
        run_id=run_id,
        timestamp=db.now_iso(),
        provider_name=target.provider_name,
        provider_url=target.base_url,
        model_name=target.model_name,
        source=target.source,
        reasoning_effort=reasoning_effort if target.supports_reasoning else None,
        avg_ttft_ms=avg_ttft,
        avg_tps=avg_tps,
        avg_total_time_ms=avg_total,
        est_10k_total_s=est_10k,
        num_runs=len(results),
        num_success=len(successful),
    )
    conn.commit()


async def run_all(
    targets: list[Target],
    num_runs: int = RUNS_PER_TARGET,
    reasoning_effort: str = "medium",
    max_concurrent: int = MAX_CONCURRENT,
    effort_sweep: bool = False,
    on_progress=None,
) -> str:
    """Run speed tests against all targets with concurrency limit and progress bar."""
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    run_id = str(uuid.uuid4())[:8]
    conn = db.connect()
    sem = asyncio.Semaphore(max_concurrent)

    efforts_for = lambda t: (
        (["low", "medium", "high"] if effort_sweep else [reasoning_effort])
        if t.supports_reasoning
        else [None]
    )

    total_jobs = sum(len(efforts_for(t)) for t in targets)
    done = 0
    start_time = asyncio.get_event_loop().time()

    pbar = None
    if tqdm:
        pbar = tqdm(total=total_jobs, desc="Speed tests", unit="model")

    async def worker(target: Target, effort: Optional[str]):
        nonlocal done, start_time
        async with sem:
            results = await run_target(target, run_id, num_runs, effort, conn)
            compute_summary(results, target, run_id, effort, conn)
            done += 1
            if pbar:
                elapsed = asyncio.get_event_loop().time() - start_time
                rate = done / elapsed if elapsed > 0 else 1
                eta = (total_jobs - done) / rate
                pbar.set_postfix(eta=f"{eta / 60:.1f}min")
                pbar.update(1)
            if on_progress:
                on_progress(done, total_jobs, target, effort, results)

    tasks = []
    for target in targets:
        for effort in efforts_for(target):
            tasks.append(worker(target, effort))

    await asyncio.gather(*tasks, return_exceptions=True)
    if pbar:
        pbar.close()
    conn.close()
    return run_id
