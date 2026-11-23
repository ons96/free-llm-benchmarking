"""Weekly free-model leaderboard newsletter draft generator.

Reads data/free_models.json, compares it with the previous snapshot, and writes
a markdown draft a human (or beehiiv's API) can send with light editing.

Ponytail: no beehiiv API client. beehiiv needs an account and a password that
only the operator has, so this stops at the draft file. Add the API call when
the publication exists.
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_JSON = Path("data/free_models.json")
DEFAULT_STATE = Path("data/newsletter-state.json")
DEFAULT_OUT = Path("drafts")

BRAND = "spendwarden"


def load_snapshot(json_path: Path) -> dict:
    """Return {stable_key: {health, throughput, context, tool_calls}} + meta."""
    data = json.loads(json_path.read_text())
    models = data.get("models") or []
    snap = {}
    for m in models:
        key = f"{m.get('provider')}/{m.get('model_id')}"
        snap[key] = {
            "health": m.get("health") or "unknown",
            "throughput": m.get("throughput"),
            "context": m.get("context"),
            "tool_calls": bool(m.get("tool_calls")),
        }
    return {
        "generated_at": data.get("generated_at"),
        "provider_count": len({k.split("/", 1)[0] for k in snap}),
        "models": snap,
    }


def diff_snapshots(old: dict | None, new: dict) -> dict:
    """Classify changes between two snapshots. Pure function, used by tests."""
    old_models = (old or {}).get("models") or {}
    new_models = new["models"]

    # On a first run everything is "new"; that is not news, so report no moves.
    added = [] if old is None else sorted(set(new_models) - set(old_models))
    removed = [] if old is None else sorted(set(old_models) - set(new_models))

    newly_healthy, newly_broken = [], []
    for key in sorted(set(old_models) & set(new_models)):
        was = old_models[key]["health"]
        now = new_models[key]["health"]
        if was != "healthy" and now == "healthy":
            newly_healthy.append(key)
        elif was == "healthy" and now != "healthy":
            newly_broken.append(key)

    counts = {}
    for m in new_models.values():
        counts[m["health"]] = counts.get(m["health"], 0) + 1

    healthy = [k for k, m in new_models.items() if m["health"] == "healthy"]
    fastest = sorted(
        ((new_models[k]["throughput"], k) for k in healthy if new_models[k]["throughput"]),
        reverse=True,
    )

    return {
        "added": added,
        "removed": removed,
        "newly_healthy": newly_healthy,
        "newly_broken": newly_broken,
        "counts": counts,
        "total": len(new_models),
        "provider_count": new["provider_count"],
        "fastest": fastest,
        "tool_capable_healthy": sum(1 for k in healthy if new_models[k]["tool_calls"]),
        "is_first_run": old is None,
    }


def _fmt(key: str) -> str:
    provider, _, model = key.partition("/")
    return f"`{model}` @ {provider}"


def render(d: dict, generated_at: str | None) -> str:
    stamp = generated_at or datetime.now(timezone.utc).isoformat()
    healthy = d["counts"].get("healthy", 0)
    degraded = d["counts"].get("degraded", 0)
    unhealthy = d["counts"].get("unhealthy", 0)
    unknown = d["counts"].get("unknown", 0)

    lines = [
        f"# {BRAND} weekly: free model radar",
        "",
        f"Draft generated {stamp} from data/free_models.json.",
        "",
        "## Subject line options",
        "",
        f"- Free model radar: {healthy} verified-working models across {d['provider_count']} providers",
        f"- This week in free LLMs: {len(d['newly_healthy'])} recovered, {len(d['newly_broken'])} broke",
        "",
        "## Preview text",
        "",
        f"{len(d['added'])} new entries, {len(d['newly_healthy'])} models came back up, "
        f"{len(d['newly_broken'])} stopped answering. Full list below.",
        "",
        "## This week's numbers",
        "",
        "| Bucket | Count |",
        "|---|---|",
        f"| Catalogued free entries | {d['total']} |",
        f"| Verified healthy | {healthy} |",
        f"| Degraded | {degraded} |",
        f"| Unhealthy | {unhealthy} |",
        f"| Not yet probed | {unknown} |",
        f"| Tool-capable and healthy | {d['tool_capable_healthy']} |",
        "",
    ]

    if d["is_first_run"]:
        lines += [
            "First run: no previous snapshot, so there are no week-over-week moves.",
            "",
        ]
    else:
        lines += ["## Came back up", ""]
        lines += [f"- {_fmt(k)}" for k in d["newly_healthy"][:15]] or ["- none"]
        lines += ["", "## Stopped answering", ""]
        lines += [f"- {_fmt(k)}" for k in d["newly_broken"][:15]] or ["- none"]
        lines += ["", "## Newly catalogued", ""]
        lines += [f"- {_fmt(k)}" for k in d["added"][:15]] or ["- none"]
        lines += ["", "## Dropped from the catalog", ""]
        lines += [f"- {_fmt(k)}" for k in d["removed"][:15]] or ["- none"]
        lines += [""]

    lines += ["## Fastest healthy models", ""]
    if d["fastest"]:
        lines += ["| Model | Provider | Tokens/sec |", "|---|---|---|"]
        for tps, key in d["fastest"][:5]:
            provider, _, model = key.partition("/")
            lines.append(f"| `{model}` | {provider} | {tps:.1f} |")
    else:
        lines.append("No throughput measurements in this run.")
    lines += [
        "",
        "## What this means for your bill",
        "",
        "A working free model is not free if you waste an hour discovering it is down.",
        "Auditing which providers actually answer, and which of your keys still have "
        "quota, is the difference between a cheap stack and a stack you cannot rely on.",
        "",
        f"Reply to this email if you want your own LLM spend audited by {BRAND}.",
        "",
        f"-- {BRAND}",
        "",
    ]
    return "\n".join(lines)


def build(json_path: Path, state_path: Path, out_dir: Path, commit: bool) -> Path:
    new = load_snapshot(json_path)
    old = json.loads(state_path.read_text()) if state_path.exists() else None
    delta = diff_snapshots(old, new)
    text = render(delta, new["generated_at"])

    out_dir.mkdir(parents=True, exist_ok=True)
    day = (new["generated_at"] or datetime.now(timezone.utc).isoformat())[:10]
    draft = out_dir / f"newsletter-{day}.md"
    draft.write_text(text)

    if commit:
        state_path.write_text(json.dumps(new, indent=2, sort_keys=True))
    return draft


def _self_test() -> int:
    old = {
        "models": {
            "a/one": {"health": "unhealthy", "throughput": None, "context": 4096, "tool_calls": False},
            "a/two": {"health": "healthy", "throughput": 10.0, "context": 4096, "tool_calls": True},
            "a/gone": {"health": "healthy", "throughput": 5.0, "context": 4096, "tool_calls": False},
        }
    }
    new = {
        "generated_at": "2026-01-01T00:00:00+00:00",
        "provider_count": 1,
        "models": {
            "a/one": {"health": "healthy", "throughput": 20.0, "context": 4096, "tool_calls": False},
            "a/two": {"health": "unhealthy", "throughput": None, "context": 4096, "tool_calls": True},
            "a/three": {"health": "unknown", "throughput": None, "context": 4096, "tool_calls": False},
        },
    }
    d = diff_snapshots(old, new)
    assert d["added"] == ["a/three"], d["added"]
    assert d["removed"] == ["a/gone"], d["removed"]
    assert d["newly_healthy"] == ["a/one"], d["newly_healthy"]
    assert d["newly_broken"] == ["a/two"], d["newly_broken"]
    assert d["fastest"] == [(20.0, "a/one")], d["fastest"]
    first = diff_snapshots(None, new)
    assert first["is_first_run"] is True and first["added"] == []
    text = render(d, new["generated_at"])
    assert "newsletter" not in text.lower().split("## Subject line options")[0]
    assert "`one` @ a" in text and "`three` @ a" in text
    print("newsletter self-test: OK")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Generate the weekly free-model newsletter draft.")
    p.add_argument("--json", type=Path, default=DEFAULT_JSON)
    p.add_argument("--state", type=Path, default=DEFAULT_STATE)
    p.add_argument("--out", type=Path, default=DEFAULT_OUT)
    p.add_argument("--commit", action="store_true", help="advance the snapshot used for next week's deltas")
    p.add_argument("--self-test", action="store_true")
    args = p.parse_args(argv)

    if args.self_test:
        return _self_test()
    if not args.json.exists():
        print(f"missing {args.json}", file=sys.stderr)
        return 2

    draft = build(args.json, args.state, args.out, args.commit)
    print(f"wrote {draft}")
    if not args.commit:
        print("state not advanced; pass --commit once the issue is sent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
