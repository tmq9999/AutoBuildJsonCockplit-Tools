import {field,section,value,checked,lines,node} from './dom.js';
import {decimalToMicro,microToDecimal,parseQuotaInput} from './api.js';
import {parseOverrides} from './codex-state.js';

const PROTOCOLS=['openai','anthropic','gemini','ollama'];

function formatQuotaPreview(value){
  const [whole,fraction='']=value.split('.');
  const grouped=whole.replace(/\B(?=(\d{3})+(?!\d))/g,'.');
  return grouped+(fraction?','+fraction:'');
}
function quotaField(parent,name,label,options={}){
  const input=field(parent,name,label,options);
  if(name!=='total' && name!=='day' && name!=='month') return input;
  input.placeholder='1m · 100m · 1b';
  const preview=node('small','',{class:'quota-preview','aria-live':'polite'});
  input.parentElement.append(preview);
  const update=()=>{
    const raw=input.value.trim();
    if(!raw){preview.textContent='Không giới hạn';preview.className='quota-preview';return;}
    try{preview.textContent=`= ${formatQuotaPreview(parseQuotaInput(raw))} token`;preview.className='quota-preview';}
    catch(error){preview.textContent=error.message;preview.className='quota-preview error';}
  };
  input.addEventListener('input',update);update();
  return input;
}
export function keyForm(form,row,data){
  const p=row?.policy??{},general=section(form,'Thông tin chung');
  field(general,'name','Tên hiển thị',{value:row?.name??'',required:true});
  const customer=field(general,'customer_id','Khách hàng',{choices:[['','Chọn khách hàng'],...data.customers.map(c=>[c.id,c.name])],value:row?.customer_id??'',required:true});customer.disabled=Boolean(row);
  field(general,'expires_at','Hết hạn (UTC)',{type:'datetime-local',value:p.expires_at?new Date(p.expires_at).toISOString().slice(0,16):''});
  field(general,'enabled','Cho phép sử dụng',{type:'checkbox',value:p.enabled??true});
  const quota=section(form,'Hạn mức token quy đổi');quota.append(node('p','Trống = không giới hạn; 0 = không có quota.',{class:'muted'}));
  for(const[k,label]of [['total','Tổng quota'],['day','Quota ngày (UTC)'],['month','Quota tháng (UTC)']])quotaField(quota,k,label,{value:row?microToDecimal(p[k+'_micro']):''});
  field(quota,'rpm','Request / phút',{type:'number',value:p.rpm??60,required:true});field(quota,'concurrency','Request đồng thời',{type:'number',value:p.concurrency??1,required:true});
  const permission=section(form,'Giao thức & model');for(const protocol of PROTOCOLS)field(permission,'protocol_'+protocol,protocol,{type:'checkbox',value:p.protocols?.includes(protocol)??!row});
  field(permission,'model_ids','Model cho phép (mỗi dòng / dấu phẩy)',{type:'textarea',value:(p.model_ids??[]).join('\n'),help:'Quyền model áp dụng chung cho các giao thức đã chọn.'});
  field(permission,'excluded_model_ids','Model loại trừ',{type:'textarea',value:(p.excluded_model_ids??[]).join('\n')});
  field(permission,'model_prefix','Prefix hiển thị (không wildcard)',{value:p.model_prefix??'',help:'Client dùng prefix/model; quota và quyền dùng canonical model.'});
  field(permission,'all_models','Cho phép tất cả model (kể cả model thêm sau)',{type:'checkbox',value:p.all_models??!row});
  permission.append(node('p','Weighted tokens = uncached input × input + cache read × cache read + cache write × cache write + output × output. Cache là tập con của input, không cộng hai lần.',{class:'formula'}));
  field(permission,'overrides','Hệ số riêng theo model',{type:'textarea',value:(p.model_overrides??[]).map(r=>`${r.model_id}|${microToDecimal(r.input_micro)}|${microToDecimal(r.output_micro)}|${microToDecimal(r.cache_read_micro)}|${microToDecimal(r.cache_write_micro)}`).join('\n'),help:'Mỗi dòng: model|input|output|cache read|cache write. Cache trống kế thừa input hiệu lực. 0 = miễn phí có chủ ý.'});
}
export function keyPayload(form,row){
  const overrides=parseOverrides(value(form,'overrides'));
  const protocolFields=PROTOCOLS.map(protocol=>form.elements.namedItem('protocol_'+protocol)).filter(Boolean);
  const protocols=protocolFields.length?PROTOCOLS.filter(protocol=>checked(form,'protocol_'+protocol)):PROTOCOLS;
  const allModels=form.elements.namedItem('all_models')?checked(form,'all_models'):!row;
  const policy={model_ids:lines(value(form,'model_ids')),protocols,all_models:allModels,enabled:checked(form,'enabled'),
    excluded_model_ids:lines(value(form,'excluded_model_ids')),model_prefix:value(form,'model_prefix'),model_overrides:overrides,rpm:Number(value(form,'rpm')),concurrency:Number(value(form,'concurrency')),expires_at:value(form,'expires_at')?value(form,'expires_at')+':00Z':null};
  for(const k of ['total','day','month'])policy[k+'_micro']=value(form,k).trim()?decimalToMicro(parseQuotaInput(value(form,k))):null;
  return row?{name:value(form,'name'),version:row.version,policy}:{name:value(form,'name'),customer_id:value(form,'customer_id'),policy};
}
