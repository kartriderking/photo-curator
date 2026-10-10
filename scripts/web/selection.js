'use strict';
// Select gallery cards without opening their preview; keep checkbox selection independent.
let rangeState=null, rangeFrame=0, suppressGalleryClickUntil=0;
const gallery=$('photo-results'),marquee=document.createElement('div');
marquee.className='selection-marquee hidden';marquee.setAttribute('aria-hidden','true');document.body.appendChild(marquee);

function paintRange(){
 const state=rangeState;if(!state?.active)return;
 const sx=state.x-window.scrollX,sy=state.y-window.scrollY;
 const left=Math.min(sx,state.clientX),top=Math.min(sy,state.clientY),right=Math.max(sx,state.clientX),bottom=Math.max(sy,state.clientY);
 Object.assign(marquee.style,{left:left+'px',top:top+'px',width:(right-left)+'px',height:(bottom-top)+'px'});
 const chosen=new Set(state.additive?state.before:[]);
 for(const entry of state.entries){
  const rect=entry.element.getBoundingClientRect();
  if(photoSelectable(entry.row)&&rect.right>left&&rect.left<right&&rect.bottom>top&&rect.top<bottom)chosen.add(entry.row['__abs']);
 }
 selected.clear();chosen.forEach(path=>selected.add(path));
 for(const entry of state.entries){const checked=selected.has(entry.row['__abs']);entry.element.classList.toggle('selected',checked);const input=entry.element.querySelector('[data-check]');if(input)input.checked=checked;}
 const eligible=state.entries.filter(entry=>photoSelectable(entry.row));
 $('select-page').checked=!!eligible.length&&eligible.every(entry=>selected.has(entry.row['__abs']));
 $('select-page').indeterminate=eligible.some(entry=>selected.has(entry.row['__abs']))&&!$('select-page').checked;
 updateSelection();
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
 if(!rangeState)return;const state=rangeState;rangeState=null;cancelAnimationFrame(rangeFrame);
 if(state.active){
  if(cancelled){selected.clear();state.before.forEach(path=>selected.add(path));}
  suppressGalleryClickUntil=performance.now()+300;marquee.classList.add('hidden');document.body.classList.remove('range-selecting');
  if(gallery.hasPointerCapture(state.pointerId))gallery.releasePointerCapture(state.pointerId);
  renderPhotos();
 }
}
gallery.addEventListener('pointerdown',event=>{
 if(currentPage!=='results'||event.button!==0||!['mouse','pen'].includes(event.pointerType)||event.target.closest('input,select,textarea,label,a'))return;
 const entries=[...gallery.querySelectorAll('[data-card]')].map(element=>({element,row:filtered[Number(element.dataset.card)]})).filter(entry=>entry.row);
 if(!entries.length)return;
 rangeState={pointerId:event.pointerId,x:event.pageX,y:event.pageY,clientX:event.clientX,clientY:event.clientY,
             active:false,additive:event.ctrlKey||event.metaKey||event.shiftKey,before:new Set(selected),entries};
});
gallery.addEventListener('dragstart',event=>event.preventDefault());
document.addEventListener('pointermove',event=>{
 if(!rangeState||rangeState.pointerId!==event.pointerId)return;
 rangeState.clientX=event.clientX;rangeState.clientY=event.clientY;
 if(!rangeState.active&&Math.hypot(event.pageX-rangeState.x,event.pageY-rangeState.y)<6)return;
 event.preventDefault();
 if(!rangeState.active){rangeState.active=true;gallery.setPointerCapture(event.pointerId);marquee.classList.remove('hidden');document.body.classList.add('range-selecting');rangeFrame=requestAnimationFrame(scrollRange);}
 paintRange();
});
document.addEventListener('pointerup',event=>{if(rangeState?.pointerId===event.pointerId)finishRange();});
document.addEventListener('pointercancel',event=>{if(rangeState?.pointerId===event.pointerId)finishRange(true);});
window.addEventListener('blur',()=>finishRange(true));
window.addEventListener('scroll',()=>{if(rangeState?.active)paintRange();});
document.addEventListener('click',event=>{
 if(performance.now()<suppressGalleryClickUntil&&gallery.contains(event.target)){event.preventDefault();event.stopImmediatePropagation();}
},true);
document.addEventListener('keydown',event=>{
 if(event.key==='Escape'&&rangeState){finishRange(true);event.preventDefault();return;}
 if(currentPage!=='results'||document.querySelector('.overlay:not(.hidden)')||event.target.closest('textarea,select,[contenteditable="true"],input:not([type="checkbox"]):not([type="radio"]):not([type="button"])'))return;
 if((event.ctrlKey||event.metaKey)&&event.key.toLowerCase()==='a'){
  event.preventDefault();selected.clear();filtered.filter(photoSelectable).forEach(row=>selected.add(row['__abs']));renderPhotos();
 }
});
