export function decimalToMicro(value) {
  const text=String(value).trim();
  if(!/^\d+(\.\d{1,6})?$/.test(text)) throw new Error('Số không âm, tối đa 6 chữ số thập phân.');
  const [whole,fraction='']=text.split('.');
  const result=BigInt(whole)*1000000n+BigInt(fraction.padEnd(6,'0'));
  if(result>=10n**38n) throw new Error('Giá trị quá lớn.');
  return result.toString();
}
export function parseQuotaInput(value) {
  const text=String(value).trim().replace(/\s+/g,' ');
  const match=/^(\d+(?:\.\d{1,6})?)\s*([kmb])?$/i.exec(text);
  if(!match) throw new Error('Hạn mức phải là số hoặc dùng hậu tố k, m, b.');
  const [,number,unit]=match;
  const factor={k:1000n,m:1000000n,b:1000000000n}[unit?.toLowerCase()??'']??1n;
  const [whole,fraction='']=number.split('.');
  const scaled=BigInt(whole)*1000000n+BigInt(fraction.padEnd(6,'0'));
  if(scaled>=10n**38n/factor) throw new Error('Giá trị quá lớn.');
  const result=scaled*factor;
  const integer=result/1000000n;
  const remainder=result%1000000n;
  const decimals=remainder.toString().padStart(6,'0').replace(/0+$/,'');
  return integer.toString()+(decimals?'.'+decimals:'');
}
export function microToDecimal(value) {
  if(value===null||value===undefined) return '';
  const n=BigInt(value), fraction=(n%1000000n).toString().padStart(6,'0').replace(/0+$/,'');
  return (n/1000000n).toString()+(fraction?'.'+fraction:'');
}
export function maskedSecret(label) {return `${label} ••••••••`;}
export function createApi(fetcher=globalThis.fetch.bind(globalThis)) {
  let csrf='',epoch=0;
  const active=new Set();
  return {
    setSession(value){csrf=value;epoch++;},
    clear(){csrf='';epoch++;for(const controller of active)controller.abort();active.clear();},
    async request(path,{method='GET',body}={}) {
      const generation=epoch,controller=new AbortController();active.add(controller);
      try {
        const response=await fetcher(path,{method,credentials:'same-origin',signal:controller.signal,
          headers:{'Content-Type':'application/json',...(csrf?{'X-CSRF-Token':csrf}:{})},
          ...(body===undefined?{}:{body:JSON.stringify(body)})});
        if(generation!==epoch)throw new Error('STALE_SESSION');
        const data=response.status===204?null:await response.json();
        if(generation!==epoch)throw new Error('STALE_SESSION');
        if(!response.ok){const error=new Error(typeof data?.error==='string'?data.error:data?.error?.code||'REQUEST_FAILED');error.status=response.status;throw error;}
        return data;
      } finally {active.delete(controller);}
    }
  };
}
