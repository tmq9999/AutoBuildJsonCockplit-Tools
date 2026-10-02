"""Opt-in real Codex CLI acceptance; prints only safe summaries, never raw bodies."""
import asyncio
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import re
import tempfile

import httpx

from autobuild_json.gateway.errors import SAFE_CODES


def client_diagnostics(stdout, stderr):
    """Return allowlisted hints only; neither CLI nor provider bodies are output."""
    messages = []
    for line in stdout.splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict) and event.get("type") in {"error", "turn.failed"}:
            messages.append(json.dumps(event).lower())
    # The live bounded-admission worktree also exposes gateway_busy.
    codes = SAFE_CODES | {"gateway_busy"}
    patterns = {
        "stream_disconnected": ("stream disconnected", "stream closed", "stream ended"),
        "response_decode": ("error decoding response body",),
        "request_transport": ("error sending request", "connection reset", "connection refused"),
        "response_schema": ("missing field", "unknown variant", "invalid type", "failed to parse"),
        "timeout": ("timed out", "timeout"),
        "missing_key_environment": ("missing environment variable",),
        "local_permission": ("permission denied", "operation not permitted"),
    }
    found_codes, hints = set(), set()
    unclassified = False
    stderr_message = stderr.decode(errors="replace").lower()
    for index, message in enumerate([*messages, stderr_message]):
        matched_codes = codes.intersection(re.findall(r"\b[a-z0-9_]+\b", message))
        matched_hints = {label for label, fragments in patterns.items()
                         if any(fragment in message for fragment in fragments)}
        matched_hints.update("http_" + status for status in re.findall(
            r"\b(?:http(?:/[0-9.]+)?|status(?: code)?)\s*:?\s+([45][0-9]{2})\b", message))
        found_codes.update(matched_codes)
        hints.update(matched_hints)
        if index < len(messages) and not (matched_codes or matched_hints):
            unclassified = True
        if index == len(messages) and stderr_message and not (matched_codes or matched_hints):
            unclassified = True
    return {"safe_client_errors": sorted(found_codes), "failure_hints": sorted(hints),
            "unclassified_client_error": unclassified}


async def verify():
    if os.environ.get("AUTOBUILD_LIVE_VERIFY") != "1":
        raise RuntimeError("Live verification requires AUTOBUILD_LIVE_VERIFY=1")
    root = Path(os.environ.get("AUTOBUILD_LIVE_RUNTIME_ROOT", ".")).resolve()
    endpoint = os.environ.get("AUTOBUILD_LIVE_BASE_URL", "http://192.168.134.128:8788/v1")
    mode = os.environ.get("AUTOBUILD_LIVE_CODEX_MODE", "text")
    if mode not in {"text", "tool"}:
        raise RuntimeError("Live mode must be text or tool")
    marker = "LIVE_GATEWAY_CODEX_TOOL_OK" if mode == "tool" else "LIVE_GATEWAY_CODEX_OK"
    prompt = ("Run exactly one harmless terminal command: printf 'GATEWAY_TOOL_PROBE\\n'. "
              "After reading its output, reply exactly " + marker + ". Do not read or modify files, "
              "use network, or delegate to other agents." if mode == "tool" else
              "Reply exactly " + marker + ". Do not use tools.")
    customer = key = None
    csrf = None
    since = datetime.now(timezone.utc).isoformat()
    origin = "http://127.0.0.1:8787"
    async with httpx.AsyncClient(base_url=origin, headers={"Origin": origin},
                                 trust_env=False, timeout=30) as admin:
        async def control(method, path, payload=None):
            response = await admin.request(method, path, json=payload,
                headers={"X-CSRF-Token": csrf} if csrf else {})
            if not response.is_success:
                raise RuntimeError(f"Admin HTTP {response.status_code}")
            return response.json()

        try:
            config = json.loads((root / "data/local-gateway/admin-data/local-config.json").read_text())
            csrf = (await control("POST", "/api/session", {"token": config["admin_token"]}))["csrf_token"]
            customer = await control("POST", "/api/service/customers", {"name": "Disposable Codex CLI acceptance"})
            key = await control("POST", "/api/service/keys", {"customer_id": customer["id"],
                "name": "Disposable CLI " + mode, "policy": {"model_ids": ["gpt-6-astra"],
                    "protocols": ["openai"], "total_micro": "100000000000000",
                    "expires_at": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()}})
            with tempfile.TemporaryDirectory(prefix="abgw-codex-cli-") as workdir:
                command = ["codex", "exec", "--ephemeral", "--skip-git-repo-check", "--json",
                    "--color", "never", "--sandbox", "read-only", "-m", "gpt-6-astra", "-C", workdir,
                    "-c", 'model_provider="gateway_live"',
                    "-c", 'model_providers.gateway_live.name="Gateway live acceptance"',
                    "-c", "model_providers.gateway_live.base_url=" + json.dumps(endpoint),
                    "-c", 'model_providers.gateway_live.wire_api="responses"',
                    "-c", 'model_providers.gateway_live.env_key="AUTOBUILD_LIVE_CLIENT_KEY"',
                    "-c", "model_providers.gateway_live.requires_openai_auth=false",
                    "-c", "model_providers.gateway_live.supports_websockets=false",
                    "-c", "model_providers.gateway_live.request_max_retries=0",
                    "-c", "model_providers.gateway_live.stream_max_retries=0",
                    "-c", 'mcp_servers.9remote.enabled=false', prompt]
                process = await asyncio.create_subprocess_exec(*command,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
                    env={**os.environ, "AUTOBUILD_LIVE_CLIENT_KEY": key["secret"]})
                try:
                    stdout, stderr = await asyncio.wait_for(process.communicate(), 220)
                except BaseException:
                    if process.returncode is None:
                        process.kill()
                        await process.communicate()
                    raise
            messages, tool_count = [], 0
            event_types, item_types = set(), set()
            command_output_match = False
            for line in stdout.splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                event_types.add(event.get("type"))
                if event.get("type") == "item.completed":
                    item = event.get("item", {})
                    item_types.add(item.get("type"))
                    if item.get("type") == "agent_message":
                        messages.append(item.get("text", ""))
                    if item.get("type") in {"command_execution", "tool_call", "mcp_tool_call"}:
                        tool_count += 1
                    if item.get("type") == "command_execution":
                        command_output_match |= (
                            item.get("exit_code") == 0
                            and item.get("aggregated_output", "").strip() == "GATEWAY_TOOL_PROBE"
                        )
            report = await control("GET", "/api/service/usage/requests?" + str(httpx.QueryParams({"from": since, "limit": 100})))
            rows = [row for row in report["items"] if row["key_id"] == key["key_id"]]
            summary = {"mode": mode, "endpoint": endpoint, "codex_exit": process.returncode,
                "marker_match": bool(messages and messages[-1].strip() == marker),
                "final_marker": marker if messages and messages[-1].strip() == marker else None,
                "command_items": tool_count, **client_diagnostics(stdout, stderr),
                "command_output_match": command_output_match,
                "event_types": sorted(value for value in event_types if isinstance(value, str)),
                "item_types": sorted(value for value in item_types if isinstance(value, str)),
                "requests": [{"status": row["status"], "request_state": row.get("request_state"),
                              "usage": row.get("usage"), "charged_micro": row.get("charged_micro"),
                              "held_micro": row.get("held_micro")}
                             for row in rows]}
            print(json.dumps(summary), flush=True)
            if process.returncode != 0 or not summary["marker_match"]:
                raise RuntimeError("Codex CLI acceptance failed")
            if mode == "tool" and (not command_output_match or tool_count != 1):
                raise RuntimeError("Codex CLI tool execution not verified")
            if mode == "text" and tool_count:
                raise RuntimeError("Unexpected tool execution in text-only test")
            # These rows are provider attempts, not distinct client requests:
            # an explicit pre-generation rejection may precede the completed
            # attempt on the same request. No ambiguous/pending attempt passes.
            grouped = {}
            for row in rows:
                grouped.setdefault(row["request_id"], []).append(row)
            if len(grouped) != (2 if mode == "tool" else 1) or any(
                sum(row["status"] == "completed" and bool(row.get("usage")) for row in attempts) != 1
                or any(row["request_state"] != "completed" or row["status"] not in {"completed", "rejected"}
                       for row in attempts)
                for attempts in grouped.values()
            ):
                raise RuntimeError("Codex CLI ledger acceptance failed")
        finally:
            cleanup_failed = False
            if key:
                try:
                    await control("POST", f"/api/service/keys/{key['key_id']}/revoke", {"version": key["version"]})
                    print('{"cleanup":"key_revoked"}', flush=True)
                except Exception:
                    cleanup_failed = True
                    print('{"cleanup":"key_revoke_failed"}', flush=True)
            if customer:
                try:
                    await control("DELETE", f"/api/service/customers/{customer['id']}", {"version": 1})
                    print('{"cleanup":"customer_disabled"}', flush=True)
                except Exception:
                    cleanup_failed = True
                    print('{"cleanup":"customer_disable_failed"}', flush=True)
            if cleanup_failed:
                raise RuntimeError("Live verification cleanup incomplete")


if __name__ == "__main__":
    try:
        asyncio.run(verify())
    except Exception as error:
        print(json.dumps({"failed": type(error).__name__}), flush=True)
        raise SystemExit(1) from None
