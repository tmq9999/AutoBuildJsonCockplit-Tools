// Statistics are scoped to the loaded report page, never extrapolated.
export function reportSummary(items,kind='requests'){
  const sum=read=>items.reduce((n,row)=>{const v=read(row);return n===null||v==null?null:n+BigInt(v);},0n);
  return {
    attempts:kind==='requests'?BigInt(items.length):sum(r=>r.attempt_count),
    completed:kind==='requests'?BigInt(items.filter(r=>r.status==='completed').length):sum(r=>r.completed_count),
    input:sum(r=>kind==='requests'?r.usage?.input_tokens:r.input_tokens),
    output:sum(r=>kind==='requests'?r.usage?.output_tokens:r.output_tokens),
    charged:sum(r=>r.charged_micro),
  };
}
export function requestTrend(items,start,end,count=12){
  const from=Date.parse(start),to=Date.parse(end),span=to-from;
  if(!Number.isFinite(span)||span<0)return [];
  const buckets=Array(count).fill(0);
  for(const r of items){const t=Date.parse(r.admitted_at??r.started_at);if(!Number.isFinite(t)||t<from||t>to)continue;const index=span===0?0:Math.min(count-1,Math.floor((t-from)/span*count));buckets[index]++;}
  return buckets;
}
