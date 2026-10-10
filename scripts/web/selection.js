'use strict';
// Select gallery cards without opening their preview; keep checkbox selection independent.
let rangeState=null, rangeFrame=0, suppressGalleryClickUntil=0;
const marquee=document.createElement('div');
marquee.className='selection-marquee hidden';marquee.setAttribute('aria-hidden','true');document.body.appendChild(marquee);

function selectionContext(){
 if(currentPage==='results')return {page:'results',gallery:$('photo-results'),rows:filtered,chosen:selected,canSelect:photoSelectable,
  cardAttr:'data-card',checkAttr:'data-check',pageCheck:$('select-page'),render:renderPhotos,update:updateSelection,
  help:$('page-results').querySelector('.selection-help')};
 if(currentPage==='pairs')return {page:'pairs',gallery:$('pair-results'),rows:pairFiltered,chosen:pairSelected,canSelect:pairSelectable,
  cardAttr:'data-pair-card',checkAttr:'data-pair-check',pageCheck:$('pair-select-page'),render:renderPairResults,update:updatePairSelection,
  help:$('pair-selection-help')};
 return null;
}

function paintRange(){
 const state=rangeState;if(!state?.active)return;const context=state.context;
 const sx=state.x-window.scrollX,sy=state.y-window.scrollY;
 const left=Math.min(sx,state.clientX),top=Math.min(sy,state.clientY),right=Math.max(sx,state.clientX),bottom=Math.max(sy,state.clientY);
 Object.assign(marquee.style,{left:left+'px',top:top+'px',width:(right-left)+'px',height:(bottom-top)+'px'});
 const chosen=new Set(state.additive?state.before:[]);
 for(const entry of state.entries){
  const rect=entry.element.getBoundingClientRect();
  if(context.canSelect(entry.row)&&rect.right>left&&rect.left<right&&rect.bottom>top&&rect.top<bottom)chosen.add(entry.row['__abs']);
 }
 context.chosen.clear();chosen.forEach(path=>context.chosen.add(path));
 for(const entry of state.entries){const checked=context.chosen.has(entry.row['__abs']);entry.element.classList.toggle('selected',checked);const input=entry.element.querySelector(`[${context.checkAttr}]`);if(input)input.checked=checked;}
 const eligible=state.entries.filter(entry=>context.canSelect(entry.row));
 context.pageCheck.checked=!!eligible.length&&eligible.every(entry=>context.chosen.has(entry.row['__abs']));
 context.pageCheck.indeterminate=eligible.some(entry=>context.chosen.has(entry.row['__abs']))&&!context.pageCheck.checked;
 context.update();
}
function scrollRange(){
 if(!rangeState?.active)return;
 const y=rangeState.clientY,before=window.scrollY;
 if(y<70)window.scrollBy(0,-Math.min(22,Math.max(3,(70-y)/3)));
 else if(y>window.innerHeight-70)window.scrollBy(0,Math.min(22,Math.max(3,(y-window.innerHeight+70)/3)));
 if(window.scrollY!==before)paintRange();
 rangeFrame=requestAnimationFrame(scrollRange);
}
function finishRange(cancelled=false){
 if(!rangeState)return;const state=rangeState,context=state.context;rangeState=null;cancelAnimationFrame(rangeFrame);
 if(state.active){
  if(cancelled){context.chosen.clear();state.before.forEach(path=>context.chosen.add(path));}
  suppressGalleryClickUntil=performance.now()+300;marquee.classList.add('hidden');document.body.classList.remove('range-selecting');
  if(context.gallery.hasPointerCapture(state.pointerId))context.gallery.releasePointerCapture(state.pointerId);
  context.render();
 }
}
document.addEventListener('pointerdown',event=>{
 const context=selectionContext();
 if(!context||event.button!==0||!['mouse','pen'].includes(event.pointerType)||document.querySelector('.overlay:not(.hidden)'))return;
 if(event.target.closest('input,select,textarea,label,a,[contenteditable="true"]'))return;
 const button=event.target.closest('button');if(button&&!button.hasAttribute('data-preview')&&!button.hasAttribute('data-pair-preview'))return;
 // Include the surrounding whitespace where a file-manager-style drag usually starts.
 const main=context.gallery.closest('.main'),bounds=main.getBoundingClientRect();
 const help=context.help.getBoundingClientRect();
 if(!context.help.getClientRects().length)return;
 if(!main.contains(event.target)||event.clientX<bounds.left||event.clientX>bounds.right||event.clientY<help.bottom)return;
 const entries=[...context.gallery.querySelectorAll(`[${context.cardAttr}]`)].map(element=>({element,row:context.rows[Number(element.getAttribute(context.cardAttr))]})).filter(entry=>entry.row);
 if(!entries.length)return;
 rangeState={context,pointerId:event.pointerId,x:event.pageX,y:event.pageY,clientX:event.clientX,clientY:event.clientY,
             active:false,additive:event.ctrlKey||event.metaKey||event.shiftKey,before:new Set(context.chosen),entries};
});
document.addEventListener('dragstart',event=>{if(rangeState)event.preventDefault();});
document.addEventListener('pointermove',event=>{
 if(!rangeState||rangeState.pointerId!==event.pointerId)return;
 rangeState.clientX=event.clientX;rangeState.clientY=event.clientY;
 if(!rangeState.active&&Math.hypot(event.pageX-rangeState.x,event.pageY-rangeState.y)<6)return;
 event.preventDefault();
 if(!rangeState.active){rangeState.active=true;rangeState.context.gallery.setPointerCapture(event.pointerId);marquee.classList.remove('hidden');document.body.classList.add('range-selecting');rangeFrame=requestAnimationFrame(scrollRange);}
 paintRange();
});
document.addEventListener('pointerup',event=>{if(rangeState?.pointerId===event.pointerId)finishRange();});
document.addEventListener('pointercancel',event=>{if(rangeState?.pointerId===event.pointerId)finishRange(true);});
window.addEventListener('blur',()=>finishRange(true));
window.addEventListener('scroll',()=>{if(rangeState?.active)paintRange();});
document.addEventListener('click',event=>{
 if(performance.now()<suppressGalleryClickUntil&&document.querySelector('.main').contains(event.target)){event.preventDefault();event.stopImmediatePropagation();}
},true);
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&rangeState){finishRange(true);event.preventDefault();return;}
 const context=selectionContext();
 if(!context||document.querySelector('.overlay:not(.hidden)')||event.target.closest('textarea,select,[contenteditable="true"],input:not([type="checkbox"]):not([type="radio"]):not([type="button"])'))return;
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a'){
  event.preventDefault();context.chosen.clear();context.rows.filter(context.canSelect).forEach(row=>context.chosen.add(row['__abs']));context.render();
 }
});
