import {node,action} from './dom.js';

// Small local icon set; independent of the desktop reference and its dependencies.
const paths={
  codex:['m8 4-6 8 6 8m8-16 6 8-6 8','m14 3-4 18'],
  key:['M14 3a6 6 0 0 0-5 9l-6 6v3h4v-3h3l3-3a6 6 0 1 0 1-12Z','M16 7h.01'],
  users:['M16 21v-3a4 4 0 0 0-4-4H6a4 4 0 0 0-4 4v3','M9 10a4 4 0 1 0 0-8 4 4 0 0 0 0 8','M17 3a4 4 0 0 1 0 8M22 21v-3a4 4 0 0 0-3-4'],
  image:['M3 3h18v18H3Z','m3 17 6-6 4 4 4-6 4 5','M8 7h.01'],
  activity:['M2 12h5l3-9 4 18 3-9h5'],
  refresh:['M20 8a8 8 0 0 0-14-3L3 8M3 3v5h5','M4 16a8 8 0 0 0 14 3l3-3M21 21v-5h-5'],
  plus:['M12 5v14M5 12h14'],close:['m6 6 12 12M18 6 6 18'],
  settings:['M4 6h16M4 12h16M4 18h16M8 3v6m8 0v6m-8 0v6'],
  route:['M5 3v12a4 4 0 0 0 4 4h10m-4-4 4 4-4 4M5 3h14v8h-7'],
  copy:['M9 9h12v12H9ZM15 5V3H3v12h2'],check:['m5 12 4 4L19 6'],
  chevron:['m6 9 6 6 6-6'],upload:['M12 16V3m-5 5 5-5 5 5M3 15v6h18v-6'],
  play:['m6 3 15 9-15 9Z'],power:['M12 2v10M6 5a9 9 0 1 0 12 0'],
  trash:['M3 6h18M8 6V3h8v3M5 6l1 15h12l1-15M10 10v7m4-7v7'],
  edit:['m14 4 6 6M3 21l5-1L21 7l-5-5L3 15Z'],
  grid:['M3 3h7v7H3ZM14 3h7v7h-7ZM3 14h7v7H3ZM14 14h7v7h-7Z']
};
export function icon(name){
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  for(const [k,v] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.6','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true',class:'icon'}))svg.setAttribute(k,v);
  for(const d of paths[name]??paths.codex){const path=document.createElementNS(svg.namespaceURI,'path');path.setAttribute('d',d);svg.append(path);}return svg;
}
export function decorate(button,name,iconOnly=false){
  const label=button.textContent;button.prepend(icon(name));
  if(iconOnly){button.setAttribute('aria-label',label);button.title=label;for(const child of [...button.childNodes])if(child.nodeType===3)child.remove();button.classList.add('icon-button');}return button;
}
export function panelHead(parent,title,id){const head=node('div','',{class:'panel-head'});head.append(node('h3',title,{id}));const actions=node('div','',{class:'actions'});head.append(actions);parent.append(head);return actions;}
export function modal(parent,title,id){
  const dialog=node('dialog','',{id,'aria-labelledby':id+'-heading'}),body=node('div','',{class:'dialog-body'});
  const head=panelHead(dialog,title,id+'-heading');decorate(action(head,'Đóng',()=>dialog.close()),'close',true);dialog.append(body);parent.append(dialog);
  dialog.addEventListener('click',e=>{if(e.target===dialog){const r=dialog.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.close();}});
  return {dialog,body};
}
