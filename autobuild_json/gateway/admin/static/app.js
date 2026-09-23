import {createApi,maskedSecret,microToDecimal} from './api.js';
import {node,action,value} from './dom.js';
import {keyForm,keyPayload} from './keys.js';
import {configForm,configPayload} from './forms.js';
const $=id=>document.getElementById(id),api=createApi();
const titles={keys:'API key',customers:'Khách hàng',providers:'Provider',credentials:'Credential',models:'Model',bindings:'Model mapping',proxies:'Proxy',oauth:'OAuth / Import',usage:'Usage',audit:'Audit',overview:'Tổng quan',playground:'Chạy thử API'};
Object.assign(titles,{aliases:'Alias',budgets:'Budget upstream'});
let data={},view='keys',selected=null,authenticated=false,busy=false,loadEpoch=0;
const endpoints=['customers','keys','providers','credentials','models','bindings','proxies','usage','audit','overview','aliases','budgets','key-balances'];
function notice(text,error=false){$('notice').textContent=text;$('notice').className='notice'+(error?' error':'');$('notice').hidden=!text;}
function clearSecret(){$('secret-value').value='';$('one-time-secret').close();}
function hideAll(){authenticated=false;loadEpoch++;data={};selected=null;clearSecret();$('editor').replaceChildren();$('rows').replaceChildren();$('workspace').hidden=true;$('login-panel').hidden=false;$('admin-token').value='';}
function controls(){for(const b of document.querySelectorAll('#workspace button'))b.disabled=busy;$('new-item').hidden=['overview','audit','usage'].includes(view);}
async function guard(fn){if(busy)return;busy=true;controls();try{await fn();}catch(e){if(e.message==='STALE_SESSION'||e.name==='AbortError')return;if(e.status===401){api.clear();hideAll();}else notice(e.message,true);}finally{busy=false;controls();}}
async function load(){const token=++loadEpoch;const values=await Promise.all(endpoints.map(k=>api.request('/api/service/'+k)));if(!authenticated||token!==loadEpoch)return;data=Object.fromEntries(endpoints.map((k,i)=>[k,values[i]]));$('pending-notice').hidden=!data.overview.pending;render();}
function dataset(){if(view==='oauth')return data.credentials?.filter(r=>r.account_id)||[];if(view==='overview')return Object.entries(data.overview??{}).map(([name,count])=>({name,count}));if(view==='playground')return[];return data[view]??[];}
function rowValues(row){
  if(view==='aliases')return[row.alias,row.model_id];
  if(view==='budgets')return[row.id,row.currency,row.budget_limit??'Không giới hạn',row.spent,row.held];
  if(view==='keys'){const owner=data.customers.find(c=>c.id===row.customer_id);return[row.name||row.prefix,owner?.name??'—',row.policy.total_micro===null?'Không giới hạn':microToDecimal(row.policy.total_micro),row.policy.protocols.join(', '),row.revoked_at?'Đã thu hồi':row.policy.enabled?'Hoạt động':'Đã khóa'];}
  if(view==='providers')return[row.config.name,row.config.adapter,row.config.root,row.config.enabled?'Hoạt động':'Đã tắt'];
  if(view==='models')return[row.model_id,row.identity,`${microToDecimal(row.input_micro)} / ${microToDecimal(row.output_micro)}`,row.enabled?'Hiển thị':'Ẩn'];
  if(view==='proxies')return[row.name,row.config.mode,row.config.region,row.version];
  if(view==='customers')return[row.name,row.enabled?'Hoạt động':'Đã khóa',row.version];
  if(view==='credentials'||view==='oauth')return[row.email||row.id.slice(0,8),row.health,row.enabled?'Bật':'Tắt',row.account_id||'API key'];
  if(view==='bindings')return[row.public_model_id,row.upstream_model,row.identity,row.priority,row.enabled?'Bật':'Tắt'];
  if(view==='usage')return[row.id.slice(0,8),row.model_id,row.state,row.usage?`${row.usage.input_tokens} / ${row.usage.output_tokens}`:'Chưa có usage',microToDecimal(row.hold)];
  if(view==='audit')return[row.action,row.actor,row.created_at];
  if(view==='overview')return[row.name,row.count];return[];
}
const headers={keys:['Key / Tên','Khách hàng','Quota tổng (quy đổi)','Giao thức','Trạng thái'],customers:['Tên','Trạng thái','Phiên bản'],providers:['Tên','Chuẩn','Base URL','Trạng thái'],models:['Public ID','Identity','Hệ số in / out','Trạng thái'],proxies:['Tên','Loại','Vùng','Phiên bản'],credentials:['Credential','Health','Trạng thái','Loại'],oauth:['Tài khoản','Health','Trạng thái','Account ID'],bindings:['Public model','Upstream','Identity','Ưu tiên','Trạng thái'],usage:['Request','Model','Trạng thái','Token thực in / out','Giữ trước'],audit:['Hành động','Actor','Thời gian'],overview:['Chỉ số','Giá trị'],playground:['Kết quả thử']};
Object.assign(headers,{aliases:['Alias','Public model'],budgets:['Budget ID','Tiền tệ','Hạn mức','Đã dùng','Giữ trước']});
function render(){
  $('page-title').textContent=view==='keys'?'Quản lý API key':titles[view];$('list-title').textContent=titles[view];$('new-item').textContent='＋ '+(view==='playground'?'Chạy thử':'Thêm '+titles[view]);
  for(const b of document.querySelectorAll('[data-nav]'))b.classList.toggle('active',b.dataset.nav===view);
  const head=node('tr');for(const title of headers[view])head.append(node('th',title));$('columns').replaceChildren(head);
  const term=$('search').value.toLocaleLowerCase(),rows=dataset().filter(r=>rowValues(r).join(' ').toLocaleLowerCase().includes(term));$('rows').replaceChildren();
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
  if(view==='bindings')return['/api/service/bindings','POST'];
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
  const submit=node('button',row?'Lưu thay đổi':'Tạo mới',{type:'submit',class:'primary'});form.append(submit);$('editor').append(form);
  form.addEventListener('submit',event=>{event.preventDefault();guard(async()=>{const payload=view==='keys'?keyPayload(form,row):configPayload(form,view,row);const[path,method]=savePath(row,form);const result=await api.request(path,{method,body:payload});
    if(view==='playground'){form.reset();$('editor').append(node('pre',JSON.stringify(result,null,2)));return;}
    $('editor').replaceChildren(node('p','Đã lưu. Chọn bản ghi để chỉnh sửa.',{class:'muted'}));selected=null;await load();notice('Đã lưu thay đổi.');if(result?.secret)reveal(result.secret);
  });});
  if(view==='keys'&&row&&!row.revoked_at){action($('editor'),'Đổi key',()=>guard(async()=>{if(!confirm('Key cũ sẽ mất hiệu lực cho request mới. Tiếp tục?'))return;const result=await api.request(`/api/service/keys/${row.key_id}/rotate`,{method:'POST',body:{version:row.version}});await load();edit(data.keys.find(k=>k.key_id===row.key_id));reveal(result.secret);}));action($('editor'),'Thu hồi key',()=>guard(async()=>{if(!confirm('Thu hồi key này? Usage/audit được giữ lại.'))return;await api.request(`/api/service/keys/${row.key_id}/revoke`,{method:'POST',body:{version:row.version}});await load();edit(data.keys.find(k=>k.key_id===row.key_id));}),'danger');}
  if(view==='providers'&&row){action($('editor'),'Lấy danh sách model',()=>guard(async()=>{const result=await api.request(`/api/service/providers/${row.id}/discover`,{method:'POST',body:{}});$('editor').append(node('pre',JSON.stringify(result,null,2)));}));action($('editor'),'Tắt provider',()=>guard(async()=>{await api.request(`/api/service/providers/${row.id}`,{method:'DELETE',body:{version:row.version}});await load();}),'danger');}
  if(view==='credentials'&&row)action($('editor'),row.enabled?'Tắt credential':'Bật credential',()=>guard(async()=>{await api.request(`/api/service/credentials/${row.id}`,{method:'PATCH',body:{version:row.version,enabled:!row.enabled}});await load();edit(data.credentials.find(c=>c.id===row.id));}));
  if(view==='proxies'&&row){action($('editor'),'Xóa profile không còn dùng',()=>guard(async()=>{if(!confirm('Xóa cấu hình proxy này? Không gọi /out.'))return;await api.request(`/api/service/proxies/${row.id}`,{method:'DELETE',body:{version:row.version}});await load();$('editor').replaceChildren();}),'danger');if(row.config.mode==='kiotproxy')action($('editor'),'Giải phóng Kiot key…',()=>guard(async()=>{const index=prompt('Số thứ tự key (bắt đầu từ 1). Chỉ giải phóng khi không có request sử dụng.');if(index===null)return;await api.request(`/api/service/proxies/${row.id}/release`,{method:'POST',body:{key_index:Number(index)-1}});notice('Đã giải phóng key được chọn.');}));}
}
for(const button of document.querySelectorAll('[data-nav]'))button.addEventListener('click',()=>{if(busy)return;view=button.dataset.nav;selected=null;$('search').value='';$('editor').replaceChildren(node('p','Chọn bản ghi hoặc thêm mới.',{class:'muted'}));render();if(view==='playground')edit();});
$('new-item').addEventListener('click',()=>edit());$('search').addEventListener('input',render);$('refresh').addEventListener('click',()=>guard(load));
$('hide-secret').addEventListener('click',clearSecret);$('one-time-secret').addEventListener('cancel',clearSecret);$('copy-secret').addEventListener('click',()=>navigator.clipboard.writeText($('secret-value').value));
$('logout').addEventListener('click',()=>guard(async()=>{try{await api.request('/api/session',{method:'DELETE'});}finally{api.clear();hideAll();}}));
$('login-form').addEventListener('submit',event=>{event.preventDefault();guard(async()=>{api.clear();$('login-error').textContent='';try{const session=await api.request('/api/session',{method:'POST',body:{token:$('admin-token').value}});api.setSession(session.csrf_token);authenticated=true;$('admin-token').value='';$('login-panel').hidden=true;$('workspace').hidden=false;await load();}catch(e){$('login-error').textContent='Không đăng nhập được: '+e.message;throw e;}});});
(async()=>{try{const session=await api.request('/api/session');api.setSession(session.csrf_token);authenticated=true;$('login-panel').hidden=true;$('workspace').hidden=false;await load();}catch(e){if(e.message!=='STALE_SESSION')hideAll();}})();
