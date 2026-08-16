"""Offline tests for the shared measurement core (benchmarks/measurement.py).

All timings are driven by an injectable fake clock, so the TPS/TTFT math
is verified deterministically without sleeps or network. The transport
test uses httpx.MockTransport with a fake SSE provider.
"""

import asyncio
import json
import sys
from pathlib import Path

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from benchmarks.measurement import (
    BenchmarkRecord,
    StreamCollector,
    approx_tokens,
    finalize_metrics,
    measure_openai_stream,
    parse_sse_line,
    tokens_from_usage,
    write_csv,
    write_json,
    load_json,
    RECORD_FIELDS,
)


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

class TestTokenCounting:
    def test_approx_tokens_four_chars_per_token(self):
        assert approx_tokens("abcdefgh") == 2
        assert approx_tokens("abc") == 1   # non-empty minimum 1
        assert approx_tokens("") == 0

    def test_usage_prefers_completion_tokens(self):
        assert tokens_from_usage({"completion_tokens": 42, "prompt_tokens": 10}) == 42

    def test_usage_field_fallbacks(self):
        assert tokens_from_usage({"output_tokens": 7}) == 7
        assert tokens_from_usage({"tokens": 9}) == 9
        assert tokens_from_usage({"total_tokens": 30, "prompt_tokens": 12}) == 18

    def test_usage_absent_or_invalid(self):
        assert tokens_from_usage(None) is None
        assert tokens_from_usage({}) is None
        assert tokens_from_usage({"completion_tokens": 0}) is None


# ---------------------------------------------------------------------------
# SSE parsing
# ---------------------------------------------------------------------------

class TestSSEParsing:
    def test_data_line_parsed(self):
        assert parse_sse_line('data: {"choices": []}') == {"choices": []}

    def test_done_sentinel_returns_none(self):
        assert parse_sse_line("data: [DONE]") is None

    def test_non_data_lines_ignored(self):
        assert parse_sse_line(": keepalive") is None
        assert parse_sse_line("event: message") is None
        assert parse_sse_line("") is None

    def test_malformed_json_skipped(self):
        assert parse_sse_line("data: {not json") is None


# ---------------------------------------------------------------------------
# TTFT / TPS math (fake clock)
# ---------------------------------------------------------------------------

def make_stream(chunks, delays):
    """Yield (chunk, clock_now) pairs from a scripted timeline."""
    now = 0.0
    for chunk, delay in zip(chunks, delays):
        now += delay
        yield chunk, now


def collect(chunks, delays):
    """Feed a scripted stream through a StreamCollector; return (stats, times)."""
    clock = {"now": 0.0}
    collector = StreamCollector(clock=lambda: clock["now"])
    for chunk, delay in zip(chunks, delays):
        clock["now"] += delay
        collector.observe(chunk)
    return collector.stats, clock["now"]


def content_chunk(text):
    return {"choices": [{"delta": {"content": text}}]}


USAGE_CHUNK = {"choices": [], "usage": {"prompt_tokens": 20, "completion_tokens": 100}}


class TestMeasurementMath:
    def test_ttft_is_time_to_first_content_chunk(self):
        stats, end = collect(
            [content_chunk("aaaa"), content_chunk("bbbb")],
            delays=[0.25, 1.0],  # first token at t=0.25, second at t=1.25
        )
        metrics = finalize_metrics(stats, t_start=0.0, t_end=end)
        assert metrics.ttft_sec == pytest.approx(0.25)
        # 8 chars -> 2 approx tokens; stream window = 1.0s -> 2 TPS
        assert metrics.tps == pytest.approx(2.0)
        assert metrics.token_source == "estimated"
        assert metrics.output_tokens == 2

    def test_usage_tokens_override_estimate(self):
        stats, end = collect(
            [content_chunk("a" * 40), USAGE_CHUNK],
            delays=[0.1, 0.9],
        )
        metrics = finalize_metrics(stats, t_start=0.0, t_end=end)
        assert metrics.output_tokens == 100
        assert metrics.token_source == "usage"
        assert metrics.prompt_tokens == 20
        assert metrics.tps == pytest.approx(100 / 0.9)

    def test_reasoning_deltas_count_as_tokens(self):
        stats, end = collect(
            [{"choices": [{"delta": {"reasoning": "thinking hard"}}]},
             {"choices": [{"delta": {"content": "answer"}}]}],
            delays=[0.5, 1.5],
        )
        assert stats.saw_tokens
        metrics = finalize_metrics(stats, t_start=0.0, t_end=end)
        assert metrics.ttft_sec == pytest.approx(0.5)
        assert metrics.output_tokens == approx_tokens("thinking hard") + approx_tokens("answer")

    def test_burst_stream_degrades_to_whole_call_rate_and_zero_ttft(self):
        # Entire body arrives in ONE chunk at t=2.0 of a 2.01s call:
        # no real streaming -> TTFT must be zeroed and TPS = tokens/total.
        stats, end = collect([content_chunk("x" * 400)], delays=[2.0])
        metrics = finalize_metrics(stats, t_start=0.0, t_end=2.01)
        assert metrics.ttft_sec == 0.0
        assert metrics.tps == pytest.approx(100 / 2.01)

    def test_short_stream_between_burst_and_real(self):
        # stream window < 0.5s but ttft/total < 0.95 -> uses gen_time window
        stats, end = collect(
            [content_chunk("aaaa"), content_chunk("bbbb")],
            delays=[0.4, 0.2],
        )
        metrics = finalize_metrics(stats, t_start=0.0, t_end=end + 0.2)
        assert metrics.ttft_sec == pytest.approx(0.4)
        assert metrics.tps == pytest.approx(2 / 0.4)

    def test_no_tokens_means_no_metrics(self):
        stats, end = collect([{"choices": [{"delta": {}}]}], delays=[0.1])
        metrics = finalize_metrics(stats, t_start=0.0, t_end=end)
        assert not stats.saw_tokens
        assert metrics.ttft_sec is None
        assert metrics.tps is None

    def test_single_token_has_ttft_but_no_tps(self):
        stats, end = collect([content_chunk("ab")], delays=[0.3])
        metrics = finalize_metrics(stats, t_start=0.0, t_end=1.0)
        assert metrics.ttft_sec == pytest.approx(0.3)
        assert metrics.tps is None


# ---------------------------------------------------------------------------
# Transport-level measurement against a mocked provider (offline, no sleeps)
# ---------------------------------------------------------------------------

def sse_response(chunks, usage=None, status=200):
    """Build SSE lines for a fake provider response."""
    lines = []
    for text in chunks:
        lines.append(f"data: {json.dumps(content_chunk(text))}")
    if usage is not None:
        lines.append(f"data: {json.dumps({'choices': [], 'usage': usage})}")
    lines.append("data: [DONE]")
    body = ("\n".join(lines) + "\n").encode()
    return httpx.Response(status, content=body, headers={"Content-Type": "text/event-stream"})


class TestMeasureOpenAIStream:
    def test_success_stream_with_usage(self):
        def handler(request):
            return sse_response(["hello " * 10, "world " * 10],
                                usage={"prompt_tokens": 5, "completion_tokens": 25})

        transport = httpx.MockTransport(handler)
        client = httpx.AsyncClient(transport=transport)
        result = asyncio.run(measure_openai_stream(
            client, "https://fake.test/v1/chat/completions", {"model": "m"}
        ))
        assert result["status"] == "success"
        assert result["output_tokens"] == 25
        assert result["token_source"] == "usage"
        assert result["prompt_tokens"] == 5
        assert result["ttft_sec"] is not None and result["ttft_sec"] >= 0
        assert result["total_time_sec"] >= 0
        assert "hello" in result["raw_sample"]

    def test_http_error_reported(self):
        def handler(request):
            return httpx.Response(429, json={"error": "rate limited"})

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = asyncio.run(measure_openai_stream(
            client, "https://fake.test/v1/chat/completions", {"model": "m"}
        ))
        assert result["status"] == "http_error"
        assert "429" in result["error_message"]

    def test_empty_stream_reported(self):
        def handler(request):
            return sse_response([])

        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        result = asyncio.run(measure_openai_stream(
            client, "https://fake.test/v1/chat/completions", {"model": "m"}
        ))
        assert result["status"] == "empty"


# ---------------------------------------------------------------------------
# Record serialization (JSON + CSV)
# ---------------------------------------------------------------------------

class TestRecordSerialization:
    def test_record_has_timestamp_and_roundtrips_json(self, tmp_path):
        record = BenchmarkRecord(
            provider="prov", model="m1", status="success",
            ttft_sec=0.25, tps=80.0, output_tokens=100, total_time_sec=1.5,
            token_source="usage", run_id="abc",
        )
        assert record.timestamp  # auto-populated ISO string
        assert "T" in record.timestamp

        path = write_json([record], tmp_path / "sub" / "results.json")
        loaded = load_json(path)
        assert loaded == [record]

    def test_csv_columns_stable(self, tmp_path):
        record = BenchmarkRecord(provider="prov", model="m1", status="success")
        path = write_csv([record], tmp_path / "results.csv")
        lines = path.read_text().strip().splitlines()
        assert lines[0] == ",".join(RECORD_FIELDS)
        assert len(lines) == 2

    def test_csv_append_keeps_single_header(self, tmp_path):
        path = tmp_path / "results.csv"
        write_csv([BenchmarkRecord(provider="p", model="a")], path)
        write_csv([BenchmarkRecord(provider="p", model="b")], path, append=True)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 3  # header + 2 rows

    def test_csv_append_with_stale_header_rotates_file(self, tmp_path):
        """Regression (review finding): appending after a schema change must
        never misalign rows under the stale header — the old file is rotated
        away and a fresh file with the current header is started."""
        path = tmp_path / "results.csv"
        # Simulate an old-schema CSV (columns predate token_source, say).
        stale_fields = [f for f in RECORD_FIELDS if f != "token_source"]
        old_line = ",".join(["p", "a", "", ""][: len(stale_fields)])
        path.write_text(",".join(stale_fields) + "\n" + old_line + "\n")

        write_csv([BenchmarkRecord(provider="q", model="b")], path, append=True)

        # New file carries the current header, correctly aligned rows only.
        lines = path.read_text().strip().splitlines()
        assert lines[0] == ",".join(RECORD_FIELDS)
        assert len(lines) == 2
        assert lines[1].split(",")[RECORD_FIELDS.index("model")] == "b"

        # The stale file was rotated, not overwritten.
        rotated = list(tmp_path.glob("results.csv.stale-*"))
        assert len(rotated) == 1
        rotated_lines = rotated[0].read_text().strip().splitlines()
        assert rotated_lines[0] == ",".join(stale_fields)

    def test_csv_append_matching_header_appends_in_place(self, tmp_path):
        path = tmp_path / "results.csv"
        write_csv([BenchmarkRecord(provider="p", model="a")], path)
        stale = list(tmp_path.glob("results.csv.stale-*"))
        write_csv([BenchmarkRecord(provider="p", model="b")], path, append=True)
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 3
        assert list(tmp_path.glob("results.csv.stale-*")) == stale  # no rotation
