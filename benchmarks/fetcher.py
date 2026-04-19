"""Benchmark data fetcher orchestration."""

import json
from pathlib import Path
from typing import Optional

import httpx
from bs4 import BeautifulSoup

import db
from matcher import normalize, extract_reasoning_effort

CACHE_DIR = Path(__file__).parent.parent / "data" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)


def _cache_path(source: str) -> Path:
    return CACHE_DIR / f"{source}.html"


def _cache_json_path(source: str) -> Path:
    return CACHE_DIR / f"{source}.json"


def _is_stale(path: Path, max_age_days: int = 7) -> bool:
    if not path.exists():
        return True
    import time

    age = time.time() - path.stat().st_mtime
    return age > max_age_days * 86400


# --- Aider ---


def fetch_aider(refresh: bool = False) -> list[dict]:
    """Scrape Aider polyglot leaderboard."""
    url = "https://aider.chat/docs/leaderboards/"
    cache = _cache_path("aider")
    if not refresh and not _is_stale(cache):
        html = cache.read_text()
    else:
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
            cache.write_text(html)
        except Exception as e:
            print(f"[aider] fetch error: {e}")
            if cache.exists():
                html = cache.read_text()
            else:
                return []

    rows = []
    try:
        soup = BeautifulSoup(html, "lxml")
        # Look for polyglot results table
        tables = soup.find_all("table")
        for table in tables:
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if "model" not in headers:
                continue
            model_idx = headers.index("model")
            # Try to find percent/score column
            score_idx = None
            for i, h in enumerate(headers):
                if "percent" in h or "pass" in h or "%" in h or "correct" in h:
                    score_idx = i
                    break
            if score_idx is None and len(headers) > 1:
                score_idx = 1  # fallback: second column

            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) <= max(model_idx, score_idx or 0):
                    continue
                model = tds[model_idx].get_text(strip=True)
                score_text = tds[score_idx].get_text(strip=True) if score_idx else ""
                try:
                    score = float(score_text.replace("%", "").strip())
                except (ValueError, AttributeError):
                    score = None
                rows.append(
                    {
                        "model": model,
                        "score": score,
                        "score_label": "polyglot %",
                        "source": "aider",
                    }
                )
            if rows:
                break  # take first matching table
    except Exception as e:
        print(f"[aider] parse error: {e}")

    return rows


# --- LiveBench ---


def fetch_livebench(refresh: bool = False) -> list[dict]:
    """Fetch LiveBench scores."""
    # LiveBench exposes data via a JSON API or CSV
    url = "https://livebench.ai/data/livebench_results.json"
    cache = _cache_json_path("livebench")

    if not refresh and not _is_stale(cache):
        try:
            data = json.loads(cache.read_text())
        except Exception:
            data = None
    else:
        data = None

    if data is None:
        # Try multiple possible endpoints
        urls = [
            "https://livebench.ai/data/livebench_results.json",
            "https://livebench.ai/api/results",
        ]
        for u in urls:
            try:
                resp = httpx.get(u, timeout=30, follow_redirects=True)
                if resp.status_code == 200:
                    data = resp.json()
                    cache.write_text(json.dumps(data, indent=2))
                    break
            except Exception:
                continue

    if not data:
        # Fallback: scrape the HTML page
        return _scrape_livebench_html(refresh)

    rows = []
    if isinstance(data, list):
        for entry in data:
            model = entry.get("model", entry.get("name", ""))
            score = entry.get("global_avg", entry.get("coding", entry.get("score")))
            if model:
                rows.append(
                    {
                        "model": model,
                        "score": float(score) if score is not None else None,
                        "score_label": "livebench global",
                        "source": "livebench",
                    }
                )
    elif isinstance(data, dict):
        for model, info in data.items():
            score = (
                info.get("global_avg", info.get("coding", info.get("score")))
                if isinstance(info, dict)
                else None
            )
            rows.append(
                {
                    "model": model,
                    "score": float(score) if score is not None else None,
                    "score_label": "livebench global",
                    "source": "livebench",
                }
            )

    return rows


def _scrape_livebench_html(refresh: bool) -> list[dict]:
    cache = _cache_path("livebench")
    if not refresh and not _is_stale(cache):
        html = cache.read_text()
    else:
        try:
            resp = httpx.get("https://livebench.ai", timeout=30, follow_redirects=True)
            resp.raise_for_status()
            html = resp.text
            cache.write_text(html)
        except Exception as e:
            print(f"[livebench] fetch error: {e}")
            return []

    rows = []
    try:
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        for table in tables:
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if "model" not in headers:
                continue
            model_idx = headers.index("model")
            score_idx = None
            for i, h in enumerate(headers):
                if "global" in h or "avg" in h or "score" in h:
                    score_idx = i
                    break
            if score_idx is None and len(headers) > 1:
                score_idx = 1

            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) <= max(model_idx, score_idx or 0):
                    continue
                model = tds[model_idx].get_text(strip=True)
                score_text = tds[score_idx].get_text(strip=True) if score_idx else ""
                try:
                    score = float(score_text)
                except ValueError:
                    score = None
                rows.append(
                    {
                        "model": model,
                        "score": score,
                        "score_label": "livebench global",
                        "source": "livebench",
                    }
                )
            if rows:
                break
    except Exception as e:
        print(f"[livebench] parse error: {e}")

    return rows


# --- LMArena ---


def fetch_lmarena(refresh: bool = False) -> list[dict]:
    """Fetch LMArena (Chatbot Arena) Elo ratings."""
    # Known API endpoints for lmarena
    urls = [
        "https://lmarena.ai/api/v1/leaderboard",
        "https://lmarena.ai/api/leaderboard",
    ]
    cache = _cache_json_path("lmarena")

    if not refresh and not _is_stale(cache):
        try:
            data = json.loads(cache.read_text())
            if data:
                return _parse_lmarena(data)
        except Exception:
            pass

    for url in urls:
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                cache.write_text(json.dumps(data, indent=2))
                return _parse_lmarena(data)
        except Exception:
            continue

    # Fallback: scrape HTML
    return _scrape_lmarena_html(refresh)


def _parse_lmarena(data) -> list[dict]:
    rows = []
    items = (
        data if isinstance(data, list) else data.get("data", data.get("results", []))
    )
    if isinstance(items, list):
        for entry in items:
            model = entry.get("model", entry.get("name", ""))
            elo = entry.get("elo", entry.get("arena_score", entry.get("score")))
            if model:
                rows.append(
                    {
                        "model": model,
                        "score": float(elo) if elo is not None else None,
                        "score_label": "Elo",
                        "source": "lmarena",
                    }
                )
    return rows


def _scrape_lmarena_html(refresh: bool) -> list[dict]:
    cache = _cache_path("lmarena")
    if not refresh and not _is_stale(cache):
        html = cache.read_text()
    else:
        try:
            resp = httpx.get(
                "https://lmarena.ai/leaderboard", timeout=30, follow_redirects=True
            )
            resp.raise_for_status()
            html = resp.text
            cache.write_text(html)
        except Exception as e:
            print(f"[lmarena] fetch error: {e}")
            return []

    # LMArena is a React app, tables might not be in static HTML
    # Try to find embedded JSON data
    rows = []
    try:
        import re

        # Look for __NEXT_DATA__ or similar embedded state
        match = re.search(r"__NEXT_DATA__\s*=\s*({.*?})</script>", html, re.DOTALL)
        if match:
            data = json.loads(match.group(1))
            # Drill into pageProps
            props = data.get("props", {}).get("pageProps", {})
            for key in ["leaderboard", "data", "results"]:
                if key in props:
                    return _parse_lmarena(props[key])
    except Exception as e:
        print(f"[lmarena] parse error: {e}")

    return rows


# --- SWE-bench ---


def fetch_swebench(refresh: bool = False) -> list[dict]:
    """Fetch SWE-bench Verified leaderboard."""
    urls = [
        "https://www.swebench.com/api/leaderboard",
        "https://swebench.com/api/leaderboard",
    ]
    cache = _cache_json_path("swebench")

    if not refresh and not _is_stale(cache):
        try:
            data = json.loads(cache.read_text())
            if data:
                return _parse_swebench(data)
        except Exception:
            pass

    for url in urls:
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            if resp.status_code == 200:
                data = resp.json()
                cache.write_text(json.dumps(data, indent=2))
                return _parse_swebench(data)
        except Exception:
            continue

    # Fallback: scrape HTML
    return _scrape_swebench_html(refresh)


def _parse_swebench(data) -> list[dict]:
    rows = []
    items = (
        data if isinstance(data, list) else data.get("data", data.get("results", []))
    )
    if isinstance(items, list):
        for entry in items:
            model = entry.get("model", entry.get("name", ""))
            score = entry.get(
                "resolved", entry.get("score", entry.get("percent_resolved"))
            )
            if model:
                rows.append(
                    {
                        "model": model,
                        "score": float(score) if score is not None else None,
                        "score_label": "% resolved",
                        "source": "swebench",
                    }
                )
    return rows


def _scrape_swebench_html(refresh: bool) -> list[dict]:
    cache = _cache_path("swebench")
    if not refresh and not _is_stale(cache):
        html = cache.read_text()
    else:
        try:
            resp = httpx.get(
                "https://www.swebench.com", timeout=30, follow_redirects=True
            )
            resp.raise_for_status()
            html = resp.text
            cache.write_text(html)
        except Exception as e:
            print(f"[swebench] fetch error: {e}")
            return []

    rows = []
    try:
        soup = BeautifulSoup(html, "lxml")
        tables = soup.find_all("table")
        for table in tables:
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if not headers:
                continue
            model_idx = next(
                (i for i, h in enumerate(headers) if "model" in h or "name" in h), 0
            )
            score_idx = next(
                (
                    i
                    for i, h in enumerate(headers)
                    if "resolved" in h or "%" in h or "score" in h
                ),
                1,
            )

            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                if len(tds) <= max(model_idx, score_idx):
                    continue
                model = tds[model_idx].get_text(strip=True)
                score_text = tds[score_idx].get_text(strip=True)
                try:
                    score = float(score_text.replace("%", "").strip())
                except ValueError:
                    score = None
                rows.append(
                    {
                        "model": model,
                        "score": score,
                        "score_label": "% resolved",
                        "source": "swebench",
                    }
                )
            if rows:
                break
    except Exception as e:
        print(f"[swebench] parse error: {e}")

    return rows


# --- Orchestration ---

BENCHMARK_SOURCES = {
    "aider": fetch_aider,
    "livebench": fetch_livebench,
    "lmarena": fetch_lmarena,
    "swebench": fetch_swebench,
}


def fetch_source(source: str, refresh: bool = False) -> list[dict]:
    fetcher = BENCHMARK_SOURCES.get(source)
    if not fetcher:
        print(f"Unknown benchmark source: {source}")
        return []
    return fetcher(refresh=refresh)


def fetch_all(refresh: bool = False) -> dict[str, list[dict]]:
    """Fetch all benchmark sources, store in DB."""
    conn = db.connect()
    results = {}
    for source, fetcher in BENCHMARK_SOURCES.items():
        print(f"  Fetching {source}...")
        rows = fetcher(refresh=refresh)
        print(f"    → {len(rows)} models found")
        results[source] = rows

        # Store in DB
        for row in rows:
            model_name = row.get("model", "")
            name_clean, effort = extract_reasoning_effort(model_name)
            canonical = normalize(name_clean)
            db.upsert_benchmark(
                conn,
                model_canonical=canonical,
                benchmark_source=source,
                reasoning_effort=effort,
                score=row.get("score"),
                score_label=row.get("score_label", ""),
                raw_data=row,
            )
        conn.commit()

    conn.close()
    return results
