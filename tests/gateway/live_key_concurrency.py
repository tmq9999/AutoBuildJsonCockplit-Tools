"""Opt-in two-request LAN check; disposable keys, no retries or raw responses."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path

import httpx

from autobuild_json.gateway.errors import SAFE_CODES, SAFE_STAGES


def response_summary(response, marker):
    body = response.json()
    error = body.get("error") or {}
    answer = "".join(part.get("text", "") for item in body.get("output", [])
                     if item.get("type") == "message" for part in item.get("content", [])
                     if part.get("type") == "output_text")
    return {"http_status": response.status_code,
            "code": error.get("code") if error.get("code") in SAFE_CODES else None,
            "stage": error.get("stage") if error.get("stage") in SAFE_STAGES else None,
            "marker_match": answer.strip() == marker}


async def verify():
    if os.environ.get("AUTOBUILD_LIVE_VERIFY") != "1":
        raise RuntimeError("Live verification is opt-in")
    concurrency = int(os.environ.get("AUTOBUILD_LIVE_CONCURRENCY", "1"))
    if concurrency not in {1, 16}:
        raise RuntimeError("Unsupported diagnostic limit")
    root = Path(__file__).resolve().parents[2]
    origin = "http://127.0.0.1:8787"
    endpoint = "http://192.168.134.128:8788/v1/responses"
    marker = "GATEWAY_CONCURRENCY_OK"
    csrf = customer = key = None
    since = datetime.now(timezone.utc).isoformat()
    async with httpx.AsyncClient(base_url=origin, headers={"Origin": origin},
                                 trust_env=False, timeout=20) as admin:
        async def control(method, path, payload=None):
            response = await admin.request(method, path, json=payload,
                headers={"X-CSRF-Token": csrf} if csrf else {})
            if not response.is_success:
                raise RuntimeError("Admin operation failed")
            return response.json()

        try:
            token = json.loads((root / "data/local-gateway/admin-data/local-config.json").read_text())["admin_token"]
            csrf = (await control("POST", "/api/session", {"token": token}))["csrf_token"]
            keys = await control("GET", "/api/service/keys")
            now = datetime.now(timezone.utc)
            candidates = [row for row in keys if row["policy"]["enabled"] and not row["revoked_at"]
                          and (not row["policy"].get("expires_at")
                               or datetime.fromisoformat(row["policy"]["expires_at"]) > now)]
            if len(candidates) != 1:
                raise RuntimeError("Expected exactly one operator key")
            policy = dict(candidates[0]["policy"], concurrency=concurrency,
                          expires_at=(now + timedelta(minutes=10)).isoformat())
            customer = await control("POST", "/api/service/customers", {"name": "Disposable concurrency verification"})
            key = await control("POST", "/api/service/keys", {"customer_id": customer["id"],
                "name": "Disposable concurrency verification", "policy": policy})
            async with httpx.AsyncClient(trust_env=False, timeout=200) as public:
                async def request():
                    try:
                        response = await public.post(endpoint, headers={"Authorization": "Bearer " + key["secret"]},
                            json={"model": "gpt-5.6-luna", "input": "Reply exactly " + marker + ". Do not use tools.",
                                  "reasoning": {"effort": "low"}, "stream": False})
                        return response_summary(response, marker)
                    except Exception as exc:
                        return {"failure_class": type(exc).__name__}
                results = await asyncio.gather(request(), request())
            report = await control("GET", "/api/service/usage/requests?" + str(httpx.QueryParams({"from": since, "limit": 200})))
            rows = [row for row in report["items"] if row["key_id"] == key["key_id"]]
            summary = {"concurrency": concurrency, "results": results, "receipts": [
                {field: row.get(field) for field in ("status", "request_state", "usage", "charged_micro", "held_micro")}
                for row in rows]}
            print(json.dumps(summary), flush=True)
            statuses = sorted(row.get("http_status", 0) for row in results)
            expected = [200, 429] if concurrency == 1 else [200, 200]
            success = statuses == expected and all(row["marker_match"] for row in results if row.get("http_status") == 200)
            if concurrency == 1:
                success = success and any(row.get("code") == "concurrency_limit" and row.get("stage") == "quota"
                                          for row in results)
            success = success and len({row["request_id"] for row in rows}) == (1 if concurrency == 1 else 2)
            success = success and all(row["request_state"] == "completed" and row["status"] in {"completed", "rejected"}
                                      for row in rows)
            if not success:
                raise RuntimeError("Live concurrency verification did not meet acceptance")
        finally:
            try:
                if key:
                    await control("POST", f"/api/service/keys/{key['key_id']}/revoke", {"version": key["version"]})
                    print('{"cleanup":"key_revoked"}', flush=True)
            finally:
                if customer:
                    await control("DELETE", f"/api/service/customers/{customer['id']}", {"version": 1})
                    print('{"cleanup":"customer_disabled"}', flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(verify())
    except Exception as error:
        print(json.dumps({"failed_class": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
