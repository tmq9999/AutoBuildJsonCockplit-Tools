import test from 'node:test';
import assert from 'node:assert/strict';
import {mergeReportAccountOptions,reportAccountFilters} from '../../autobuild_json/gateway/admin/static/codex-view.js';

test('report account selection keeps stable credential UUIDs across account pages',()=>{
  const alice={id:'credential-alice',email:'alice@example.invalid'};
  const bob={id:'credential-bob',email:'bob@example.invalid'};
  let options=mergeReportAccountOptions(new Map(),[alice]);
  const selected=alice.id;

  options=mergeReportAccountOptions(options,[bob]);

  assert.deepEqual([...options],[
    [alice.id,alice.email],
    [bob.id,bob.email]
  ]);
  assert.equal(options.get(selected),alice.email,'the selected off-page account keeps its label');
  assert.deepEqual(reportAccountFilters(selected),{credential_id:alice.id},'submission must not substitute the same index from Bob\'s page');
  assert.deepEqual(reportAccountFilters(bob.id),{credential_id:bob.id},'the newly loaded page account is selectable');
  assert.deepEqual(reportAccountFilters(''),{});
});
