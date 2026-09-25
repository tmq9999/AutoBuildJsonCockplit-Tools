"""Private, offline bulk imports use independent credential transactions."""
import asyncio
from copy import deepcopy
from uuid import UUID, uuid4

import httpx
import pytest
from sqlalchemy import text

from tests.gateway.codex_admin_support import ORIGIN, private_env
from tests.gateway.integration.test_oauth_import import credential_service, record

pytestmark = [pytest.mark.postgres, pytest.mark.asyncio]
PATH = "/api/service/oauth/import-batch"


def unique_record(index):
    value = record()
    value["email"] = f"synthetic-{index}@example.com"
    value["account"]["id"] = f"synthetic-account-{index}"
    return value


def assert_no_secrets(response):
    for secret in ("synthetic-access", "synthetic-refresh", "synthetic-id", "synthetic-account-",
                   "encrypted_secret", "DO-NOT-ECHO"):
        assert secret not in response.text


async def test_imports_all_226_records_without_provider_network_and_keeps_tokens_encrypted(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        records = [unique_record(index) for index in range(226)]
        response = await client.post(PATH, json={"records": records, "proxy_profile_id": None})
        assert response.status_code == 200, response.text
        result = response.json()
        assert {key: result[key] for key in ("total", "imported", "duplicate", "failed")} == {
            "total": 226, "imported": 226, "duplicate": 0, "failed": 0}
        assert len({row["id"] for row in result["results"]}) == 226
        for index, row in enumerate(result["results"], 1):
            assert row["index"] == index and row["status"] == "imported"
            assert row["email"] == records[index - 1]["email"]
            UUID(row["id"])
        assert "no-store" in response.headers["cache-control"]
        assert_no_secrets(response)
        async with pg_db.sessions() as session:
            stored = (await session.execute(text("SELECT health,profile_id,encrypted_secret::text FROM credentials"))).all()
            assert len(stored) == 226
            assert all(row[0] == "unverified" and row[1] is None for row in stored)
            assert all("synthetic-refresh" not in row[2] for row in stored)
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='oauth.imported'")) == 226
            assert await session.scalar(text("SELECT count(*) FROM requests")) == 0

        # The unchanged single-record API and the batch endpoint share the
        # same issuer/account deduplication, not just IDs from this request.
        replay = await client.post("/api/service/oauth/import", json={"record": records[0]})
        assert replay.status_code == 201 and replay.json()["id"] == result["results"][0]["id"]
        duplicate = await client.post(PATH, json={"records": records[:2]})
        assert duplicate.json()["imported"] == 0 and duplicate.json()["duplicate"] == 2
        assert [row["id"] for row in duplicate.json()["results"]] == [row["id"] for row in result["results"][:2]]


async def test_partial_batch_reports_duplicates_and_safe_errors_and_keeps_later_rows(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        first = unique_record(1)
        duplicate = deepcopy(first)
        duplicate["id"] = str(uuid4())
        duplicate["tokens"]["refresh_token"] = "DO-NOT-ECHO replacement"
        malformed = unique_record(2)
        del malformed["tokens"]["refresh_token"]
        malformed["tokens"]["access_token"] = "DO-NOT-ECHO validation"
        invalid_email = {"email": "DO-NOT-ECHO\nnot-an-email", "tokens": {"access_token": "DO-NOT-ECHO"}}
        storage_failure = unique_record(3)
        storage_failure["account"]["id"] = "DO-NOT-ECHO\x00invalid-postgres-text"
        last = unique_record(4)
        response = await client.post(PATH, json={"records": [first, duplicate, malformed, invalid_email, storage_failure, last]})
        assert response.status_code == 200, response.text
        value = response.json()
        assert (value["total"], value["imported"], value["duplicate"], value["failed"]) == (6, 2, 1, 3)
        rows = value["results"]
        assert [row["status"] for row in rows] == ["imported", "duplicate", "error", "error", "error", "imported"]
        assert rows[0]["id"] == rows[1]["id"]
        assert rows[2] == {"index": 3, "status": "error", "email": malformed["email"], "code": "invalid_request"}
        assert rows[3] == {"index": 4, "status": "error", "code": "invalid_request"}
        assert rows[4]["code"] == "storage_unavailable" and "id" not in rows[4]
        assert_no_secrets(response)
        async with pg_db.sessions() as session:
            stored = (await session.execute(text("SELECT email FROM credentials ORDER BY email"))).scalars().all()
            assert stored == [first["email"], last["email"]]
            assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='oauth.imported'")) == 2
        saved = await services.engine.credentials._record(UUID(rows[0]["id"]))
        assert services.engine.credentials._tokens(saved)["refresh_token"] == "synthetic-refresh"


async def test_batch_profile_preflight_and_explicit_profile_and_inherit(pg_db, settings):
    from autobuild_json.gateway.admin.schemas import ProxyInput
    async with private_env(pg_db, settings) as (client, services):
        bad = await client.post(PATH, json={"records": [unique_record(1), unique_record(2)], "proxy_profile_id": str(uuid4())})
        assert bad.status_code == 422
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM credentials")) == 0
            assert await session.scalar(text("SELECT count(*) FROM providers")) == 0
        profile = await services.profiles.create(ProxyInput(name="Synthetic direct", mode="direct"))
        created = await client.post(PATH, json={"records": [unique_record(1), unique_record(2)], "proxy_profile_id": str(profile)})
        assert created.status_code == 200 and created.json()["imported"] == 2
        inherited = await client.post(PATH, json={"records": [unique_record(3)], "proxy_profile_id": None})
        assert inherited.json()["imported"] == 1
        async with pg_db.sessions() as session:
            rows = (await session.execute(text("SELECT email,profile_id FROM credentials ORDER BY email"))).all()
            assert [row[1] for row in rows] == [profile, profile, None]
        # Existing credentials keep their own configured profile on replay.
        replay = await client.post(PATH, json={"records": [unique_record(1)], "proxy_profile_id": None})
        assert replay.json()["duplicate"] == 1
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT profile_id FROM credentials WHERE email=:email"),
                                        {"email": "synthetic-1@example.com"}) == profile


async def test_batch_requires_private_session_origin_csrf_and_strict_top_level_fields(pg_db, settings):
    from autobuild_json.gateway.http.app import create_gateway_app
    payload = {"records": [unique_record(1)]}
    async with private_env(pg_db, settings, login=False) as (client, services):
        assert (await client.post(PATH, headers={"Origin": ORIGIN}, json=payload)).status_code == 401
        login = await client.post("/api/session", headers={"Origin": ORIGIN}, json={"token": "test-admin-token"})
        assert (await client.post(PATH, headers={"Origin": ORIGIN}, json=payload)).status_code == 403
        assert (await client.post(PATH, headers={"Origin": "http://evil.invalid", "X-CSRF-Token": login.json()["csrf_token"]}, json=payload)).status_code == 403
        client.headers.update({"Origin": ORIGIN, "X-CSRF-Token": login.json()["csrf_token"]})
        assert (await client.post(PATH + "?token=DO-NOT-ECHO", json=payload)).status_code == 400
        for invalid in ({}, {"records": []}, {"records": [{}] * 1001}, {"records": "DO-NOT-ECHO"},
                        {"records": [unique_record(1)], "unexpected": "DO-NOT-ECHO"},
                        {"records": [unique_record(1)], "proxy_profile_id": "DO-NOT-ECHO"}):
            result = await client.post(PATH, json=invalid)
            assert result.status_code == 422 and "DO-NOT-ECHO" not in result.text
        public = create_gateway_app(services.identity, services.catalog, services.engine, allowed_hosts=("public",))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=public), base_url="http://public") as other:
            assert (await other.post(PATH, json=payload)).status_code == 404
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM credentials")) == 0
            assert await session.scalar(text("SELECT count(*) FROM audit_events")) == 0


async def test_scalar_array_and_null_rows_are_individual_errors_not_batch_rejection(pg_db, settings):
    async with private_env(pg_db, settings) as (client, services):
        response = await client.post(PATH, json={"records": [unique_record(1), "DO-NOT-ECHO", None, [], 42, True, unique_record(2)]})
        assert response.status_code == 200
        result = response.json()
        assert (result["total"], result["imported"], result["duplicate"], result["failed"]) == (7, 2, 0, 5)
        assert result["results"][1:6] == [
            {"index": index, "status": "error", "code": "invalid_request"} for index in range(2, 7)]
        assert_no_secrets(response)
        async with pg_db.sessions() as session:
            assert await session.scalar(text("SELECT count(*) FROM credentials")) == 2


async def test_concurrent_import_has_exactly_one_created_result_and_audit(pg_db):
    service = await credential_service(pg_db)
    value = unique_record(1)
    results = await asyncio.gather(*(service.import_record_result(value, None) for _ in range(4)))
    assert len({identity for identity, created in results}) == 1
    assert sum(created for identity, created in results) == 1
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM credentials")) == 1
        assert await session.scalar(text("SELECT count(*) FROM audit_events WHERE action='oauth.imported'")) == 1


async def test_single_import_rejects_missing_profile_atomically(pg_db):
    from autobuild_json.gateway.errors import GatewayError
    service = await credential_service(pg_db)
    with pytest.raises(GatewayError, match="invalid_request"):
        await service.import_record(unique_record(1), uuid4())
    async with pg_db.sessions() as session:
        assert await session.scalar(text("SELECT count(*) FROM credentials")) == 0
        assert await session.scalar(text("SELECT count(*) FROM providers")) == 0
