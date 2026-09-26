import test from 'node:test';
import assert from 'node:assert/strict';
import {reportSummary,requestTrend} from '../../autobuild_json/gateway/admin/static/codex-report.js';
test('report totals preserve exact integers and never add cached tokens twice',()=>{
  const data=[{status:'completed',usage:{input_tokens:'9007199254740993',cached_read:'600',cached_write:'100',output_tokens:'200'},charged_micro:'1085000000'}];
  assert.deepEqual(reportSummary(data),{attempts:1n,completed:1n,input:9007199254740993n,output:200n,charged:1085000000n});
});
test('missing report usage is unknown rather than a zero total',()=>{
  const s=reportSummary([{status:'failed',usage:null,charged_micro:'0'}]);assert.equal(s.input,null);assert.equal(s.output,null);assert.equal(s.charged,0n);
  assert.equal(reportSummary([]).input,0n);
});
test('account summaries use attempt counts not the number of account rows',()=>{
  assert.deepEqual(reportSummary([{attempt_count:5,completed_count:3,input_tokens:'10',output_tokens:'2',charged_micro:'8'}],'accounts'),{attempts:5n,completed:3n,input:10n,output:2n,charged:8n});
});
test('trend places both endpoints and excludes unknown/outside timestamps',()=>{
  const start='2026-09-01T00:00:00Z',end='2026-09-02T00:00:00Z';
  assert.deepEqual(requestTrend([{started_at:start},{started_at:end},{started_at:'bad'},{started_at:'2020-01-01'}],start,end,2),[1,1]);
  assert.deepEqual(requestTrend([],end,start),[]);
});
test('trend retains a request admitted in the window but dispatched after it',()=>{
  const start='2026-09-01T00:00:00Z',end='2026-09-01T00:01:00Z';
  assert.deepEqual(requestTrend([
    {admitted_at:start,started_at:'2026-09-01T00:02:00Z'},
    {admitted_at:'2026-08-31T23:59:59Z',started_at:end},
  ],start,end,2),[1,0]);
});
