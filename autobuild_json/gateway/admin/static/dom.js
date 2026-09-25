export function node(tag,text='',attrs={}){const el=document.createElement(tag);el.textContent=text;for(const[k,v]of Object.entries(attrs))if(v!==undefined&&v!==null)el.setAttribute(k,String(v));return el;}
export function field(form,name,label,options={}){
  const {type='text',value='',choices=null,required=false,help=''}=options;
  const selected=Object.prototype.hasOwnProperty.call(options,'value')?value??'':choices?.[0]?.[0]??'';
  const wrapper=node('label',label),input=node(choices?'select':type==='textarea'?'textarea':'input','',{name,id:'field-'+name,autocomplete:'off',spellcheck:'false'});
  if(choices)for(const[key,title]of choices)input.append(node('option',title,{value:key}));
  else if(type!=='textarea')input.type=type;
  if(type==='checkbox'){wrapper.className='check';input.checked=Boolean(value);wrapper.prepend(input);}else{input.value=selected;wrapper.append(input);}
  input.required=required;if(help)wrapper.append(node('small',help));form.append(wrapper);return input;
}
export function action(parent,label,handler,kind=''){const b=node('button',label,{type:'button',class:kind});b.addEventListener('click',handler);parent.append(b);return b;}
export function section(form,title){const f=node('fieldset');f.append(node('legend',title));form.append(f);return f;}
export const value=(form,name)=>form.elements.namedItem(name)?.value??'';
export const checked=(form,name)=>Boolean(form.elements.namedItem(name)?.checked);
export const lines=text=>text.split(/[\n,]/).map(s=>s.trim()).filter(Boolean);
