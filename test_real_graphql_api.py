import json
import sys
from collections import Counter
from typing import Any

import httpx

import server


def build_payload() -> dict[str, Any]:
    return {
        "queryName": server.POE_LEADERBOARD_QUERY_NAME,
        "variables": {"interval": server.POE_LEADERBOARD_INTERVAL},
        "extensions": {"hash": server.POE_LEADERBOARD_QUERY_HASH},
    }


def build_headers() -> dict[str, str]:
    return {
        **server.HEADERS,
        "poe-queryname": server.POE_LEADERBOARD_QUERY_NAME,
    }


def print_json(title: str, value: Any) -> None:
    print(f"\n=== {title} ===")
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def analyze_top_rankings(data: dict[str, Any]) -> None:
    top_model_latest = data.get("data", {}).get("topModelLatest")
    print("\n=== Analysis: data.topModelLatest ===")
    if not isinstance(top_model_latest, dict):
        print("data.topModelLatest is missing or not an object")
        return

    print(f"topModelLatest keys: {sorted(top_model_latest.keys())}")
    top_rankings = top_model_latest.get("topRankings")
    if not isinstance(top_rankings, list):
        print("data.topModelLatest.topRankings is missing or not a list")
        return

    print(f"topRankings count: {len(top_rankings)}")

    ranking_key_counter: Counter[str] = Counter()
    ranked_key_counter: Counter[str] = Counter()

    for index, ranking in enumerate(top_rankings):
        if not isinstance(ranking, dict):
            print(f"ranking[{index}] is not an object: {type(ranking).__name__}")
            continue

        ranking_keys = sorted(ranking.keys())
        ranking_key_counter.update(ranking_keys)
        ranked = ranking.get("ranked")

        print(f"\nranking[{index}] keys: {ranking_keys}")
        print_json(f"ranking[{index}]", ranking)

        if isinstance(ranked, dict):
            ranked_keys = sorted(ranked.keys())
            ranked_key_counter.update(ranked_keys)
            print(f"ranking[{index}].ranked keys: {ranked_keys}")
        else:
            print(f"ranking[{index}].ranked is not an object: {type(ranked).__name__}")

    print("\n=== Summary: ranking keys frequency ===")
    print_json("ranking key counts", dict(sorted(ranking_key_counter.items())))

    print("\n=== Summary: ranked keys frequency ===")
    print_json("ranked key counts", dict(sorted(ranked_key_counter.items())))


def main() -> int:
    payload = build_payload()
    headers = build_headers()

    print_json("Request URL", server.POE_GRAPHQL_URL)
    print_json("Request Headers", headers)
    print_json("Request Payload", payload)

    try:
        response = httpx.post(
            server.POE_GRAPHQL_URL,
            headers=headers,
            json=payload,
            timeout=30.0,
            follow_redirects=True,
        )
        response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        print(f"\nHTTP status error: {exc.response.status_code}", file=sys.stderr)
        print(exc.response.text, file=sys.stderr)
        return 1
    except httpx.HTTPError as exc:
        print(f"\nHTTP request failed: {exc}", file=sys.stderr)
        return 1

    try:
        response_json = response.json()
    except json.JSONDecodeError as exc:
        print(f"\nResponse is not valid JSON: {exc}", file=sys.stderr)
        print(response.text, file=sys.stderr)
        return 1

    print_json("Response JSON", response_json)
    analyze_top_rankings(response_json)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
