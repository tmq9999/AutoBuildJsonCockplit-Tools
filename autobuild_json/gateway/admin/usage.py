from datetime import datetime, timedelta, timezone
from uuid import UUID

from sqlalchemy import text

from ..errors import GatewayError


def report_window(start=None, end=None):
    for value in (start, end):
        if value is not None and (value.tzinfo is None or value.utcoffset() is None):
            raise GatewayError("invalid_request", 422)
    try:
        end = (end or datetime.now(timezone.utc)).astimezone(timezone.utc)
        start = start.astimezone(timezone.utc) if start else end - timedelta(hours=24)
    except (OverflowError, ValueError):
        raise GatewayError("invalid_request", 422) from None
    if start > end or end - start > timedelta(days=31):
        raise GatewayError("invalid_request", 422)
    return start, end


# Unknown attribution is one explicit group, sorted before UUID accounts. This
# cursor sentinel cannot be confused with a missing cursor (None).
UNKNOWN_ACCOUNT = UUID(int=0)
COUNTERS = ("input_tokens", "output_tokens", "cached_read", "cached_write", "reasoning")


async def usage_summary(session, *, start=None, end=None, credential_id=None, model_id=None):
    """One row per request, exact settled usage, never a sum of report pages.

    Failed attempts cannot erase known usage or be charged twice. Unknown
    legacy/completion data is counted separately rather than invented as zero.
    Input includes cached subsets; output includes reasoning subsets.
    """
    sums = ','.join(f"CASE WHEN count(*) FILTER (WHERE usage IS NOT NULL)=0 THEN '0' "
                    f"WHEN count(usage->>'{name}')=count(*) FILTER (WHERE usage IS NOT NULL) "
                    f"THEN sum((usage->>'{name}')::numeric)::text ELSE NULL END AS {name}" for name in COUNTERS)
    row = (await session.execute(text("""WITH receipts AS (
        SELECT CASE WHEN CAST(:credential AS uuid) IS NOT NULL AND scoped.status='rejected'
            THEN 'released' ELSE r.state END AS state,
            CASE WHEN r.state='completed' AND a.n=1 AND l.id IS NOT NULL
                AND (CAST(:credential AS uuid) IS NULL OR a.credential_id=:credential) THEN a.usage END AS usage,
            CASE WHEN r.state='completed' AND a.n=1
                AND (CAST(:credential AS uuid) IS NULL OR a.credential_id=:credential) THEN l.amount_micro END AS charged_micro,
            CASE WHEN r.state IN ('reserved','dispatched','usage_pending')
                AND (CAST(:credential AS uuid) IS NULL OR latest.credential_id=:credential)
                THEN r.hold ELSE 0 END AS held_micro
        FROM requests r
        LEFT JOIN LATERAL (SELECT x.usage,x.credential_id,count(*) OVER () AS n FROM attempts x
            WHERE x.request_id=r.id AND x.status='completed' ORDER BY x.started_at DESC,x.id DESC LIMIT 1) a ON true
        LEFT JOIN LATERAL (SELECT x.credential_id FROM attempts x WHERE x.request_id=r.id
            ORDER BY x.started_at DESC,x.id DESC LIMIT 1) latest ON true
        LEFT JOIN LATERAL (SELECT x.status FROM attempts x
            WHERE x.request_id=r.id AND x.credential_id=:credential
            ORDER BY x.started_at DESC,x.id DESC LIMIT 1) scoped ON true
        LEFT JOIN usage_ledger l ON l.request_id=r.id AND l.entry_kind='settlement'
        WHERE (CAST(:start AS timestamptz) IS NULL OR r.admitted_at>=:start)
            AND (CAST(:end AS timestamptz) IS NULL OR r.admitted_at<=:end)
            AND (CAST(:model AS text) IS NULL OR r.model_id=:model)
            AND (CAST(:credential AS uuid) IS NULL OR scoped.status IS NOT NULL)
    ) SELECT count(*) AS requests,
        count(*) FILTER (WHERE state='completed') AS completed,
        count(*) FILTER (WHERE state='released') AS failed,
        count(*) FILTER (WHERE state IN ('reserved','dispatched','usage_pending')) AS pending,
        count(*) FILTER (WHERE state='completed' AND (usage->>'input_tokens' IS NULL
            OR usage->>'output_tokens' IS NULL)) AS unknown_usage_requests,
        count(*) FILTER (WHERE state='completed' AND (usage->>'cached_read' IS NULL
            OR usage->>'cached_write' IS NULL OR usage->>'reasoning' IS NULL)) AS unknown_detail_requests,
        COALESCE(sum(charged_micro),0)::text AS charged_micro,
        COALESCE(sum(held_micro),0)::text AS held_micro,""" + sums + " FROM receipts"),
        {'start': start, 'end': end, 'model': model_id, 'credential': credential_id})).mappings().one()
    result = dict(row)
    result['total_tokens'] = (str(int(result['input_tokens']) + int(result['output_tokens']))
                              if result['input_tokens'] is not None and result['output_tokens'] is not None else None)
    result['uncached_input_tokens'] = (str(int(result['input_tokens'])-int(result['cached_read'])-int(result['cached_write']))
                                       if all(result[name] is not None for name in ('input_tokens','cached_read','cached_write')) else None)
    return result

# Serving attribution is based exclusively on the immutable attempt snapshot.
# A settlement is joined only to one unambiguous completed attempt; retries,
# pending attempts and ambiguous legacy completions cannot receive that charge.
REPORT_ROWS = """WITH report_rows AS (
    SELECT a.id,a.request_id,a.provider_id,a.credential_id,a.binding_id,a.config_version,
        r.key_id,r.model_id,r.protocol,r.state AS request_state,a.status,a.started_at,r.admitted_at,
        a.usage,a.cost AS upstream_cost,
        COALESCE(a.credential_id,'00000000-0000-0000-0000-000000000000'::uuid) AS account_cursor,
        CASE WHEN a.credential_id IS NULL THEN 'unknown' ELSE 'snapshot' END AS attribution,
        CASE WHEN a.id=serving.id AND serving.completed_count=1 AND r.state='completed'
            THEN COALESCE(l.amount_micro,0) ELSE 0 END AS charged_micro,
        CASE WHEN a.usage IS NOT NULL THEN
            ((a.usage->>'input_tokens')::numeric-COALESCE((a.usage->>'cached_read')::numeric,0)
                -COALESCE((a.usage->>'cached_write')::numeric,0))*r.input_micro
            +COALESCE((a.usage->>'cached_read')::numeric,0)*r.cache_read_micro
            +COALESCE((a.usage->>'cached_write')::numeric,0)*r.cache_write_micro
            +(a.usage->>'output_tokens')::numeric*r.output_micro ELSE NULL END AS computed_micro,
        CASE WHEN r.state IN ('reserved','dispatched','usage_pending') AND a.id=latest.id
            THEN r.hold ELSE 0 END AS held_micro
    FROM attempts a JOIN requests r ON r.id=a.request_id
    LEFT JOIN usage_ledger l ON l.request_id=r.id AND l.entry_kind='settlement'
    LEFT JOIN LATERAL (SELECT x.id,count(*) OVER () AS completed_count FROM attempts x
        WHERE x.request_id=r.id AND x.status='completed' ORDER BY x.started_at DESC,x.id DESC LIMIT 1) serving ON true
    LEFT JOIN LATERAL (SELECT x.id FROM attempts x WHERE x.request_id=r.id
        ORDER BY x.started_at DESC,x.id DESC LIMIT 1) latest ON true
    WHERE r.admitted_at>=:start AND r.admitted_at<=:end
        AND (CAST(:credential AS uuid) IS NULL OR a.credential_id=CAST(:credential AS uuid))
        AND (CAST(:model AS text) IS NULL OR r.model_id=CAST(:model AS text))
) """


def _page(rows, limit, cursor):
    return {"items": rows[:limit], "next_after": rows[limit - 1][cursor] if len(rows) > limit else None}


class UsageReports:
    def __init__(self, db):
        self.db = db

    async def report_requests(self, credential_id=None, model_id=None, start=None, end=None, limit=100, after=None):
        start, end = report_window(start, end)
        async with self.db.sessions.begin() as session:
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            summary = await usage_summary(session, start=start, end=end, credential_id=credential_id, model_id=model_id)
            rows = (await session.execute(text(REPORT_ROWS + "SELECT * FROM report_rows "
                "WHERE (CAST(:after AS uuid) IS NULL OR id>CAST(:after AS uuid)) ORDER BY id LIMIT :limit"),
                {"credential": credential_id, "model": model_id, "start": start, "end": end,
                 "after": after, "limit": limit + 1})).mappings().all()
        items = []
        for row in rows:
            value = dict(row)
            value.pop("account_cursor")
            for name in ("charged_micro", "computed_micro", "held_micro"):
                value[name] = str(value[name]) if value[name] is not None else None
            if value["usage"] is not None:
                value["usage"] = {name: str(value["usage"][name]) if name in value["usage"] else None
                                  for name in COUNTERS}
            items.append(value)
        return dict(_page(items, limit, "id"), **{"from": start, "to": end, "summary": summary})

    async def report_accounts(self, credential_id=None, model_id=None, start=None, end=None, limit=100, after=None):
        start, end = report_window(start, end)
        sums = ",".join(f"SUM((usage->>'{name}')::numeric) AS {name}" for name in COUNTERS)
        sql = REPORT_ROWS + """, groups AS (
            SELECT account_cursor,credential_id,attribution,COUNT(*) AS attempt_count,
                COUNT(*) FILTER (WHERE status='completed') AS completed_count,
                COUNT(*) FILTER (WHERE usage IS NULL) AS unknown_usage_count,
                COUNT(*) FILTER (WHERE upstream_cost IS NULL) AS unknown_cost_count,
                SUM(charged_micro) AS charged_micro,SUM(computed_micro) AS computed_micro,
                SUM(held_micro) AS held_micro,""" + sums + """ FROM report_rows
            GROUP BY account_cursor,credential_id,attribution
        ), cost_groups AS (
            SELECT account_cursor,upstream_cost->>'currency' AS currency,
                SUM((upstream_cost->>'amount')::numeric)::text AS amount FROM report_rows
            WHERE upstream_cost IS NOT NULL GROUP BY account_cursor,upstream_cost->>'currency'
        ) SELECT g.*,COALESCE((SELECT jsonb_agg(jsonb_build_object('currency',currency,'amount',amount)
            ORDER BY currency) FROM cost_groups c WHERE c.account_cursor=g.account_cursor),'[]'::jsonb) AS upstream_costs
        FROM groups g WHERE (CAST(:after AS uuid) IS NULL OR account_cursor>CAST(:after AS uuid))
        ORDER BY account_cursor LIMIT :limit"""
        async with self.db.sessions.begin() as session:
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY'))
            summary = await usage_summary(session, start=start, end=end, credential_id=credential_id, model_id=model_id)
            rows = (await session.execute(text(sql), {"credential": credential_id, "model": model_id,
                "start": start, "end": end, "after": after, "limit": limit + 1})).mappings().all()
        items = []
        for row in rows:
            value = dict(row)
            for name in (*COUNTERS, "attempt_count", "completed_count", "unknown_usage_count", "unknown_cost_count",
                         "charged_micro", "computed_micro", "held_micro"):
                value[name] = str(value[name]) if value[name] is not None else None
            items.append(value)
        return dict(_page(items, limit, "account_cursor"), **{"from": start, "to": end, "summary": summary})

    async def requests(self, limit=100):
        async with self.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,key_id,model_id,protocol,state,admitted_at,"
                "hold,usage,reason FROM requests ORDER BY admitted_at DESC LIMIT :limit"), {"limit": min(500, max(1, limit))})).mappings()]


    async def overview(self):
        async with self.db.sessions() as session:
            return await usage_summary(session)

    async def audit(self):
        async with self.db.sessions() as session:
            return [dict(row) for row in (await session.execute(text("SELECT id,actor,action,record_id,details,created_at "
                "FROM audit_events ORDER BY created_at DESC LIMIT 200"))).mappings()]
