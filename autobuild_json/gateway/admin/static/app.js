import {createApi,maskedSecret,microToDecimal} from './api.js';
import {node,action,value} from './dom.js';
import {keyForm,keyPayload} from './keys.js';
import {configForm,configPayload} from './forms.js';
import {createCodexWorkspace} from './codex-view.js';
import {icon,decorate} from './codex-ui.js';
const $=id=>document.getElementById(id),api=createApi();
const codex=createCodexWorkspace($('codex-workspace'),api,()=>{api.clear();hideAll();});
// Keep the navigation focused on Codex while retaining direct access to the
// model registry and bindings used by the API service.
const adminNav=document.querySelector('.admin-nav');
for(const link of document.querySelectorAll('[data-footer-nav]'))link.addEventListener('click',e=>{e.preventDefault();document.querySelector(`[data-nav="${link.dataset.footerNav}"]`)?.click();});
if(adminNav&&!adminNav.querySelector('[data-nav="models"]')){
  const anchor=adminNav.querySelector('[data-nav="proxies"]');
  for(const [id,label] of [['models','◉ Models'],['bindings','▣ Model mapping']]){
    const button=document.createElement('button');button.dataset.nav=id;button.textContent=label;adminNav.insertBefore(button,anchor);
  }
}
const titles={keys:'API key',customers:'Khách hàng',providers:'Provider',credentials:'Credential',models:'Model',bindings:'Model mapping',proxies:'Proxy',oauth:'OAuth / Import',usage:'Usage',audit:'Audit',overview:'Tổng quan',playground:'Chạy thử API'};
Object.assign(titles,{aliases:'Alias',budgets:'Budget upstream'});
let data={},view='codex-accounts',selected=null,authenticated=false,busy=false,loadEpoch=0;
const endpoints=['customers','keys','providers','credentials','models','bindings','proxies','usage','audit','overview','aliases','budgets','key-balances'];
function notice(text,error=false){$('notice').textContent=text;$('notice').className='notice'+(error?' error':'');$('notice').hidden=!text;}
function clearSecret(){$('secret-value').value='';$('one-time-secret').close();}
function hideAll(){authenticated=false;loadEpoch++;data={};selected=null;codex.clear();view='codex-accounts';$('split-workspace').hidden=true;clearSecret();$('editor').replaceChildren();$('rows').replaceChildren();$('workspace').hidden=true;$('login-panel').hidden=false;$('admin-token').value='';}
function controls(){for(const b of document.querySelectorAll('#workspace button'))if(!b.closest('#codex-workspace'))b.disabled=busy;$('new-item').hidden=['overview','audit','usage','codex-accounts'].includes(view);$('refresh').hidden=view==='codex-accounts';}
async function guard(fn){if(busy)return;busy=true;controls();try{await fn();}catch(e){if(e.message==='STALE_SESSION'||e.name==='AbortError')return;if(e.status===401){api.clear();hideAll();}else notice(e.message,true);}finally{busy=false;controls();}}
async function load(){const token=++loadEpoch;const values=await Promise.all(endpoints.map(k=>api.request('/api/service/'+k)));if(!authenticated||token!==loadEpoch)return;data=Object.fromEntries(endpoints.map((k,i)=>[k,values[i]]));$('pending-notice').hidden=!data.overview.pending;render();}
function dataset(){if(view==='oauth')return data.credentials?.filter(r=>r.account_id)||[];if(view==='overview')return Object.entries(data.overview??{}).map(([name,count])=>({name,count}));if(view==='playground')return[];return data[view]??[];}
function rowValues(row){
  if(view==='aliases')return[row.alias,row.model_id];
  if(view==='budgets')return[row.id,row.currency,row.budget_limit??'Không giới hạn',row.spent,row.held];
  if(view==='keys'){const owner=data.customers.find(c=>c.id===row.customer_id),balance=data['key-balances']?.find(b=>b.key_id===row.key_id);const total=row.policy.total_micro===null?'Không giới hạn':microToDecimal(row.policy.total_micro);return[row.name||row.prefix,owner?.name??'—',`${microToDecimal(balance?.spent??'0')} dùng · ${microToDecimal(balance?.held??'0')} giữ / ${total}`,row.policy.protocols.join(', '),row.revoked_at?'Đã thu hồi':row.policy.enabled?'Hoạt động':'Đã khóa'];}
  if(view==='providers')return[row.config.name,row.config.adapter,row.config.root,row.config.enabled?'Hoạt động':'Đã tắt'];
  if(view==='models')return[row.model_id,row.identity,`${microToDecimal(row.input_micro)} / ${microToDecimal(row.output_micro)}`,row.enabled?'Hiển thị':'Ẩn'];
  if(view==='proxies')return[row.name,row.config.mode,row.config.region,row.version];
  if(view==='customers')return[row.name,row.enabled?'Hoạt động':'Đã khóa',row.version];
  if(view==='credentials'||view==='oauth')return[row.email||row.id.slice(0,8),row.health,row.enabled?'Bật':'Tắt',row.account_id||'API key'];
  if(view==='bindings')return[row.public_model_id,row.upstream_model,row.identity,row.priority,row.enabled?'Bật':'Tắt'];
  if(view==='usage')return[row.id.slice(0,8),row.model_id,row.state,row.usage?`${row.usage.input_tokens} / ${row.usage.output_tokens}`:'Chưa có usage',microToDecimal(row.hold)];
  if(view==='audit')return[row.action,row.actor,row.created_at];
  if(view==='overview'){const labels={requests:'Yêu cầu',completed:'Hoàn tất',failed:'Lỗi',pending:'Chờ usage',input_tokens:'Input token',output_tokens:'Output token',total_tokens:'Tổng token',cached_read:'Cache đọc (trong input)',cached_write:'Cache ghi (trong input)',reasoning:'Reasoning (trong output)',uncached_input_tokens:'Input chưa cache',unknown_usage_requests:'Hoàn tất thiếu usage',unknown_detail_requests:'Thiếu chi tiết usage',charged_micro:'Quota quy đổi đã trừ',held_micro:'Quota đang giữ'};return[labels[row.name]??row.name,row.name.endsWith('_micro')?microToDecimal(row.count):row.count];}return[];
}
const headers={keys:['Key / Tên','Khách hàng','Quota tổng (quy đổi)','Giao thức','Trạng thái'],customers:['Tên','Trạng thái','Phiên bản'],providers:['Tên','Chuẩn','Base URL','Trạng thái'],models:['Public ID','Identity','Hệ số in / out','Trạng thái'],proxies:['Tên','Loại','Vùng','Phiên bản'],credentials:['Credential','Health','Trạng thái','Loại'],oauth:['Tài khoản','Health','Trạng thái','Account ID'],bindings:['Public model','Upstream','Identity','Ưu tiên','Trạng thái'],usage:['Request','Model','Trạng thái','Token thực in / out','Giữ trước'],audit:['Hành động','Actor','Thời gian'],overview:['Chỉ số','Giá trị'],playground:['Kết quả thử']};
Object.assign(headers,{aliases:['Alias','Public model'],budgets:['Budget ID','Tiền tệ','Hạn mức','Đã dùng','Giữ trước']});
function isRowActive(v,r){
  if(v==='keys')return Boolean(r.policy?.enabled&&!r.revoked_at);
  if(v==='customers'||v==='providers'||v==='credentials'||v==='models'||v==='bindings')return Boolean(r.enabled);
  return true;
}
function render(){
  $('split-workspace').hidden=view==='codex-accounts';
  $('global-header').hidden=view==='codex-accounts';
  if(view==='codex-accounts'){controls();return;}
  $('page-title').textContent=view==='keys'?'Quản lý API key':titles[view];$('list-title').textContent=titles[view];$('new-item').textContent='＋ '+(view==='playground'?'Chạy thử':'Thêm '+titles[view]);
  for(const b of document.querySelectorAll('[data-nav]'))b.classList.toggle('active',b.dataset.nav===view);
  const head=node('tr');for(const title of headers[view])head.append(node('th',title));$('columns').replaceChildren(head);
  const statusFilter=$('status-filter')?.value||'all';
  if($('status-filter'))$('status-filter').hidden=!['keys','customers','providers','credentials','models','bindings'].includes(view);
  const term=$('search').value.toLocaleLowerCase(),rows=dataset().filter(r=>{
    if(statusFilter==='active'&&!isRowActive(view,r))return false;
    if(statusFilter==='inactive'&&isRowActive(view,r))return false;
    return rowValues(r).join(' ').toLocaleLowerCase().includes(term);
  });$('rows').replaceChildren();
  for(const row of rows){const tr=node('tr'),values=rowValues(row);values.forEach((v,i)=>{const cell=node('td');if(i===0&&!['audit','overview'].includes(view)){action(cell,String(v),()=>edit(row),'row-link');if(view==='keys')cell.append(node('small',maskedSecret(row.prefix)));}else cell.textContent=String(v??'—');tr.append(cell);});$('rows').append(tr);}
  if(!rows.length){const tr=node('tr');tr.append(node('td','Chưa có dữ liệu.',{colspan:headers[view].length,class:'empty'}));$('rows').append(tr);}
  $('row-count').textContent=`${rows.length} bản ghi`;controls();
}
function reveal(secret){$('secret-value').value=secret;$('one-time-secret').showModal();}
function savePath(row,form){const id=encodeURIComponent(row?.key_id??row?.id??row?.model_id??'');
  if(view==='aliases'||view==='budgets')return['/api/service/'+view,'POST'];
  if(view==='keys'||view==='customers')return['/api/service/'+view+(row?'/'+id:''),row?'PATCH':'POST'];
  if(view==='providers'||view==='proxies'||view==='models')return['/api/service/'+view+(row?`/${id}/${row.version}`:''),row?'PUT':'POST'];
  if(view==='credentials')return[row?`/api/service/credentials/${id}/rotate`:`/api/service/providers/${value(form,'provider_id')}/credentials`,'POST'];
  if(view==='bindings')return[row?`/api/service/bindings/${id}/${row.version}`:'/api/service/bindings',row?'PUT':'POST'];
  if(view==='oauth')return['/api/service/oauth/import','POST'];
  if(view==='usage')return[`/api/service/usage/${id}/adjust`,'POST'];
  return['/api/service/playground','POST'];
}
function edit(row=null){
  selected=row;$('editor-title').textContent=(row?'Chi tiết ':'Thêm ')+titles[view];$('editor').replaceChildren();
  if(view==='overview'||view==='audit'){return;}
  if(view==='oauth'&&row){$('editor').append(node('pre',JSON.stringify(row,null,2)));action($('editor'),'Refresh token',()=>guard(async()=>{await api.request(`/api/service/oauth/${row.id}/refresh`,{method:'POST',body:{}});await load();notice('Đã refresh token.');}));return;}
  if(view==='usage'&&row?.state!=='usage_pending'){$('editor').append(node('pre',JSON.stringify(row,null,2)));return;}
  if(view==='budgets'&&row){$('editor').append(node('pre',JSON.stringify(row,null,2)));return;}
  const form=node('form','',{id:'editor-form',autocomplete:'off'});if(view==='keys')keyForm(form,row,data);else configForm(form,view,row,data);
  const submit=node('button',view==='playground'?'Gửi request thật':row?'Lưu thay đổi':'Tạo mới',{type:'submit',class:'primary'});form.append(submit);$('editor').append(form);
  form.addEventListener('submit',event=>{event.preventDefault();guard(async()=>{const payload=view==='keys'?keyPayload(form,row):configPayload(form,view,row);const[path,method]=savePath(row,form);
    let result;
    try{
      result=await api.request(path,{method,body:payload});
    }catch(err){
      if(view==='keys'&&!row&&(err.code==='permission_denied'||err.status===403)){
        const custId=value(form,'customer_id');
        const cust=data.customers.find(c=>c.id===custId);
        if(cust&&!cust.enabled){
          throw new Error(`Không thể cấp API key: Khách hàng "${cust.name}" đang bị khóa. Cần mở lại khách hàng trước khi cấp key.`);
        }
      }
      throw err;
    }
    if(view==='playground'){const usage=result?.usage;const old=$('playground-result');if(old)old.remove();const output=node('section','',{id:'playground-result'});output.append(node('h3','Response thật từ gateway'));if(usage)output.append(node('pre',JSON.stringify(usage,null,2)));const full=node('details');full.append(node('summary','Xem response JSON'),node('pre',JSON.stringify(result,null,2)));output.append(full);$('editor').append(output);await load();notice('Request hoàn tất. Usage và tổng gateway đã cập nhật.');return;}
    $('editor').replaceChildren(node('p','Đã lưu. Chọn bản ghi để chỉnh sửa.',{class:'muted'}));selected=null;await load();notice('Đã lưu thay đổi.');if(result?.secret)reveal(result.secret);
  });});
  if(view==='keys'&&row&&!row.revoked_at){action($('editor'),'Đổi key',()=>guard(async()=>{if(!confirm('Key cũ sẽ mất hiệu lực cho request mới. Tiếp tục?'))return;const result=await api.request(`/api/service/keys/${row.key_id}/rotate`,{method:'POST',body:{version:row.version}});await load();edit(data.keys.find(k=>k.key_id===row.key_id));reveal(result.secret);}));action($('editor'),'Thu hồi key',()=>guard(async()=>{if(!confirm('Thu hồi key này? Usage/audit được giữ lại.'))return;await api.request(`/api/service/keys/${row.key_id}/revoke`,{method:'POST',body:{version:row.version}});await load();edit(data.keys.find(k=>k.key_id===row.key_id));}),'danger');}
  if(view==='keys'&&row&&!row.revoked_at)action($('editor'),'Xóa API key',()=>guard(async()=>{if(!confirm('Xóa API key? Key sẽ mất hiệu lực, usage và audit vẫn được giữ lại.'))return;await api.request(`/api/service/keys/${row.key_id}`,{method:'DELETE',body:{version:row.version}});await load();edit(data.keys.find(k=>k.key_id===row.key_id));notice('Đã xóa API key.');}),'danger');
  if(view==='customers'&&row)action($('editor'),row.enabled?'Xóa khách hàng':'Mở lại khách hàng',()=>guard(async()=>{if(row.enabled&&!confirm('Xóa khách hàng? Các key và lịch sử usage vẫn được giữ lại.'))return;await api.request(`/api/service/customers/${row.id}`,{method:row.enabled?'DELETE':'PATCH',body:row.enabled?{version:row.version}:{version:row.version,enabled:true,name:row.name}});await load();edit(data.customers.find(c=>c.id===row.id));notice(row.enabled?'Đã xóa khách hàng.':'Đã mở lại khách hàng.');}),row.enabled?'danger':'');
  if(view==='providers'&&row){action($('editor'),'Lấy danh sách model',()=>guard(async()=>{const result=await api.request(`/api/service/providers/${row.id}/discover`,{method:'POST',body:{}});$('editor').append(node('pre',JSON.stringify(result,null,2)));}));action($('editor'),'Tắt provider',()=>guard(async()=>{await api.request(`/api/service/providers/${row.id}`,{method:'DELETE',body:{version:row.version}});await load();}),'danger');}
  if(view==='credentials'&&row)action($('editor'),row.enabled?'Tắt credential':'Bật credential',()=>guard(async()=>{await api.request(`/api/service/credentials/${row.id}`,{method:'PATCH',body:{version:row.version,enabled:!row.enabled}});await load();edit(data.credentials.find(c=>c.id===row.id));}));
  if(view==='models'&&row)action($('editor'),'Ẩn / ngừng model',()=>guard(async()=>{await api.request(`/api/service/models/${encodeURIComponent(row.model_id)}`,{method:'DELETE',body:{version:row.version}});await load();edit(data.models.find(m=>m.model_id===row.model_id));}),'danger');
  if(view==='proxies'&&row){action($('editor'),'Xóa profile không còn dùng',()=>guard(async()=>{if(!confirm('Xóa cấu hình proxy này? Không gọi /out.'))return;try{await api.request(`/api/service/proxies/${row.id}`,{method:'DELETE',body:{version:row.version}});await load();$('editor').replaceChildren();}catch(err){if(err.status===409||err.code==='invalid_state'){throw new Error('Không thể xóa profile proxy: Profile này đang được gán cho Provider hoặc Tài khoản Codex. Vui lòng gỡ liên kết trước khi xóa.');}throw err;}}),'danger');if(row.config.mode==='kiotproxy')action($('editor'),'Giải phóng Kiot key…',()=>guard(async()=>{const index=prompt('Số thứ tự key (bắt đầu từ 1). Chỉ giải phóng khi không có request sử dụng.');if(index===null)return;await api.request(`/api/service/proxies/${row.id}/release`,{method:'POST',body:{key_index:Number(index)-1}});notice('Đã giải phóng key được chọn.');}));}
}
const navIcons={overview:'activity',customers:'users',keys:'key',providers:'route',credentials:'key',models:'image',bindings:'route',proxies:'route',oauth:'upload',playground:'play',usage:'activity',audit:'activity',aliases:'route',budgets:'settings','codex-accounts':'codex'};
document.querySelector('.cockpit-logo').replaceChildren(icon('codex'));
for(const button of document.querySelectorAll('[data-nav]')){
  button.textContent=button.dataset.nav==='codex-accounts'?'Codex':button.textContent.replace(/^\S+\s*/, '');decorate(button,navIcons[button.dataset.nav]);
  button.addEventListener('click',()=>{
    if(busy)return;
    const wasCodex=view==='codex-accounts',requested=button.dataset.nav;
    view=requested==='oauth'?'codex-accounts':requested;selected=null;$('search').value='';if($('status-filter'))$('status-filter').value='all';
    if(view==='codex-accounts'){
      codex.open().then(()=>{if(requested==='oauth'&&view==='codex-accounts')codex.showImport();});
      $('codex-workspace').hidden=false;render();
      for(const other of document.querySelectorAll('[data-nav]'))other.classList.toggle('active',other===button);
      return;
    }
    codex.leave();$('editor').replaceChildren(node('p','Chọn bản ghi hoặc thêm mới.',{class:'muted'}));render();
    // Codex publication has its own data cache. Build the required model
    // selector only after fetching the current registry, including first entry.
    if(wasCodex||view==='playground')guard(async()=>{
      await load();
      if(authenticated&&view==='playground')edit();
    });
  });
}
$('new-item').addEventListener('click',()=>edit());$('search').addEventListener('input',render);$('status-filter')?.addEventListener('change',render);$('refresh').addEventListener('click',()=>guard(load));
$('hide-secret').addEventListener('click',clearSecret);$('one-time-secret').addEventListener('cancel',clearSecret);$('copy-secret').addEventListener('click',()=>navigator.clipboard.writeText($('secret-value').value));
$('logout').addEventListener('click',()=>guard(async()=>{try{await api.request('/api/session',{method:'DELETE'});}finally{api.clear();hideAll();}}));
$('login-form').addEventListener('submit',event=>{event.preventDefault();guard(async()=>{api.clear();$('login-error').textContent='';try{const session=await api.request('/api/session',{method:'POST',body:{token:$('admin-token').value}});api.setSession(session.csrf_token);authenticated=true;$('admin-token').value='';$('login-panel').hidden=true;$('workspace').hidden=false;await Promise.all([load(),codex.open()]);}catch(e){$('login-error').textContent='Không đăng nhập được: '+e.message;throw e;}});});
(async()=>{try{const session=await api.request('/api/session');api.setSession(session.csrf_token);authenticated=true;$('login-panel').hidden=true;$('workspace').hidden=false;await Promise.all([load(),codex.open()]);}catch(e){if(e.message!=='STALE_SESSION')hideAll();}})();
