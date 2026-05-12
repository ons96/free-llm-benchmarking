"""Async streaming speed test runner."""

import asyncio
import json
import sys
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

MAX_TOKENS = 4000
TIMEOUT_NORMAL = 60
TIMEOUT_REASONING = 180
RETRY_COUNT = 2
RUNS_PER_TARGET = 1
MAX_CONCURRENT = 5
INTER_CALL_DELAY = 0

# Providers that require temperature > 0 (e.g. xinjianya)
MIN_TEMPERATURE_PROVIDERS = {"xinjianya"}

# Providers that don't accept stream_options field in request body
NO_STREAM_OPTIONS_PROVIDERS = {"xinjianya"}

SKIP_MODEL_TYPES = {
    "image", "vision", "tts", "speech", "audio",
    "safety", "content-safety", "moderation", "embed", "rerank",
}

SKIP_MODEL_NAME_PATTERNS = [
    "content-safety",
    "safety-guard",
    "safety-reasoning",
    "topic-control",
    "translation",
    "calibration",
    "nemoguard",
    "riva-translate",
]


def _should_skip_model(target: Target) -> bool:
    if target.model_type.lower() in SKIP_MODEL_TYPES:
        return True
    name_lower = target.model_name.lower()
    for pat in SKIP_MODEL_NAME_PATTERNS:
        if pat in name_lower:
            return True
    return False

REASONING_HIGHEST = {
    "gpt-5": "high",
    "gpt5": "high",
    "claude": "3",
    "grok-4": "high",
    "deepseek-r": "high",
    "qwen": "thinking",
    "qwen3": "thinking",
    "glm-4.5": "high", "glm-4.6": "high", "glm-5": "high",
    "kimi": "high",
    "nemotron": "high",
}

NO_REASONING_EFFORT_PROVIDERS = {"xinjianya"}

_call_history: dict[str, deque[float]] = defaultdict(lambda: deque(maxlen=200))
# Track providers with exhausted credits (skip all their models)
_credit_exhausted: set[str] = set()
# Track model performance for adaptive token sizing
_model_performance: dict[str, dict] = {}
# Track adaptive rate limits per provider (learned from 429 errors)
_adaptive_rate_limits: dict[str, int] = {}
# Track last 429 error time per provider for backoff
_last_429_time: dict[str, float] = {}


def _get_adaptive_max_tokens(target: Target) -> int:
    """Determine adaptive max_tokens based on previous model performance."""
    model_key = f"{target.provider_name}/{target.model_name}"

    if model_key not in _model_performance:
        # First test: use 2000 tokens for more accurate TPS measurement
        # This avoids inflated TPS from short 1000-token responses
        return 2000

    perf = _model_performance[model_key]
    tps = perf.get("tps")
    ttft = perf.get("ttft")

    # If we have good TPS data, adjust token count
    if tps and tps > 0:
        if tps > 100:
            # Very fast model: use full 4000 tokens for accurate measurement
            return 4000
        elif tps > 50:
            # Fast model: use 3000 tokens
            return 3000
        elif tps > 20:
            # Moderate speed: use 2000 tokens
            return 2000
        elif tps > 10:
            # Slow model: use 1500 tokens
            return 1500
        else:
            # Very slow model: use 1000 tokens to save time
            return 1000

    # If we only have TTFT data, use it to estimate
    if ttft and ttft > 0:
        if ttft < 0.5:
            # Fast first token: likely fast model
            return 3000
        elif ttft < 1.0:
            # Moderate first token: moderate model
            return 2000
        else:
            # Slow first token: slow model
            return 1000

    # Default: conservative 1000 tokens
    return 1000


def _check_rate_limit(provider_name: str) -> float:
    limit = RATE_LIMITS.get(provider_name)
    if not limit:
        limit = _adaptive_rate_limits.get(provider_name, 30)

    now = time.time()
    history = _call_history[provider_name]

    while history and now - history[0] >= 60:
        history.popleft()

    if len(history) >= limit:
        oldest = history[0]
        return max(0.0, 60.0 - (now - oldest))

    return 0.0


def _update_adaptive_rate_limit(provider_name: str, error_message: str) -> None:
    """Update adaptive rate limit based on 429 error."""
    now = time.time()

    # Check if we recently had a 429 error (within 60s)
    if provider_name in _last_429_time and now - _last_429_time[provider_name] < 60:
        # We're still hitting rate limits, reduce further
        current_limit = _adaptive_rate_limits.get(provider_name, 30)
        new_limit = max(1, current_limit // 2)
        _adaptive_rate_limits[provider_name] = new_limit
        _last_429_time[provider_name] = now
        return

    # First 429 error in a while, set initial adaptive limit
    # Try to parse the limit from error message
    import re

    m = re.search(r"(\d+)\s*request[s]?\s*per\s*(minute|hour|day)", error_message.lower())
    if m:
        limit = int(m.group(1))
        period = m.group(2)
        if period == "hour":
            limit = max(1, limit // 60)
        elif period == "day":
            limit = max(1, limit // 1440)
        _adaptive_rate_limits[provider_name] = limit
    else:
        # Default to conservative 10 requests/min if we can't parse
        _adaptive_rate_limits[provider_name] = 10

    _last_429_time[provider_name] = now


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


def set_credit_exhausted(provider_name: str):
    """Mark a provider as having exhausted credits."""
    _credit_exhausted.add(provider_name)


def is_credit_exhausted(provider_name: str) -> bool:
    """Check if a provider has exhausted credits."""
    return provider_name in _credit_exhausted


@dataclass
class TestResult:
    ttft_sec: Optional[float] = None
    tps: Optional[float] = None
    output_tokens: int = 0
    total_time_sec: Optional[float] = None
    status: str = "error"
    error_message: Optional[str] = None
    raw_sample: str = ""


def _update_model_performance(target: Target, result: TestResult) -> None:
    """Update model performance tracking for adaptive token sizing."""
    if result.status != "success" or result.tps is None or result.ttft_sec is None:
        return

    model_key = f"{target.provider_name}/{target.model_name}"
    _model_performance[model_key] = {
        "tps": result.tps,
        "ttft": result.ttft_sec,
        "last_test": time.time(),
    }


def _is_outlier(result: TestResult) -> tuple[bool, str]:
    """Check if result is an outlier that needs retesting. Returns (is_outlier, reason)."""
    if result.status != "success" or result.tps is None or result.ttft_sec is None:
        return False, ""

    # TTFT = 0 means pre-computed/cached response (not actual streaming)
    if result.ttft_sec == 0:
        return True, f"TTFT=0 (pre-computed, not streaming)"

    if result.tps > 2000:
        return True, f"TPS too high: {result.tps:.1f}"

    if result.tps < 1:
        return True, f"TPS too low: {result.tps:.1f}"

    # Very short responses with high TPS are measurement artifacts
    if result.output_tokens < 100 and result.tps > 200:
        return True, f"Suspicious: {result.output_tokens} tokens @ {result.tps:.1f} TPS"

    if result.ttft_sec > 10:
        return True, f"TTFT too high: {result.ttft_sec:.1f}s"

    if result.ttft_sec < 0.01:
        return True, f"TTFT too low: {result.ttft_sec:.3f}s"

    return False, ""


async def test_single_call(
    client: httpx.AsyncClient,
    target: Target,
    reasoning_effort: Optional[str] = None,
    max_tokens: Optional[int] = None,
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
        "max_tokens": max_tokens if max_tokens else MAX_TOKENS,
        "stream": use_stream,
    }

    # xinjianya rejects temperature=0.0 with "must be > 0"
    if target.provider_name in MIN_TEMPERATURE_PROVIDERS:
        body["temperature"] = 0.7
    else:
        body["temperature"] = 0.0

    # xinjianya rejects stream_options field
    if target.provider_name in NO_STREAM_OPTIONS_PROVIDERS:
        pass  # don't add stream_options
    elif "stream_options" not in body:
        body["stream_options"] = {"include_usage": True}

    if reasoning_effort and target.supports_reasoning:
        if target.provider_name not in NO_REASONING_EFFORT_PROVIDERS:
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

    NON_STREAM_TOKENS = 4000
    body_stream = dict(body, stream=True)
    body_nonstream = dict(body, stream=False, max_tokens=NON_STREAM_TOKENS)
    # Strip fields that specific providers reject
    if target.provider_name in NO_STREAM_OPTIONS_PROVIDERS:
        body_stream.pop("stream_options", None)
        body_nonstream.pop("stream_options", None)

    if not use_stream:
        try:
            resp = await client.post(
                url, json=body_nonstream, headers=headers, timeout=timeout
            )
            if resp.status_code != 200:
                return TestResult(
                    status="http_error",
                    error_message=f"HTTP {resp.status_code}: {resp.text[:300]}",
                    total_time_sec=(time.monotonic() - t_start),
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
            total_time_sec = time.monotonic() - t_start
            gen_s = total_time_sec
            tps = float(token_count) / gen_s if token_count and gen_s > 0 else None
            return TestResult(
                ttft_sec=0,
                tps=tps,
                output_tokens=token_count,
                total_time_sec=total_time_sec,
                status="success" if token_count else "empty",
                raw_sample=collected_text[:200],
            )
        except Exception as e:
            return TestResult(
                status="error",
                error_message=str(e)[:200],
                total_time_sec=(time.monotonic() - t_start),
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
                    total_time_sec=(time.monotonic() - t_start),
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
            total_time_sec=(time.monotonic() - t_start),
        )
    except httpx.HTTPError as e:
        return TestResult(
            status="http_error",
            error_message=str(e)[:300],
            total_time_sec=(time.monotonic() - t_start),
        )
    except Exception as e:
        return TestResult(
            status="error",
            error_message=f"{type(e).__name__}: {str(e)[:200]}",
            total_time_sec=(time.monotonic() - t_start),
        )

    t_end = time.monotonic()
    total_time_sec = t_end - t_start

    if first_token_time is None:
        return TestResult(
            status="empty",
            error_message="No content tokens received",
            total_time_sec=total_time_sec,
        )

    ttft_sec = first_token_time - t_start

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
        gen_time_s = total_time_sec - ttft_sec

        if stream_s >= 0.5:
            tps = token_count / stream_s
        elif gen_time_s > 0:
            tps = token_count / gen_time_s

    return TestResult(
        ttft_sec=ttft_sec,
        tps=tps,
        output_tokens=token_count,
        total_time_sec=total_time_sec,
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
            adaptive_max_tokens = _get_adaptive_max_tokens(target)

            for attempt in range(1 + RETRY_COUNT):
                result = await test_single_call(client, target, effort, adaptive_max_tokens)
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
                        # Don't retry permanent failures
                        if "model_not_found" in err.lower() or "not found" in err.lower():
                            break
                        if "end of life" in err.lower() or "410" in err:
                            break
                        if "no auth available" in err.lower():
                            set_credit_exhausted(target.provider_name)
                            break
                        if "429" in err:
                            _update_adaptive_rate_limit(target.provider_name, err)
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

            _update_model_performance(target, result)

            # Append result to list
            results.append(result)

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
                    ttft_sec=result.ttft_sec,
                    tps=result.tps,
                    output_tokens=result.output_tokens,
                    total_time_sec=result.total_time_sec,
                    status=result.status,
                    error_message=result.error_message,
                    run_number=i + 1,
                    raw_sample=result.raw_sample,
                )
                conn.commit()

            if i < num_runs - 1:
                await asyncio.sleep(INTER_CALL_DELAY)

        # Check for outliers and retest with more tokens/runs
        if results and results[-1].status == "success":
            is_outlier, reason = _is_outlier(results[-1])
            if is_outlier:
                # Retest with 3x more tokens and 3 runs for accuracy
                retest_max_tokens = min(adaptive_max_tokens * 3, 4000)
                retest_runs = 3

                for j in range(retest_runs):
                    for attempt in range(1 + RETRY_COUNT):
                        retest_result = await test_single_call(client, target, effort, retest_max_tokens)
                        if retest_result.status == "success" or retest_result.status in ("timeout", "empty"):
                            break
                        if attempt < RETRY_COUNT and retest_result.status == "http_error":
                            err = retest_result.error_message or ""
                            if "429" in err or "500" in err or "502" in err or "503" in err or "504" in err:
                                if "429" in err:
                                    _update_adaptive_rate_limit(target.provider_name, err)
                                wait = parse_error_for_wait_time(err)
                                if wait == 0.0:
                                    wait = 5 if "429" in err else 2
                                await asyncio.sleep(wait)
                                continue
                            break

                    _update_model_performance(target, retest_result)
                    results.append(retest_result)

                    _record_call(target.provider_name)

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
                            ttft_sec=retest_result.ttft_sec,
                            tps=retest_result.tps,
                            output_tokens=retest_result.output_tokens,
                            total_time_sec=retest_result.total_time_sec,
                            status=retest_result.status,
                            error_message=retest_result.error_message,
                            run_number=num_runs + j + 1,
                            raw_sample=retest_result.raw_sample,
                        )
                        conn.commit()

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
            avg_ttft_sec=None,
            avg_tps=None,
            avg_total_time_sec=None,
            est_10k_total_s=None,
            num_runs=len(results),
            num_success=0,
        )
        conn.commit()
        return

    ttfts = [r.ttft_sec for r in successful if r.ttft_sec is not None]
    tps_vals = [r.tps for r in successful if r.tps is not None and r.tps > 0]
    totals = [r.total_time_sec for r in successful if r.total_time_sec is not None]
    tokens_list = [r.output_tokens for r in successful]

    avg_ttft = sum(ttfts) / len(ttfts) if ttfts else None
    avg_tps = sum(tps_vals) / len(tps_vals) if tps_vals else None
    avg_total = sum(totals) / len(totals) if totals else None
    avg_tokens = sum(tokens_list) / len(tokens_list) if tokens_list else None

    if avg_tps is None and avg_total is not None and avg_tokens and avg_tokens > 0:
        gen_time = avg_total - avg_ttft if avg_ttft else avg_total
        if gen_time > 0:
            avg_tps = avg_tokens / gen_time

    est_10k = None
    if avg_ttft is not None:
        if avg_tps and avg_tps > 0:
            est_10k = avg_ttft + (10000 / avg_tps)
        elif avg_total is not None and avg_tokens and avg_tokens > 0:
            gen_time = avg_total - avg_ttft
            if gen_time > 0.05:
                est_10k = avg_ttft + (10000 * gen_time / avg_tokens)

    db.insert_summary(
        conn,
        run_id=run_id,
        timestamp=db.now_iso(),
        provider_name=target.provider_name,
        provider_url=target.base_url,
        model_name=target.model_name,
        source=target.source,
        reasoning_effort=reasoning_effort if target.supports_reasoning else None,
        avg_ttft_sec=avg_ttft,
        avg_tps=avg_tps,
        avg_total_time_sec=avg_total,
        avg_output_tokens=avg_tokens,
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
    try:
        from tqdm import tqdm
    except ImportError:
        tqdm = None

    run_id = str(uuid.uuid4())[:8]
    conn = db.connect()

    efforts_for = lambda t: (
        [None, REASONING_HIGHEST[t.model_name]] if t.model_name in REASONING_HIGHEST
        else [None]
    )

    filtered = [t for t in targets if not _should_skip_model(t)]
    targets = filtered

    async def validate_target(target: Target) -> bool:
        try:
            async with httpx.AsyncClient(timeout=15) as client:
                resp = await client.get(
                    f"{target.base_url.rstrip('/')}/models",
                    headers={"Authorization": f"Bearer {target.api_key}"} if target.api_key else {},
                )
                if resp.status_code != 200:
                    return False
                result = resp.json()
                models_data = result.get("data", result) if isinstance(result, dict) else result
                for m in models_data:
                    if m.get("id", "") == target.model_name:
                        return True
                print(f"SKIP: {target.model_name} not found in /v1/models")
                return False
        except Exception as e:
            print(f"WARN: Could not validate {target.model_name}: {e}")
            return True
    
    valid_targets = []
    for t in targets:
        if await validate_target(t):
            valid_targets.append(t)
    targets = valid_targets
    
    total_jobs = sum(len(efforts_for(t)) for t in targets)
    done = 0
    start_time = asyncio.get_event_loop().time()

    pbar = None
    if tqdm:
        pbar = tqdm(total=total_jobs, desc="Speed tests", unit="model")

    provider_sems: dict[str, asyncio.Semaphore] = {}
    for t in targets:
        if t.provider_name not in provider_sems:
            limit = RATE_LIMITS.get(t.provider_name, 30)
            provider_sems[t.provider_name] = asyncio.Semaphore(max(1, min(limit // 5, 5)))

    overall_sem = asyncio.Semaphore(max_concurrent)

    async def worker(target: Target, effort: Optional[str]):
        nonlocal done, start_time
        async with overall_sem:
            async with provider_sems[target.provider_name]:
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

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for r in results:
        if isinstance(r, Exception):
            print(f"ERROR: {r}", file=sys.stderr)
    if pbar:
        pbar.close()
    conn.close()
    return run_id
