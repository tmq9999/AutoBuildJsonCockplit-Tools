// Stored observations only; a missing allowance must never render as 100%.
export function additionalQuotaGroups(projection){
  const snapshot=projection?.snapshot,entries=snapshot?.additional_rate_limits??[];
  const groups=entries.map(entry=>{
    const reserve=entry.model_id==='gpt-reserve';
    const exhausted=entry.limit_reached===true||[entry.primary,entry.secondary].some(w=>w?.used_percent!=null&&Number(w.used_percent)>=100);
    let status=entry.allowed===false||exhausted?'Đã hết / không được phép':entry.allowed===true?'Được phép':'Không rõ';
    if(reserve&&entry.allowed===true&&!exhausted){
      status=snapshot.allowed===false&&snapshot.banner_type==='luna_reserve'?'Đủ điều kiện Reserve':
        snapshot.allowed===true?'Chưa kích hoạt · quota chính còn dùng được':'Chưa đủ điều kiện kích hoạt';
    }
    if(projection?.stale||projection?.last_error||snapshot.last_error)status='Chưa xác nhận · snapshot cũ, cần refresh';
    return {label:reserve?'GPT-5.6 Reserve':entry.limit_name,model:entry.model_id,status,
      primary:entry.primary,secondary:entry.secondary};
  });
  if(!entries.some(e=>e.model_id==='gpt-reserve'))groups.push({label:'GPT-5.6 Reserve',model:'gpt-reserve',
    status:snapshot?'Upstream chưa có dữ liệu Reserve':'Chưa có dữ liệu Reserve',primary:null,secondary:null});
  return groups;
}
