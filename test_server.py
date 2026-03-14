import asyncio
import json
import os
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import HTTPException
from fastapi.testclient import TestClient

import server
from api import routes as api_routes
from api.routes import UpdateStatusStore
from services import poe_client


SAMPLE_GRAPHQL_RESPONSE = {
    "data": {
        "topModelLatest": {
            "topRankings": [
                {
                    "rankMetric": 0.174033,
                    "ranked": {
                        "displayName": "Nano-Banana-2",
                        "__typename": "Bot",
                    },
                },
                {
                    "rankMetric": 0.12,
                    "ranked": {
                        "displayName": "claude-sonnet-4.6",
                        "__typename": "Bot",
                    },
                },
                {
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
                    "rankMetric": 0.22,
                    "ranked": {
                        "displayName": "App-One",
                        "__typename": "ExternalApiApp",
                    },
                },
                {
                    "rankMetric": 0.18,
                    "ranked": {
                        "displayName": "App-Two",
                        "__typename": "ExternalApiApp",
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
        self.headers = {"content-type": "application/json"}

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

    async def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return self.response

    async def aclose(self):
        return None


class FetchPoeLeaderboardGraphQLTests(unittest.IsolatedAsyncioTestCase):
    async def test_fetch_models_via_graphql(self):
        fake_client = FakeAsyncClient(FakeResponse(SAMPLE_GRAPHQL_RESPONSE))

        with patch("server.httpx.AsyncClient", return_value=fake_client):
            items = await server.fetch_poe_leaderboard_via_graphql(5, "models")

        self.assertEqual(
            items,
            [
                {"handle": "Nano-Banana-2", "rank": 1, "rankMetric": 0.174033},
                {"handle": "Claude-sonnet-4.6", "rank": 2, "rankMetric": 0.12},
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

        self.assertEqual(items, [{"handle": "App-One", "rank": 1, "rankMetric": 0.22}])

    async def test_fetch_models_falls_back_to_handle_field(self):
        fake_client = FakeAsyncClient(
            FakeResponse(
                {
                    "data": {
                        "topModelLatest": {
                            "topRankings": [
                                {
                                    "rankMetric": 0.31,
                                    "ranked": {
                                        "handle": "gpt-oss-120b",
                                        "__typename": "Bot",
                                    },
                                }
                            ]
                        }
                    }
                }
            )
        )

        with patch("server.httpx.AsyncClient", return_value=fake_client):
            items = await server.fetch_poe_leaderboard_via_graphql(5, "models")

        self.assertEqual(items, [{"handle": "GPT-oss-120b", "rank": 1, "rankMetric": 0.31}])

    async def test_fetch_raises_on_missing_rankings(self):
        fake_client = FakeAsyncClient(FakeResponse({"data": {"topModelLatest": {}}}))

        with patch("server.httpx.AsyncClient", return_value=fake_client):
            with self.assertRaises(HTTPException) as exc_info:
                await server.fetch_poe_leaderboard_via_graphql(5, "models")

        self.assertEqual(exc_info.exception.status_code, 502)
        self.assertIn("topModelLatest.topRankings", exc_info.exception.detail)

    async def test_fetch_raises_when_rankings_exist_but_no_items_are_parseable(self):
        fake_client = FakeAsyncClient(
            FakeResponse(
                {
                    "data": {
                        "topModelLatest": {
                            "topRankings": [
                                {
                                    "ranked": {
                                        "__typename": "Bot",
                                    },
                                }
                            ]
                        }
                    }
                }
            )
        )

        with patch("server.httpx.AsyncClient", return_value=fake_client):
            with self.assertRaises(HTTPException) as exc_info:
                await server.fetch_poe_leaderboard_via_graphql(5, "models")

        self.assertEqual(exc_info.exception.status_code, 502)
        self.assertIn("Could not parse Poe leaderboard items", exc_info.exception.detail)


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


class UpdateStatusStoreTests(unittest.IsolatedAsyncioTestCase):
    async def test_older_task_finish_does_not_override_newer_active_task(self):
        store = UpdateStatusStore()

        first_task_id = await store.start_task(2)
        second_task_id = await store.start_task(1)

        await store.set_current(first_task_id, "GPT-5")
        await store.mark_completed(first_task_id)
        await store.finish_task(first_task_id)

        await store.set_current(second_task_id, "Claude-sonnet-4.6")
        status = await store.snapshot()

        self.assertEqual(
            status,
            {
                "running": True,
                "total": 1,
                "completed": 0,
                "current": "Claude-sonnet-4.6",
                "error": "",
                "updated_at": status["updated_at"],
            },
        )

    async def test_snapshot_returns_latest_finished_task_when_no_active_task(self):
        store = UpdateStatusStore()

        task_id = await store.start_task(1)
        await store.mark_completed(task_id)
        await store.finish_task(task_id)

        status = await store.snapshot()

        self.assertFalse(status["running"])
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["completed"], 1)
        self.assertEqual(status["current"], "")
        self.assertEqual(status["error"], "")
        self.assertIsNotNone(status["updated_at"])


class SharedAsyncClientLifecycleTests(unittest.TestCase):
    def test_lifespan_registers_and_closes_shared_async_client(self):
        class DummyAsyncClient:
            def __init__(self, **kwargs):
                self.kwargs = kwargs
                self.closed = False

            async def aclose(self):
                self.closed = True

        created_clients = []

        def build_client(**kwargs):
            client = DummyAsyncClient(**kwargs)
            created_clients.append(client)
            return client

        self.assertIsNone(poe_client.get_async_client())

        with patch("server.httpx.AsyncClient", side_effect=build_client):
            with TestClient(server.app):
                self.assertEqual(len(created_clients), 1)
                self.assertIs(poe_client.get_async_client(), created_clients[0])
                self.assertTrue(created_clients[0].kwargs["follow_redirects"])

        self.assertIsNone(poe_client.get_async_client())
        self.assertTrue(created_clients[0].closed)


class UpdateAllConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_update_all_limits_concurrency_and_isolates_failures(self):
        status_store = UpdateStatusStore()
        active = 0
        peak_active = 0

        async def fake_fetch_single_rate(handle):
            nonlocal active, peak_active
            active += 1
            peak_active = max(peak_active, active)
            try:
                await asyncio.sleep(0.01)
                if handle == "Broken":
                    raise RuntimeError("upstream failure")
                return {
                    "handle": handle,
                    "input": 1.0,
                    "output": 2.0,
                    "cache_discount": None,
                }
            finally:
                active -= 1

        with tempfile.NamedTemporaryFile(mode="w+", encoding="utf-8", delete=False) as tmp_file:
            data_file = tmp_file.name

        try:
            with patch.object(api_routes, "update_status_store", status_store), patch(
                "server.load_config",
                return_value={"handles": ["Alpha", "Broken", "Gamma"]},
            ), patch(
                "server.fetch_single_rate",
                side_effect=fake_fetch_single_rate,
            ), patch.object(
                server,
                "DATA_FILE",
                data_file,
            ), patch.object(
                server,
                "UPDATE_MAX_CONCURRENCY",
                2,
            ):
                results = await server.update_all()

            self.assertEqual(
                results,
                [
                    {"handle": "Alpha", "input": 1.0, "output": 2.0, "cache_discount": None},
                    {"handle": "Gamma", "input": 1.0, "output": 2.0, "cache_discount": None},
                ],
            )
            self.assertEqual(peak_active, 2)

            with open(data_file, "r", encoding="utf-8") as f:
                persisted = json.load(f)

            self.assertEqual(persisted, results)

            status = await status_store.snapshot()
            self.assertFalse(status["running"])
            self.assertEqual(status["total"], 3)
            self.assertEqual(status["completed"], 3)
            self.assertEqual(status["current"], "")
            self.assertIn("Broken: upstream failure", status["error"])
        finally:
            try:
                os.unlink(data_file)
            except FileNotFoundError:
                pass


if __name__ == "__main__":
    unittest.main()
