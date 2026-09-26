import test from 'node:test';
import assert from 'node:assert/strict';
import {ACCOUNT_PAGE_SIZES,pageNumbers,accountPagePath} from '../../autobuild_json/gateway/admin/static/codex-pagination.js';

test('account page controls remain bounded and contain current and edge pages',()=>{
  assert.deepEqual(ACCOUNT_PAGE_SIZES,[24,48,96]);
  assert.deepEqual(pageNumbers(1,1),[1]);
  assert.deepEqual(pageNumbers(1,21),[1,2,null,21]);
  assert.deepEqual(pageNumbers(21,21),[1,null,20,21]);
  assert.deepEqual(pageNumbers(10,21),[1,null,9,10,11,null,21]);
  assert(pageNumbers(500000,1000000).length<=7);
});

test('account page requests encode global search and status without a cursor',()=>{
  const url=new URL(accountPagePath(21,48,' test+%_@example.invalid ','disabled'),'http://local');
  assert.equal(url.pathname,'/api/service/oauth-accounts');
  assert.deepEqual(Object.fromEntries(url.searchParams),{page:'21',limit:'48',q:'test+%_@example.invalid',status:'disabled'});
  assert.deepEqual(Object.fromEntries(new URL(accountPagePath(1,24),'http://local').searchParams),{page:'1',limit:'24'});
});
