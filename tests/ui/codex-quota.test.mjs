import assert from 'node:assert/strict';
import test from 'node:test';
import {additionalQuotaGroups} from '../../autobuild_json/gateway/admin/static/codex-quota.js';
const entry={model_id:'gpt-reserve',limit_name:'GPT Reserve',allowed:true,primary:{used_percent:'25',window_seconds:18000,reset_at:'2026-09-26T12:00:00Z'}};
const projection=(overrides={},stale=false)=>({stale,snapshot:{allowed:false,banner_type:'luna_reserve',additional_rate_limits:[entry],...overrides}});
test('Reserve has its own quota and window, independent of exhausted normal quota',()=>{
  const [group]=additionalQuotaGroups(projection({primary:{used_percent:'100'}}));
  assert.equal(group.label,'GPT-5.6 Reserve');assert.equal(group.primary.used_percent,'25');assert.equal(group.status,'Đủ điều kiện Reserve');
});
test('missing Reserve stays unknown, not 100 or zero',()=>{
  for(const p of [null,{snapshot:{}},projection({additional_rate_limits:[]})]){
    const [group]=additionalQuotaGroups(p);assert.equal(group.primary,null);assert.match(group.status,/dữ liệu Reserve/);
  }
});
test('regular allowance and stale evidence never claim active reserve',()=>{
  assert.match(additionalQuotaGroups(projection({allowed:true}))[0].status,/Chưa kích hoạt/);
  assert.match(additionalQuotaGroups(projection({banner_type:null}))[0].status,/Chưa đủ/);
  assert.match(additionalQuotaGroups(projection({},true))[0].status,/snapshot cũ/);
  assert.doesNotMatch(additionalQuotaGroups(projection({},true))[0].status,/Đủ điều kiện/);
  assert.match(additionalQuotaGroups({...projection(),last_error:'codex_usage_unavailable'})[0].status,/Chưa xác nhận/);
});
test('zero percent remaining and additional model quota are retained',()=>{
  const groups=additionalQuotaGroups(projection({additional_rate_limits:[{...entry,primary:{used_percent:'100'}},
    {model_id:'gpt-5.3-codex-spark',limit_name:'Spark',allowed:false}]}));
  assert.match(groups[0].status,/Đã hết/);assert.equal(groups[0].primary.used_percent,'100');assert.equal(groups[1].label,'Spark');
});
