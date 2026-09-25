import {decimalToMicro,microToDecimal} from './api.js';

const locked=new Set(['prepared','dispatched','unknown','succeeded_refresh_failed']);
export const resetLocked=operation=>locked.has(operation?.state);
export function canConsumeReset({available_count,fresh,operation_state}={}) {
  return fresh===true && Number.isInteger(available_count) && available_count>0 && !locked.has(operation_state);
}
export function quotaLabel(window){return window?.used_percent==null?'Không rõ':`${window.used_percent}% đã dùng`;}
export function windowDuration(seconds){
  if(!Number.isInteger(seconds)||seconds<=0)return 'Không rõ';
  for(const [size,label] of [[86400,'ngày'],[3600,'giờ'],[60,'phút']])if(seconds%size===0)return `${seconds/size} ${label}`;
  return `${seconds} giây`;
}
export function remainingPercent(value){
  if(value==null||!/^\d+(\.\d+)?$/.test(value))return null;
  const [whole,fraction='']=value.split('.'),scale=10n**BigInt(fraction.length),remaining=100n*scale-BigInt(whole+fraction);
  if(remaining<0n)return null;
  const decimal=(remaining%scale).toString().padStart(fraction.length,'0').replace(/0+$/,'');
  return (remaining/scale).toString()+(decimal?'.'+decimal:'');
}
export function mergePoolMembers(existing,edited,renderedIds){
  return existing.filter(member=>!renderedIds.has(member.credential_id)).concat(edited);
}
export function poolSingleSelection(mode,selectedIndex,accounts,existingId){
  if(mode!=='single')return {valid:true,credential_id:null};
  if(selectedIndex!==''){
    const account=accounts[Number(selectedIndex)];
    return account?{valid:true,credential_id:account.id}:{valid:false,credential_id:null};
  }
  const existingLoaded=accounts.some(account=>account.id===existingId);
  return existingId&&!existingLoaded?{valid:true,credential_id:existingId}:{valid:false,credential_id:null};
}
export const grantFailureDisposition=error=>Number.isInteger(error?.status)&&error.status>=400&&error.status<500?'reload':'retain';
export async function recoverGrantFailure(error,reload){
  if(grantFailureDisposition(error)==='retain')return true;
  await reload();
  return false;
}
export const rateValue=value=>String(value).trim()===''?null:decimalToMicro(value);
export const quotaAmountMicro=value=>decimalToMicro(value);
export const formatMicro=value=>microToDecimal(value);
export function parseOverrides(text){return text.split('\n').filter(s=>s.trim()).map(line=>{
  const p=line.split('|');if(![3,5].includes(p.length))throw new Error('Hệ số cần model|input|output|cache read|cache write');
  return {model_id:p[0].trim(),input_micro:decimalToMicro(p[1]),output_micro:decimalToMicro(p[2]),cache_read_micro:rateValue(p[3]??''),cache_write_micro:rateValue(p[4]??'')};
});}
export function weightedMicro(usage,rates){
  if(usage?.input_tokens==null||usage?.output_tokens==null)return null;
  const input=BigInt(usage.input_tokens),read=BigInt(usage.cached_read??'0'),write=BigInt(usage.cached_write??'0');
  if(read+write>input)throw new Error('Cache vượt input');
  const rate=BigInt(rates.input_micro);
  return ((input-read-write)*rate+read*BigInt(rates.cache_read_micro??rate)+write*BigInt(rates.cache_write_micro??rate)+BigInt(usage.output_tokens)*BigInt(rates.output_micro)).toString();
}
export function createCodexState(){
  let epoch=0,records=new Map();const state={accountId:null,
    select(id){epoch++;this.accountId=id;if(id&&!records.has(id))records.set(id,{usage:null,credits:null,operation:null,confirmed:null,busy:false});},
    clear(){epoch++;records=new Map();this.accountId=null;},
    ticket(){return {epoch,id:this.accountId,record:records.get(this.accountId)};},
    release(ticket){if(ticket.record&&records.get(ticket.id)===ticket.record){ticket.record.busy=false;return ticket.id===this.accountId;}return false;},
    current(ticket){return ticket.epoch===epoch&&ticket.id===this.accountId;},
    accept(ticket,values){if(!this.current(ticket))return false;
      if('usage'in values)this.usage=values.usage;
      if('credits'in values){this.credits=values.credits;const op=values.credits?.active_reset;
        // Null storage projection does not prove an ambiguous POST never arrived.
        if(op)this.operation=op;
      }
      if(values.operation)this.operation=values.operation;return true;
    }
  };
  for(const key of ['usage','credits','operation','confirmed','busy'])Object.defineProperty(state,key,{get(){return records.get(state.accountId)?.[key]??null;},set(value){const row=records.get(state.accountId);if(row)row[key]=value;}});
  return state;
}
