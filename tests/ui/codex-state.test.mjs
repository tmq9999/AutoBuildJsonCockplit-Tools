import assert from 'node:assert/strict';
import test from 'node:test';
import {canConsumeReset,quotaLabel,createCodexState,weightedMicro,rateValue,parseOverrides} from '../../autobuild_json/gateway/admin/static/codex-state.js';
import {createCodexActions} from '../../autobuild_json/gateway/admin/static/codex-actions.js';
import * as codexState from '../../autobuild_json/gateway/admin/static/codex-state.js';

class FakeElement {
  constructor(tag){this.tagName=tag.toUpperCase();this.children=[];this.attributes={};this.value='';this.checked=false;}
  setAttribute(name,value){this.attributes[name]=String(value);}
  append(...children){this.children.push(...children);}
  prepend(child){this.children.unshift(child);}
}
globalThis.document={createElement:tag=>new FakeElement(tag)};
const {field}=await import('../../autobuild_json/gateway/admin/static/dom.js');

test('uncertain server reset states never offer another consume',()=>{
  for(const operation_state of ['prepared','dispatched','unknown','succeeded_refresh_failed']) assert.equal(canConsumeReset({available_count:2,fresh:true,operation_state}),false);
  assert.equal(canConsumeReset({available_count:2,fresh:true,operation_state:null}),true);
  assert.equal(canConsumeReset({available_count:2,fresh:false}),false);
  assert.equal(canConsumeReset({available_count:null,fresh:true}),false);
});
test('missing quota is unknown, not zero or full',()=>{
  assert.equal(quotaLabel(null),'Không rõ');
  assert.equal(quotaLabel({used_percent:null}),'Không rõ');
  assert.equal(quotaLabel({used_percent:'0'}),'0% đã dùng');
});
test('window labels use actual duration and exact remaining percent, including unknown',()=>{
  assert.equal(typeof codexState.windowDuration,'function');
  for(const [seconds,label] of [[18000,'5 giờ'],[604800,'7 ngày'],[3600,'1 giờ'],[90,'90 giây'],[null,'Không rõ']])assert.equal(codexState.windowDuration(seconds),label);
  for(const [used,left] of [['42','58'],['0','100'],['100','0'],['18.125','81.875'],[null,null]])assert.equal(codexState.remainingPercent(used),left);
});
test('selection and logout epochs reject late account reads',()=>{
  const state=createCodexState();state.select('a');const ticket=state.ticket();state.select('b');
  assert.equal(state.accept(ticket,{usage:{snapshot:'wrong'}}),false);assert.equal(state.usage,null);
  const next=state.ticket();state.clear();assert.equal(state.accept(next,{usage:{snapshot:'wrong'}}),false);
});
test('same confirmed UUID survives ambiguous failure and cannot create a new reset',async()=>{
  const state=createCodexState();state.select('a');state.credits={snapshot:{version:7,available_count:2},stale:false,active_reset:null};
  let calls=0;const actions=createCodexActions({request:async()=>{calls++;throw new Error('network');}},state,()=>{},()=> 'confirmed-uuid');
  actions.confirm();await assert.rejects(actions.consume(),/network/);assert.equal(calls,1);
  assert.deepEqual(state.confirmed,{request_id:'confirmed-uuid',credits_version:7,acknowledge:true});
  assert.equal(state.operation.state,'unknown');assert.throws(()=>actions.confirm());
  await assert.rejects(actions.consume());assert.equal(calls,1);
});
test('storage GET cannot silently clear locally ambiguous reset',()=>{
  const s=createCodexState();s.select('a');s.operation={state:'unknown'};s.confirmed={request_id:'same'};
  s.accept(s.ticket(),{credits:{snapshot:{version:8,available_count:2},stale:false,active_reset:null}});
  assert.equal(s.operation.state,'unknown');assert.equal(s.confirmed.request_id,'same');
});
test('server authoritative refresh-failed then succeeded updates state without second consume',async()=>{
  const s=createCodexState();s.select('a');s.credits={snapshot:{version:7,available_count:2},stale:false};let posts=0;
  const actions=createCodexActions({request:async(path)=>{
    if(path.endsWith('/consume')){posts++;return {operation_id:'id',state:'succeeded_refresh_failed',version:2};}
    if(path.endsWith('/quota/refresh'))return {usage:{snapshot:null,stale:true},credits:{snapshot:{version:8,available_count:1},stale:false,active_reset:{operation_id:'id',state:'succeeded',version:3}}};
  }},s,()=>{},()=> 'id');
  actions.confirm();await actions.consume();assert.equal(s.operation.state,'succeeded_refresh_failed');
  await actions.refresh();assert.equal(s.operation.state,'succeeded');assert.equal(posts,1);
});
test('100m example subtracts cached subsets exactly once with BigInt',()=>{
  const charged=weightedMicro({input_tokens:'1000',cached_read:'200',cached_write:'100',output_tokens:'100'},{input_micro:'1000000',cache_read_micro:'100000',cache_write_micro:'1500000',output_micro:'2000000'});
  assert.equal(charged,'1070000000');assert.equal((100000000000000n-BigInt(charged)).toString(),'99998930000000');
  assert.equal(weightedMicro({input_tokens:'9007199254740993',output_tokens:'0'},{input_micro:'1',output_micro:'1'}),'9007199254740993');
});

test('approved weighted quota example keeps 1085 spent and restores 99998915 available',()=>{
  const total='100000000';
  const spent=weightedMicro({input_tokens:'1000',cached_read:'600',cached_write:'100',output_tokens:'200'},
    {input_micro:'1000000',cache_read_micro:'100000',cache_write_micro:'1250000',output_micro:'3000000'});
  assert.equal(spent,'1085000000');
  assert.equal((BigInt(total)*1000000n-BigInt(spent)).toString(),'99998915000000');
  assert.equal((BigInt(total)*1000000n-BigInt(spent)+BigInt(spent)).toString(),'100000000000000');
});

test('omitted cache rates inherit input and produce 1200 when every effective rate is one',()=>{
  assert.equal(weightedMicro({input_tokens:'1000',cached_read:'600',cached_write:'100',output_tokens:'200'},
    {input_micro:'1000000',cache_read_micro:null,cache_write_micro:null,output_micro:'1000000'}),'1200000000');
});
test('cache inheritance is null and explicit zero remains zero',()=>{
  assert.equal(rateValue(''),null);assert.equal(rateValue('0'),'0');
  assert.deepEqual(parseOverrides('m|1|2|0|'),[{model_id:'m',input_micro:'1000000',output_micro:'2000000',cache_read_micro:'0',cache_write_micro:null}]);
  assert.deepEqual(parseOverrides('legacy|1|2'),[{model_id:'legacy',input_micro:'1000000',output_micro:'2000000',cache_read_micro:null,cache_write_micro:null}]);
});

test('quota grant conversion stays decimal-string exact beyond safe integer range',()=>{
  return import('../../autobuild_json/gateway/admin/static/codex-state.js').then(mod=>{
    assert.equal(typeof mod.quotaAmountMicro,'function');
    assert.equal(mod.quotaAmountMicro('100000000000000.123456'),'100000000000000123456');
    assert.equal(mod.formatMicro('100000000000000123456'),'100000000000000.123456');
    assert.throws(()=>mod.quotaAmountMicro('100000000000000000000000000000000000000'),/too large|Giá trị quá lớn/i);
  });
});

test('pool and grant actions use exact backend paths and never retry POST',async()=>{
  const state=createCodexState();state.select('account');
  const calls=[];
  const api={request:async(path,options={})=>{calls.push([path,options]);return {version:2};}};
  const actions=createCodexActions(api,state,()=>{},()=> 'fixed-request-id');
  await actions.savePool('vendor/model:v1',{version:1,policy:{mode:'auto',members:[]}});
  await actions.grant('key-id',{version:4,amount:'100000000000000.123456',reason:'reviewed grant'});
  assert.deepEqual(calls.map(([path])=>path),['/api/service/account-pools/vendor/model%3Av1','/api/service/keys/key-id/quota-adjust']);
  assert.equal(calls[1][1].body.amount_micro,'100000000000000123456');
  assert.equal(calls[1][1].body.request_id,'fixed-request-id');
  assert.equal(calls[1][1].method,'POST');
});

test('leaving during an account read drops its result but releases the old busy flag',async()=>{
  const state=createCodexState();state.select('a');let release;
  const gate=new Promise(r=>release=r);
  const actions=createCodexActions({request:async()=>{await gate;return {snapshot:null,stale:true,active_reset:null};}},state,()=>{});
  const pending=actions.read();state.select('b');release();await pending;state.select('a');
  assert.equal(state.usage,null);assert.equal(state.busy,false);
});

test('pool settings and customer grants do not require selecting an OAuth account',async()=>{
  const state=createCodexState(),calls=[];
  const actions=createCodexActions({request:async(path,body)=>{calls.push({path,body});return {version:1};}},state,()=>{},()=> 'grant-uuid');
  await actions.savePool('model',{version:0,policy:{members:[]}});
  await actions.grant('key',{version:1,amount:'1085',reason:'Synthetic grant'});
  assert.equal(calls.length,2);assert.equal(calls[1].body.body.amount_micro,'1085000000');
});

test('failed grant sends only one POST even for a network error',async()=>{
  const state=createCodexState();state.select('a');let posts=0;
  const actions=createCodexActions({request:async()=>{posts++;throw Error('network');}},state,()=>{});
  await assert.rejects(actions.grant('key',{version:1,amount:'0',reason:'Audit noop'}),/network/);
  assert.equal(posts,1);
});

test('select fields default to their first choice and preserve explicit values',()=>{
  const form=new FakeElement('form');
  const first=field(form,'health','Health',{choices:[['all','Tất cả'],['active','active']]});
  assert.equal(first.value,'all');
  const explicit=field(form,'mode','Mode',{choices:[['auto','auto'],['single','single']],value:'single'});
  assert.equal(explicit.value,'single');
  const explicitEmpty=field(form,'optional','Optional',{choices:[['inherited','Kế thừa']],value:''});
  assert.equal(explicitEmpty.value,'');
});
