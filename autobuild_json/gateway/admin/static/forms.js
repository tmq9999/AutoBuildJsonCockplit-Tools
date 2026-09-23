import {field,value,checked,lines,node} from './dom.js';
import {decimalToMicro,microToDecimal} from './api.js';
export function configForm(form,view,row,data){
  const r=row?.config??row??{},profiles=[['','Direct / mặc định'],...data.proxies.map(p=>[p.id,p.name])];
  if(view==='customers'){field(form,'name','Tên khách hàng',{value:r.name??'',required:true});field(form,'enabled','Hoạt động',{type:'checkbox',value:r.enabled??true});}
  if(view==='aliases'){field(form,'alias','Alias',{value:r.alias??'',required:true});field(form,'model_id','Public model',{choices:data.models.map(m=>[m.model_id,m.model_id]),value:r.model_id??'',required:true});}
  if(view==='budgets'){field(form,'currency','Đơn vị tiền tệ',{value:r.currency??'USD',required:true});field(form,'limit','Giới hạn chi phí upstream (trống = không giới hạn)',{value:r.budget_limit??''});form.append(node('p','Tạo budget riêng rồi gán ID cho provider. Không gộp quota của khách hoặc các đồng tiền khác nhau.',{class:'muted'}));}
  if(view==='providers'){
    field(form,'name','Tên provider',{value:r.name??'',required:true});field(form,'adapter','Chuẩn upstream',{choices:['openai_compatible','anthropic','gemini','ollama','codex_oauth'].map(x=>[x,x]),value:r.adapter??'openai_compatible'});
    field(form,'rpm_limit','RPM tối đa của provider',{type:'number',value:r.rpm_limit??600});field(form,'concurrency_limit','Request đồng thời của provider',{type:'number',value:r.concurrency_limit??16});
    field(form,'root','Base URL (gồm /v1 hoặc /api)',{value:r.root??'',required:true});field(form,'wire_api','OpenAI wire API',{choices:[['chat','Chat Completions'],['responses','Responses']],value:r.wire_api??'chat'});
    field(form,'auth_mode','Xác thực upstream',{choices:['bearer','x-api-key','x-goog-api-key','none','oauth'].map(x=>[x,x]),value:r.auth_mode??'bearer'});
    field(form,'proxy_profile_id','Proxy profile',{choices:profiles,value:r.proxy_profile_id??''});field(form,'timeout','Timeout (1–600 giây)',{type:'number',value:r.timeout??180});field(form,'budget_id','Budget upstream ID (tùy chọn)',{value:r.budget_id??''});
    field(form,'cost_schedule','Giá mua vào (JSON, tùy chọn)',{type:'textarea',value:r.cost_schedule?JSON.stringify(r.cost_schedule,null,2):'',help:'currency, input_per_million, output_per_million; giá dùng chuỗi thập phân.'});field(form,'enabled','Hoạt động',{type:'checkbox',value:r.enabled??true});
  }
  if(view==='credentials'){field(form,'provider_id','Provider',{choices:[['','Chọn provider'],...data.providers.map(p=>[p.id,p.config.name])],value:r.provider_id??'',required:true});field(form,'secret','API key upstream mới',{type:'password',required:true,help:'Không hiển thị lại key đã lưu.'});}
  if(view==='models'){field(form,'model_id','Public model ID',{value:r.model_id??'',required:true}).disabled=Boolean(row);field(form,'identity','Model identity thực',{value:r.identity??'',required:true});field(form,'input','Hệ số input',{value:microToDecimal(r.input_micro??'1000000')});field(form,'output','Hệ số output',{value:microToDecimal(r.output_micro??'1000000')});field(form,'enabled','Hiển thị model',{type:'checkbox',value:r.enabled??false});field(form,'router_model','Alias chủ động cho nhiều model khác nhau',{type:'checkbox',value:r.router_model??false});}
  if(view==='bindings'){
    for(const[n,label,choices]of [['provider_id','Provider',data.providers.map(p=>[p.id,p.config.name])],['credential_id','Credential',data.credentials.map(c=>[c.id,c.id.slice(0,8)+' · '+c.health])],['public_model_id','Public model',data.models.map(m=>[m.model_id,m.model_id])]])field(form,n,label,{choices:[['','Chọn…'],...choices],value:r[n]??'',required:true});
    field(form,'upstream_model','Upstream model ID',{value:r.upstream_model??'',required:true});field(form,'identity','Model identity thực',{value:r.identity??'',required:true});field(form,'input_bound','Input bound đã xác minh',{type:'number',value:r.input_bound??4096});field(form,'output_bound','Output bound đã xác minh',{type:'number',value:r.output_bound??1024});field(form,'capabilities','Capabilities (dấu phẩy)',{value:(r.capabilities??['text','tools']).join(',')});field(form,'priority','Ưu tiên (nhỏ trước)',{type:'number',value:r.priority??0});field(form,'enabled','Hoạt động',{type:'checkbox',value:r.enabled??true});
  }
  if(view==='proxies'){field(form,'name','Tên profile',{value:row?.name??'',required:true});field(form,'mode','Kiểu proxy',{choices:[['direct','Direct — không proxy'],['fixed','Một proxy cố định'],['pool','Danh sách proxy'],['kiotproxy','KiotProxy API key']],value:r.mode??'direct'});field(form,'entries_text','Proxy / Kiot key (mỗi dòng một mục)',{type:'textarea',help:row?'Nhập lại danh sách để thay thế. Không hiển thị secret đã lưu.':'HTTP/HTTPS/SOCKS5 hoặc mỗi dòng một Kiot key.'});field(form,'region','Vùng Kiot',{choices:[['random','Ngẫu nhiên'],['bac','Bắc'],['trung','Trung'],['nam','Nam']],value:r.region??'random'});field(form,'protocol','Giao thức Kiot',{choices:[['http','HTTP'],['socks5','SOCKS5']],value:r.protocol??'http'});field(form,'rotate','Đổi giữa các lượt khi cooldown cho phép',{type:'checkbox',value:r.rotate??false});}
  if(view==='oauth'){field(form,'record','JSON OAuth (một bản ghi)',{type:'textarea',required:true,help:'Không dán account/password. Import không tự xác nhận quyền sử dụng upstream.'});field(form,'proxy_profile_id','Proxy profile',{choices:profiles});}
  if(view==='usage'){form.append(node('p','Chỉ quyết toán khi có bằng chứng usage. Không sửa token thực gốc.'));field(form,'amount','Token quy đổi quyết toán',{value:'0'});field(form,'reason','Lý do / nguồn đối soát',{type:'textarea',required:true});}
  if(view==='playground'){field(form,'client_key','API key khách để thử',{type:'password',required:true});field(form,'protocol','Giao thức',{choices:['openai','anthropic','gemini','ollama'].map(x=>[x,x]),value:'openai'});field(form,'body','Request JSON',{type:'textarea',value:'{"model":"test-model","messages":[{"role":"user","content":"Hello"}],"max_tokens":32}',required:true});form.append(node('p','Request sẽ trừ quota key đã chọn. Chỉ JSON, không stream trong playground này.',{class:'muted'}));}
}
export function configPayload(form,view,row){
  const v=n=>value(form,n),r=row?.config??row??{};
  if(view==='aliases')return{alias:v('alias'),model_id:v('model_id')};
  if(view==='budgets')return{currency:v('currency'),limit:v('limit').trim()||null};
  if(view==='customers')return row?{name:v('name'),enabled:checked(form,'enabled'),version:row.version}:{name:v('name')};
  if(view==='providers')return{name:v('name'),adapter:v('adapter'),root:v('root'),wire_api:v('wire_api'),auth_mode:v('auth_mode'),proxy_profile_id:v('proxy_profile_id')||null,budget_id:v('budget_id')||null,cost_schedule:v('cost_schedule').trim()?JSON.parse(v('cost_schedule')):null,timeout:Number(v('timeout')),rpm_limit:Number(v('rpm_limit')),concurrency_limit:Number(v('concurrency_limit')),enabled:checked(form,'enabled')};
  if(view==='credentials')return{secret:v('secret'),...(row?{version:row.version}:{})};
  if(view==='models')return{model_id:r.model_id??v('model_id'),identity:v('identity'),input_micro:decimalToMicro(v('input')),output_micro:decimalToMicro(v('output')),enabled:checked(form,'enabled'),router_model:checked(form,'router_model')};
  if(view==='bindings')return{id:r.id??crypto.randomUUID(),provider_id:v('provider_id'),credential_id:v('credential_id'),public_model_id:v('public_model_id'),upstream_model:v('upstream_model'),identity:v('identity'),input_bound:Number(v('input_bound')),output_bound:Number(v('output_bound')),capabilities:lines(v('capabilities')),priority:Number(v('priority')),enabled:checked(form,'enabled')};
  if(view==='proxies')return{name:v('name'),mode:v('mode'),entries_text:v('entries_text'),region:v('region'),protocol:v('protocol'),rotate:checked(form,'rotate')};
  if(view==='oauth')return{record:JSON.parse(v('record')),proxy_profile_id:v('proxy_profile_id')||null};
  if(view==='usage')return{amount_micro:decimalToMicro(v('amount')),reason:v('reason')};
  if(view==='playground')return{client_key:v('client_key'),protocol:v('protocol'),body:JSON.parse(v('body'))};
  throw new Error('Không có thao tác lưu.');
}
