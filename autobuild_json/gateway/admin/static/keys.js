import {field,section,value,checked,lines,node} from './dom.js';
import {decimalToMicro,microToDecimal} from './api.js';
export function keyForm(form,row,data){
  const p=row?.policy??{},general=section(form,'Thông tin chung');
  field(general,'name','Tên hiển thị',{value:row?.name??'',required:true});
  const customer=field(general,'customer_id','Khách hàng',{choices:[['','Chọn khách hàng'],...data.customers.map(c=>[c.id,c.name])],value:row?.customer_id??'',required:true});customer.disabled=Boolean(row);
  field(general,'expires_at','Hết hạn (UTC)',{type:'datetime-local',value:p.expires_at?new Date(p.expires_at).toISOString().slice(0,16):''});
  field(general,'enabled','Cho phép sử dụng',{type:'checkbox',value:p.enabled??true});
  const quota=section(form,'Hạn mức token quy đổi');quota.append(node('p','Trống = không giới hạn; 0 = không có quota.',{class:'muted'}));
  for(const[k,label]of [['total','Tổng quota'],['day','Quota ngày (UTC)'],['month','Quota tháng (UTC)']])field(quota,k,label,{value:microToDecimal(p[k+'_micro']??(k==='total'?'0':null))});
  field(quota,'rpm','Request / phút',{type:'number',value:p.rpm??60,required:true});field(quota,'concurrency','Request đồng thời',{type:'number',value:p.concurrency??1,required:true});
  const permission=section(form,'Giao thức & model');for(const protocol of ['openai','anthropic','gemini','ollama'])field(permission,'protocol_'+protocol,protocol,{type:'checkbox',value:p.protocols?.includes(protocol)});
  field(permission,'model_ids','Model cho phép (mỗi dòng / dấu phẩy)',{type:'textarea',value:(p.model_ids??[]).join('\n'),help:'Quyền model áp dụng chung cho các giao thức đã chọn.'});
  field(permission,'all_models','Cho phép tất cả model (kể cả model thêm sau)',{type:'checkbox',value:p.all_models??false});
  permission.append(node('p','Quota = token input × hệ số input + token output × hệ số output',{class:'formula'}));
  field(permission,'overrides','Hệ số riêng theo model',{type:'textarea',value:(p.model_overrides??[]).map(r=>`${r.model_id}|${microToDecimal(r.input_micro)}|${microToDecimal(r.output_micro)}`).join('\n'),help:'Mỗi dòng: model|hệ số input|hệ số output. Trống dùng hệ số của model.'});
}
export function keyPayload(form,row){
  const overrides=value(form,'overrides').split('\n').filter(s=>s.trim()).map(line=>{const p=line.split('|');if(p.length!==3)throw new Error('Hệ số cần model|input|output');return{model_id:p[0].trim(),input_micro:decimalToMicro(p[1]),output_micro:decimalToMicro(p[2])};});
  const policy={model_ids:lines(value(form,'model_ids')),protocols:['openai','anthropic','gemini','ollama'].filter(p=>checked(form,'protocol_'+p)),all_models:checked(form,'all_models'),enabled:checked(form,'enabled'),
    model_overrides:overrides,rpm:Number(value(form,'rpm')),concurrency:Number(value(form,'concurrency')),expires_at:value(form,'expires_at')?value(form,'expires_at')+':00Z':null};
  for(const k of ['total','day','month'])policy[k+'_micro']=value(form,k).trim()?decimalToMicro(value(form,k)):null;
  return row?{name:value(form,'name'),version:row.version,policy}:{name:value(form,'name'),customer_id:value(form,'customer_id'),policy};
}
