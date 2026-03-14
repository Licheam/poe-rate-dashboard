import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException

import server


SAMPLE_GRAPHQL_RESPONSE = {
    "data": {
        "topModelLatest": {
            "topRankings": [
                {
                    "rank": 1,
                    "rankMetric": 0.174033,
                    "ranked": {
                        "displayName": "Nano-Banana-2",
                        "__typename": "Bot",
                    },
                },
                {
                    "rank": 2,
                    "rankMetric": 0.12,
                    "ranked": {
                        "displayName": "claude-sonnet-4.6",
                        "__typename": "Bot",
                    },
                },
                {
                    "rank": 2,
                    "rankMetric": 0.11,
                    "ranked": {
                        "displayName": "Claude-sonnet-4.6",
                        "__typename": "Bot",
                    },
                },
            ]
        },
        "topAppLatest": {
            "topRankings": [
                {
                    "rank": 1,
                    "rankMetric": 0.22,
                    "ranked": {
                        "displayName": "App-One",
                        "__typename": "Bot",
                    },
                },
                {
                    "rank": 2,
                    "rankMetric": 0.18,
                    "ranked": {
                        "displayName": "App-Two",
                        "__typename": "Bot",
                    },
                },
            ]
        },
    }
}


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.request = httpx.Request("POST", server.POE_GRAPHQL_URL)

    def raise_for_status(self):
        if self.status_code >= 400:
            response = httpx.Response(self.status_code, request=self.request)
            raise httpx.HTTPStatusError("upstream error", request=self.request, response=response)

    def json(self):
        return self._payload


class FakeAsyncClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
            }
        )
        return self.response


class FetchPoeLeaderboardGraphQLTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_models_via_graphql(self):
        fake_client = FakeAsyncClient(FakeResponse(SAMPLE_GRAPHQL_RESPONSE))

        with patch("server.httpx.AsyncClient", return_value=fake_client):
            items = await server.fetch_poe_leaderboard_via_graphql(5, "models")

        self.assertEqual(
            items,
            [
                {"handle": "Nano-Banana-2", "rank": 1},
                {"handle": "Claude-sonnet-4.6", "rank": 2},
            ],
        )
        self.assertEqual(fake_client.calls[0]["url"], server.POE_GRAPHQL_URL)
        self.assertEqual(
            fake_client.calls[0]["json"],
            {
                "queryName": server.POE_LEADERBOARD_QUERY_NAME,
                "variables": {"interval": server.POE_LEADERBOARD_INTERVAL},
                "extensions": {"hash": server.POE_LEADERBOARD_QUERY_HASH},
            },
        )
        self.assertEqual(
            fake_client.calls[0]["headers"]["poe-queryname"],
            server.POE_LEADERBOARD_QUERY_NAME,
        )

    async def test_fetch_apps_via_graphql(self):
        fake_client = FakeAsyncClient(FakeResponse(SAMPLE_GRAPHQL_RESPONSE))

        with patch("server.httpx.AsyncClient", return_value=fake_client):
            items = await server.fetch_poe_leaderboard_via_graphql(1, "apps")

        self.assertEqual(items, [{"handle": "App-One", "rank": 1}])

    async def test_fetch_raises_on_missing_rankings(self):
        fake_client = FakeAsyncClient(FakeResponse({"data": {"topModelLatest": {}}}))

        with patch("server.httpx.AsyncClient", return_value=fake_client):
            with self.assertRaises(HTTPException) as exc_info:
                await server.fetch_poe_leaderboard_via_graphql(5, "models")

        self.assertEqual(exc_info.exception.status_code, 502)
        self.assertIn("topModelLatest.topRankings", exc_info.exception.detail)


class LeaderboardEndpointTests(unittest.IsolatedAsyncioTestCase):
    async def test_get_poe_leaderboard_endpoint_uses_graphql_data(self):
        with patch(
            "server.fetch_poe_leaderboard_via_graphql",
            return_value=[{"handle": "Nano-Banana-2", "rank": 1}],
        ) as fetch_mock:
            response = await server.get_poe_leaderboard(count=1, type="models")

        self.assertEqual(response, [{"handle": "Nano-Banana-2", "rank": 1}])
        fetch_mock.assert_called_once_with(1, "models")

    async def test_import_leaderboard_adds_handles_via_existing_helper(self):
        cfg = {"handles": ["GPT-5"]}
        leaderboard_items = [
            {"handle": "Nano-Banana-2", "rank": 1},
            {"handle": "claude-sonnet-4.6", "rank": 2},
            {"handle": "GPT-5", "rank": 3},
        ]

        with patch("server.fetch_poe_leaderboard_via_graphql", return_value=leaderboard_items), patch(
            "server.load_config", return_value=cfg
        ), patch("server.save_config") as save_mock:
            response = await server.import_leaderboard_models(
                server.LeaderboardImportRequest(count=3, type="models")
            )

        self.assertEqual(
            response,
            ["GPT-5", "Nano-Banana-2", "Claude-sonnet-4.6"],
        )
        save_mock.assert_called_once_with(cfg)


if __name__ == "__main__":
    unittest.main()
