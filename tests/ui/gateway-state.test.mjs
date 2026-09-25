import test from 'node:test';
import assert from 'node:assert/strict';
import {decimalToMicro, microToDecimal, parseQuotaInput, maskedSecret, createApi} from '../../autobuild_json/gateway/admin/static/api.js';
import {keyPayload} from '../../autobuild_json/gateway/admin/static/keys.js';

test('quota shorthand converts token units before micro-unit encoding',()=>{
  assert.equal(parseQuotaInput('1m'),'1000000');
  assert.equal(parseQuotaInput('100m'),'100000000');
  assert.equal(parseQuotaInput('1b'),'1000000000');
  assert.equal(parseQuotaInput(' 1.5 M '),'1500000');
  assert.equal(decimalToMicro(parseQuotaInput('1m')),'1000000000000');
  assert.equal(decimalToMicro(parseQuotaInput('1b')),'1000000000000000');
  for(const value of ['NaN','-1m','1e9','1.2.3m','1x']) assert.throws(()=>parseQuotaInput(value));
});

test('quota shorthand keeps exact digits beyond Number precision',()=>{
  for(const [input,expected] of [
    ['1000000','1000000'], ['1M','1000000'], ['1B','1000000000'],
    ['2k','2000'], ['1.25b','1250000000'], ['0m','0'],
    ['0.000001m','1'], ['0.000001','0.000001'],
    ['9007199254740993m','9007199254740993000000'],
    ['100000000000000.123456','100000000000000.123456'],
  ]) assert.equal(parseQuotaInput(input),expected);
});

test('quota parser rejects overflow before preview can advertise an unsaveable limit',()=>{
  assert.equal(parseQuotaInput('99999999999999999999999999999999.999999'),
    '99999999999999999999999999999999.999999');
  for(const input of ['100000000000000000000000000000000','100000000000000000000000000000000m'])
    assert.throws(()=>parseQuotaInput(input),/Giá trị quá lớn/);
  for(const input of ['', '1 000', '1mm', 'Infinity', '1.0000001m', '<script>'])
    assert.throws(()=>parseQuotaInput(input));
});

test('key payload converts shorthand for creation and edits without making zero unlimited',()=>{
  const fields={name:{value:'Test quota'},customer_id:{value:'test-customer'},
    total:{value:'100m'},day:{value:'0'},month:{value:''},rpm:{value:'60'},concurrency:{value:'1'}};
  const form={elements:{namedItem:name=>fields[name]}};
  const created=keyPayload(form);
  assert.equal(created.policy.total_micro,'100000000000000');
  assert.equal(created.policy.day_micro,'0');
  assert.equal(created.policy.month_micro,null);
  assert.deepEqual(created.policy.protocols,['openai','anthropic','gemini','ollama']);
  assert.equal(created.policy.all_models,true);
  fields.total.value='1b';
  assert.equal(keyPayload(form,{version:2}).policy.total_micro,'1000000000000000');
  fields.total.value='-1m';assert.throws(()=>keyPayload(form));
});

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
