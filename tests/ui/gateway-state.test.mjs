import test from 'node:test';
import assert from 'node:assert/strict';
import {decimalToMicro, microToDecimal, maskedSecret, createApi} from '../../autobuild_json/gateway/admin/static/api.js';

test('quota arithmetic preserves large integers and six decimal places',()=>{
  assert.equal(decimalToMicro('100000000000000.123456'),'100000000000000123456');
  assert.equal(microToDecimal('100000000000000123456'),'100000000000000.123456');
  assert.equal(decimalToMicro('0'),'0');
  for(const x of ['NaN','1e9','-1','0.0000001']) assert.throws(()=>decimalToMicro(x));
});
test('stored secret displays only the nonsecret label',()=>assert.equal(maskedSecret('abgw_123'),'abgw_123 ••••••••'));
test('logout invalidates an in-flight response',async()=>{
  let finish;
  const api=createApi(()=>new Promise(resolve=>{finish=resolve;}));
  api.setSession('csrf');
  const pending=api.request('/api/service/keys');
  api.clear();
  finish({ok:true,status:200,json:async()=>[{name:'stale'}]});
  await assert.rejects(pending,/STALE_SESSION/);
});
