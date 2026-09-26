// Numbered pages with bounded controls even for large account catalogs.
export const ACCOUNT_PAGE_SIZES=[24,48,96];
export function pageNumbers(page,total){
  const included=new Set([1,total]);
  for(let n=Math.max(1,page-1);n<=Math.min(total,page+1);n++)included.add(n);
  const result=[];for(const n of [...included].sort((a,b)=>a-b)){
    const previous=result.at(-1);if(typeof previous==='number'&&n-previous>1)result.push(null);
    result.push(n);
  }return result;
}
export function accountPagePath(page,size,search='',status='Tất cả'){
  const query=new URLSearchParams({page:String(page),limit:String(size)});
  if(search.trim())query.set('q',search.trim());
  if(status!=='Tất cả')query.set('status',status);
  return '/api/service/oauth-accounts?'+query;
}
