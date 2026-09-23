import test from 'node:test';
import assert from 'node:assert/strict';
import {proxyPayload} from '../../autobuild_json/static/app.js';
test('Kiot batch payload never sends key as static proxy',()=>{
  assert.deepEqual(proxyPayload('kiotproxy','synthetic-key','nam','http'),{proxy_mode:'kiotproxy',proxies_text:'',kiot_keys_text:'synthetic-key',proxy_region:'nam',proxy_protocol:'http'});
  assert.equal(proxyPayload('direct','ignored').proxies_text,'');
  assert.equal(proxyPayload('legacy','proxy.invalid:80').proxies_text,'proxy.invalid:80');
});
