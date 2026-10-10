'use strict';
let pairScanId='', pairState=null, pairRows=[], pairFiltered=[], pairFilter='orphans', pairPage=0, pairTimer, pairBusy=false;
const pairSelected=new Set();
const pairFilters={orphans:'缺失配对',missing_video:'缺少视频',missing_photo:'缺少照片',unknown:'无法判断'};

function pairMatches(row,filter){const state=row['配对状态'];return filter==='orphans'?state.startsWith('missing_'):filter==='unknown'?!state.startsWith('missing_'):state===filter;}
function setPairFolder(path){
 if(path===$('pair-dir').value&&pairState?.input_dir===path)return;
 $('pair-dir').value=path;pairScanId='';pairState=null;pairRows=[];pairSelected.clear();clearTimeout(pairTimer);
 try{localStorage.removeItem('light-album-pair-scan');}catch{}
 renderPairState();
}
async function startPairScan(){
 const root=$('pair-dir').value.trim();if(!root){toast('请先选择要检查的文件夹',true);$('pair-dir').focus();return;}
 $('start-pair-scan').disabled=true;
 try{
  const state=await api('/api/live-photos/scans',{input_dir:root,recursive:$('pair-recursive').checked});
  pairScanId=state.id;pairState=state;pairRows=[];pairSelected.clear();pairFilter='orphans';pairPage=0;
  try{localStorage.setItem('light-album-pair-scan',pairScanId);}catch{}
  renderPairState();await loadPairState();
 }catch(error){toast(error.message,true);$('start-pair-scan').disabled=false;}
}
async function loadPairState(){
 if(pairBusy)return;
 if(!pairScanId){try{pairScanId=localStorage.getItem('light-album-pair-scan')||'';}catch{}}
 if(!pairScanId){renderPairState();return;}
 const id=pairScanId;pairBusy=true;clearTimeout(pairTimer);
 try{
  const state=await api('/api/live-photos/scans/'+encodeURIComponent(id));if(id!==pairScanId)return;
  pairState=state;pairRows=state.rows||[];$('pair-dir').value=state.input_dir;$('pair-recursive').checked=state.recursive;
  const eligible=new Set(pairRows.filter(r=>r['__can_delete']).map(r=>r['__abs']));pairSelected.forEach(p=>{if(!eligible.has(p))pairSelected.delete(p);});
  renderPairState();if(['queued','running'].includes(state.status))pairTimer=setTimeout(loadPairState,1500);
 }catch(error){if(id===pairScanId){toast(error.message,true);pairScanId='';pairState=null;renderPairState();}}
 finally{pairBusy=false;}
}
function renderPairState(){
 const busy=pairState&&['queued','running'].includes(pairState.status);
 $('pair-dir').readOnly=!!busy;$('choose-pair-folder').disabled=!!busy;$('pair-recursive').disabled=!!busy;
 $('start-pair-scan').disabled=!!busy;$('start-pair-scan').textContent=busy?'正在检查…':pairState?'重新检查':'开始检查';
 $('cancel-pair-scan').classList.toggle('hidden',!busy);$('pair-progress').classList.toggle('hidden',!pairState);
 if(pairState){
  $('pair-progress-text').textContent=`${{queued:'准备检查',running:'正在读取实况 UUID',done:'检查完成',stale:'需要重新检查',failed:'检查未完成',cancelled:'检查已停止'}[pairState.status]||''} · ${pairState.done} / ${pairState.total} 个文件${pairState.cached?'，复用 '+pairState.cached+' 个缓存':''}`;
  $('pair-progress-bar').style.width=(pairState.total?Math.round(pairState.done/pairState.total*100):0)+'%';
 }
 const summary=pairState?.summary||{},complete=pairState&&['done','stale'].includes(pairState.status);
 $('pair-summary').classList.toggle('hidden',!complete);
 if(complete)$('pair-summary').innerHTML=[['paired','完整配对','组'],['missing_video','仅剩照片','个'],['missing_photo','仅剩视频','个'],['no_uuid','未读到实况 UUID','个']].map(([key,label,unit])=>`<div class="card"><span>${label}</span><strong>${summary[key]||0}<small>${unit}</small></strong></div>`).join('');
 let warning=pairState?.error||'';
 if(!warning&&complete)warning=summary.error||summary.conflict?`有 ${summary.error||0} 个文件读取失败、${summary.conflict||0} 个 UUID 无法确认。请处理后重新检查，再决定是否移入回收站。`:'缺失配对只表示当前扫描范围内未找到另一半，对方也可能在其他文件夹。未读到 UUID 的文件不会被当作实况残片。';
 $('pair-warning').textContent=warning;$('pair-warning').classList.toggle('hidden',!warning);
 $('pair-result-tools').classList.toggle('hidden',!complete);renderPairResults();
}
function renderPairResults(){
 const query=$('pair-search').value.trim().toLowerCase();pairFiltered=pairRows.filter(r=>pairMatches(r,pairFilter)&&(!query||[r['相对路径'],r['实况UUID']].some(v=>String(v).toLowerCase().includes(query))));
 const count=Math.max(1,Math.ceil(pairFiltered.length/pageSize));pairPage=Math.min(pairPage,count-1);const offset=pairPage*pageSize,visible=pairFiltered.slice(offset,offset+pageSize);
 $('pair-tabs').innerHTML=pairState&&['done','stale'].includes(pairState.status)?Object.entries(pairFilters).map(([key,label])=>`<button class="result-tab ${key===pairFilter?'active':''}" data-pair-filter="${key}">${label}<span>${pairRows.filter(r=>pairMatches(r,key)).length}</span></button>`).join(''):'';
 const cards=visible.map((r,n)=>`<article class="photo-card ${pairSelected.has(r['__abs'])?'selected':''}">${r['__can_delete']?`<label class="photo-checkbox"><input type="checkbox" data-pair-check="${offset+n}" aria-label="选择 ${esc(basename(r['相对路径']))}" ${pairSelected.has(r['__abs'])?'checked':''}></label>`:''}<button class="photo-image" data-pair-preview="${offset+n}" aria-label="预览 ${esc(basename(r['相对路径']))}">${thumbnail(r)}</button><div class="photo-info"><div class="photo-name" title="${esc(r['相对路径'])}">${esc(basename(r['相对路径']))}</div><span class="pill ${r['配对状态'].startsWith('missing_')?'amber':''}">${esc(r['建议标签'])}</span><div class="photo-meta" title="${esc(r['实况UUID']||r['失败环节'])}">${esc(r['实况UUID']||r['失败环节']||'无法确认是否来自实况照片')}</div></div></article>`).join('');
 const scanning=pairState&&['queued','running'].includes(pairState.status),incomplete=pairState&&['failed','cancelled'].includes(pairState.status);
 $('pair-results').innerHTML=visible.length?`<div class="photo-grid">${cards}</div>`:empty(!pairState?'选择文件夹检查实况照片':scanning?'正在读取实况 UUID':incomplete?'检查未完成':'没有符合条件的文件',!pairState?'无需先运行任何整理任务。':scanning?'完整读取后才显示缺失配对，避免把尚未扫描的文件误判为缺失。':incomplete?'请重新检查；读取过的元数据缓存已保留。':pairFilter==='unknown'?'没有无法判断的文件。':'当前范围内未发现这一类缺失配对。','stack');
 $('pair-pager').innerHTML=pairFiltered.length>pageSize?`<button data-pair-page="-1" ${pairPage===0?'disabled':''}>上一页</button><span>${pairPage+1} / ${count}</span><button data-pair-page="1" ${pairPage===count-1?'disabled':''}>下一页</button>`:'';
 const eligible=visible.filter(r=>r['__can_delete']);$('pair-select-page').disabled=!eligible.length;$('pair-select-page').checked=!!eligible.length&&eligible.every(r=>pairSelected.has(r['__abs']));
 $('pair-selection-count').textContent=` · 已选 ${pairSelected.size} 项`;$('delete-pair-files').disabled=!pairSelected.size;
 $('pair-results').querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{const fallback=document.createElement('span');fallback.innerHTML=icon('photo');img.replaceWith(fallback);}));
}
async function deletePairSelected(){
 if(!pairSelected.size)return;
 if(!confirm(`将选中的 ${pairSelected.size} 个缺失配对文件移入回收站？\n\n另一半可能在未扫描的文件夹里，请确认这些文件无需保留。之后可以从 Windows 回收站还原。`))return;
 $('delete-pair-files').disabled=true;
 try{const result=await api(`/api/live-photos/scans/${pairScanId}/delete`,{paths:[...pairSelected]});closeOverlay('preview-overlay');toast(`已移入回收站 ${result.deleted} 项${result.results.some(r=>!r.ok)?'，部分文件未成功处理':''}`);pairSelected.clear();if(result.deleted)await startPairScan();else await loadPairState();}
 catch(error){toast(error.message,true);await loadPairState();}
 finally{renderPairResults();}
}
document.addEventListener('click',async event=>{
 const target=event.target.closest('button');if(!target)return;
 if(target.dataset.pairFilter){pairFilter=target.dataset.pairFilter;pairPage=0;renderPairResults();return;}
 if(target.dataset.pairPage){pairPage+=Number(target.dataset.pairPage);renderPairResults();return;}
 if(target.dataset.pairPreview!==undefined){previewMode='pairs';previewIndex=Number(target.dataset.pairPreview);previewCompare=false;renderPreview();openOverlay('preview-overlay');return;}
 try{switch(target.id){
  case 'choose-pair-folder':pickerTarget='pair-dir';$('folder-title').textContent='选择要检查的文件夹';openOverlay('folder-overlay');await browseFolder($('pair-dir').value.trim());break;
  case 'start-pair-scan':await startPairScan();break;
  case 'cancel-pair-scan':if(pairScanId){await api(`/api/live-photos/scans/${pairScanId}/cancel`,{});toast('正在停止检查，读取过的元数据缓存会保留');}break;
  case 'delete-pair-files':await deletePairSelected();break;
 }}catch(error){toast(error.message,true);}
});
document.addEventListener('change',event=>{
 const element=event.target;
 if(element.dataset.pairCheck!==undefined){const row=pairFiltered[Number(element.dataset.pairCheck)];if(row?.__can_delete){element.checked?pairSelected.add(row['__abs']):pairSelected.delete(row['__abs']);renderPairResults();}}
 if(element.id==='pair-select-page'){pairFiltered.slice(pairPage*pageSize,(pairPage+1)*pageSize).filter(r=>r['__can_delete']).forEach(r=>element.checked?pairSelected.add(r['__abs']):pairSelected.delete(r['__abs']));renderPairResults();}
 if(element.id==='pair-dir')setPairFolder(element.value);
});
$('pair-search').addEventListener('input',()=>{pairPage=0;renderPairResults();});
renderPairState();
