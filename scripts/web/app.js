'use strict';
const $=id=>document.getElementById(id);
const esc=v=>String(v??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const icon=name=>`<svg class="icon" aria-hidden="true"><use href="#i-${name}"/></svg>`;
const names={gps:'读取拍摄信息',places:'查找城市和地名',phash:'查找相似照片（快速）',quality:'筛选模糊照片',similar:'AI 增强相似查找',classify:'识别照片类别',landmarks:'识别知名景点',trips:'按时间分组',frames:'提取视频画面'};
const presets={quick:['phash','quality'],travel:['gps','places','trips'],content:['classify','landmarks']};
const statusNames={queued:'等待整理',running:'正在整理',done:'已完成',partial:'部分完成',failed:'需要重试',cancelled:'已暂停'};
const categoryNames={all:'全部',similar:'相似照片',blurry:'模糊照片',documents:'截图与文档',travel:'旅行照片'};
let jobs=[], currentPage='home', currentJob='', mode='quick', rows=[], filtered=[], category='all', view='grid', page=0;
let requestVersion=0, jobsBusy=false, toastTimer, pickerCurrent='', pickerParent=null, pickerVersion=0, pickerTarget='dir', previewMode='results', previewIndex=-1, previewCompare=false, previousFocus=null, renderedJobsKey='', selectOptionsKey='';
const selected=new Set(), pageSize=48;
let resultRevision=null, keeperKeys=new Set();
const fileKey=path=>String(path||'').replace(/\\/g,'/').toLowerCase();
function photoSelectable(row){return !row['__protected_keep']&&!keeperKeys.has(fileKey(row['__abs']));}
try{previewCompare=localStorage.getItem('light-album-compare')==='1';}catch{}

async function api(path,body){
 let response,result;
 try{response=await fetch(path,body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});}catch{throw new Error('暂时连接不上本地工作台，请确认服务已启动');}
 try{result=await response.json();}catch{throw new Error('暂时无法完成操作，请稍后重试');}
 if(!response.ok) throw new Error(typeof result.detail==='string'?result.detail:'操作未完成，请稍后重试');
 return result;
}
function toast(message,error=false){clearTimeout(toastTimer);$('toast').textContent=message;$('toast').classList.toggle('error',error);$('toast').classList.remove('hidden');toastTimer=setTimeout(()=>$('toast').classList.add('hidden'),error?6500:3200);}
function basename(path){return String(path||'').replace(/[\\/]+$/,'').split(/[\\/]/).pop()||path;}
function dateText(value,withTime=false){if(!value||String(value)==='NaT')return '';const s=String(value).replace('T',' ');return s.slice(0,withTime?16:10).replace(/-/g,'/');}
function features(){return [...document.querySelectorAll('[name="feature"]:checked')].map(x=>x.value);}
function applyMode(next){mode=next;document.querySelectorAll('[name="feature"]').forEach(x=>x.checked=presets[next].includes(x.value));$('force').checked=false;updateMode();}
function updateMode(changedFeature=''){
 const quick=document.querySelector('[name="feature"][value="phash"]'),ai=$('similar-ai');
 if(changedFeature==='phash'&&!quick.checked)ai.checked=false;
 if(ai.checked)quick.checked=true;
 ai.disabled=!quick.checked;$('similar-thr').disabled=!ai.checked;$('hamming').disabled=!quick.checked;
 const f=features();mode=Object.keys(presets).find(m=>presets[m].length===f.length&&presets[m].every(k=>f.includes(k)))||'custom';
 document.querySelectorAll('[data-mode]').forEach(el=>{const chosen=el.dataset.mode===mode;el.classList.toggle('active',chosen);el.setAttribute('aria-pressed',chosen);el.querySelector('.mode-select').textContent=chosen?'✓':'';});
 $('custom-indicator').textContent=mode==='custom'?' · 已自定义':'';
 $('mode-caption').textContent=f.some(k=>['classify','similar','landmarks'].includes(k))?(f.includes('landmarks')?'需要本地识别服务，处理时间较长':f.includes('similar')?'已启用 AI 增强相似查找，处理较慢':'照片内容将在本机分析'):'本机快速处理，适合日常整理';
 const need=[];const scope=$('vlm-scope').value;
 if((f.includes('places')||f.includes('trips')||(f.includes('landmarks')&&scope==='no_gps'))&&!f.includes('gps'))need.push('拍摄信息');
 if(f.includes('landmarks')&&scope!=='all'&&!f.includes('classify'))need.push('照片类别');
 $('dependency-note').textContent=(need.length?'会自动读取所需的'+need.join('和')+'。 ':'')+(f.includes('frames')?'提取画面时会自动包含视频。 ':'')+'只运行你选择的功能。';
}
function showPage(next,load=true){
 currentPage=next;
 ['home','history','results','pairs'].forEach(p=>$('page-'+p).classList.toggle('hidden',p!==next));
 document.querySelectorAll('.nav-button').forEach(el=>el.classList.toggle('active',el.dataset.page===next));
 $('page-location').textContent={home:'开始整理',results:'我的照片',history:'整理记录',pairs:'检查实况照片'}[next];
 $('selection-bar').classList.toggle('hidden',next!=='results'||!selected.size);
 if(next==='results'){syncJobSelect();if(load&&!rows.length)loadResults();}
 if(next==='pairs'&&load)loadPairState();
 window.scrollTo({top:0,behavior:'smooth'});
}
function empty(title,description,kind='photo'){return `<div class="empty"><div class="empty-icon">${icon(kind)}</div><h3>${esc(title)}</h3><p>${esc(description)}</p></div>`;}
function jobRow(j){
 const tint={done:'green',running:'blue',queued:'amber',partial:'amber',failed:'red'}[j.status]||'';
 const usable=['done','partial','failed','cancelled','running'].includes(j.status);
 const summary=j.options?j.options.features.map(f=>names[f]).filter(Boolean).join(' · '):'之前的整理记录';
 return `<div class="job-row"><div class="folder-badge">${icon('folder')}</div><div class="job-main"><div class="job-name"><span title="${esc(j.input_dir)}">${esc(basename(j.input_dir))}</span><span class="pill ${tint}">${statusNames[j.status]||'整理记录'}</span></div><div class="job-sub" title="${esc(j.input_dir)}">${esc(dateText(j.created,true))}　${esc(summary)}</div></div><div class="job-actions">${usable?`<button class="link-button" data-action="view" data-id="${j.id}">查看照片</button>`:''}<details><summary aria-label="更多操作">•••</summary><div class="job-menu"><button data-action="reuse" data-id="${j.id}">再次整理这个文件夹</button>${j.status==='running'||j.status==='queued'?`<button data-action="cancel" data-id="${j.id}">暂停整理</button>`:`<button data-action="retry" data-id="${j.id}">继续上次整理</button>`}</div></details></div></div>`;
}
function syncJobSelect(){
 const usable=jobs.filter(j=>['done','partial','failed','cancelled','running'].includes(j.status));
 let saved='';try{saved=localStorage.getItem('light-album-job')||'';}catch{}
 if(!usable.some(j=>j.id===currentJob))currentJob=usable.some(j=>j.id===saved)?saved:(usable.find(j=>j.status==='done'&&!/test_images/i.test(j.input_dir))||usable[0])?.id||'';
 const optionsKey=JSON.stringify(usable.map(j=>[j.id,j.status]));
 if(optionsKey!==selectOptionsKey){$('sel-job').innerHTML=usable.length?usable.map(j=>`<option value="${j.id}">${esc(basename(j.input_dir))} · ${esc(dateText(j.created))}${j.status==='running'?' · 正在整理':''}</option>`).join(''):'<option value="">还没有整理记录</option>';selectOptionsKey=optionsKey;}
 $('sel-job').value=currentJob;
}
async function loadJobs(){
 if(jobsBusy)return;jobsBusy=true;
 try{
  const data=await api('/api/jobs');jobs=data.jobs;
  const jobsKey=JSON.stringify(jobs);
  if(jobsKey!==renderedJobsKey){
  const html=jobs.map(jobRow);$('recent-jobs').innerHTML=html.slice(0,3).join('')||empty('还没有整理过照片','选一个照片文件夹，开始第一次整理吧。','folder');$('history-jobs').innerHTML=html.join('')||empty('还没有整理记录','你整理过的文件夹会保存在这里。','clock');
  const roots=[...new Set(jobs.map(j=>j.input_dir))].slice(0,3);
  $('recent-folders').innerHTML=roots.length?'<span class="muted small">最近使用：</span>'+roots.map(p=>`<button class="folder-chip" data-folder="${esc(p)}" title="${esc(p)}">${esc(basename(p))}</button>`).join(''):'<span class="muted small">支持电脑、移动硬盘里的照片文件夹</span>';
  renderedJobsKey=jobsKey;
  }
  renderActivity(data);if(currentPage==='results')syncJobSelect();
 }catch(ex){$('activity').classList.remove('hidden');$('activity-title').textContent='暂时无法连接工作台';$('activity-detail').textContent='请确认本地服务已启动，稍后会自动重试。';}finally{jobsBusy=false;}
}
function renderActivity(data){
 const running=jobs.find(j=>j.status==='running')||jobs.find(j=>j.status==='queued');
 $('activity').classList.toggle('hidden',!running);if(!running)return;
 const p=data.progress||{};const active=Object.entries(p.features||{}).filter(([,s])=>s.status!=='skipped');
 const step=active.find(([,s])=>s.status==='running');
 const total=active.reduce((n,[,s])=>n+(s.total||0),0),done=active.reduce((n,[,s])=>n+(s.done||0),0);
 const pct=total?Math.min(100,Math.round(done/total*100)):0;
 $('activity-title').textContent=`${running.status==='queued'?'等待整理':'正在整理'} · ${basename(running.input_dir)}`;
 $('activity-detail').textContent=step?`${names[step[0]]||'正在处理'}${step[1].total?'　'+step[1].done+' / '+step[1].total+' 项':''}${step[1].cached?'，已复用 '+step[1].cached+' 项':''}`:'正在准备照片，请稍候…';
 $('progress-track').classList.toggle('busy',!total);$('activity-progress').style.width=pct+'%';
 $('activity-results').dataset.id=running.id;
 $('activity-stages').textContent=active.map(([f,s])=>`${names[f]}：${{pending:'等待中',running:'处理中',done:'完成',partial:'部分失败',failed:'失败'}[s.status]||s.status}`).join(' · ');
 $('pipe-log').textContent=(p.log_tail||[]).join('\n');
}
function reuseJob(id){
 const j=jobs.find(j=>j.id===id);if(!j)return;$('dir').value=j.input_dir;
 if(j.options){const o=j.options;document.querySelectorAll('[name="feature"]').forEach(x=>x.checked=o.features.includes(x.value));$('videos').checked=o.include_videos;for(const [key,input] of [['geo_mode','geo-mode'],['vlm_scope','vlm-scope'],['scenic_threshold','thr'],['similar_threshold','similar-thr'],['dup_hamming','hamming'],['trip_gap_hours','trip-gap'],['vlm_model','vlm-model']])$(input).value=o[key];$('reuse-legacy-vlm').checked=o.reuse_legacy_vlm!==false;updateMode();$('advanced').open=mode==='custom';}
 else{applyMode('quick');$('videos').checked=false;}
 $('force').checked=false;showPage('home');$('dir').focus();
}
async function startJob(){
 const path=$('dir').value.trim();if(!path){toast('请先选择一个照片文件夹',true);$('dir').focus();return;}
 const chosen=features();if(!chosen.length){toast('请至少选择一项整理功能',true);$('advanced').open=true;return;}
 const button=$('start-button');button.disabled=true;button.innerHTML='<span class="spinner"></span>正在开始…';
 const body={input_dir:path,features:chosen,include_videos:$('videos').checked,geo_mode:$('geo-mode').value,vlm_scope:$('vlm-scope').value,scenic_threshold:Number($('thr').value),similar_threshold:Number($('similar-thr').value),dup_hamming:Number($('hamming').value),trip_gap_hours:Number($('trip-gap').value),vlm_model:$('vlm-model').value,force:$('force').checked,reuse_legacy_vlm:$('reuse-legacy-vlm').checked};
 try{await api('/api/jobs',body);toast('已开始整理，完成后可以查看照片');await loadJobs();$('activity').scrollIntoView({behavior:'smooth',block:'nearest'});}catch(ex){toast(ex.message,true);}finally{button.disabled=false;button.innerHTML=icon('sparkle')+'开始整理';}
}
async function viewJob(id){currentJob=id;selected.clear();rows=[];category='all';showPage('results',false);await loadResults();}
async function loadResults(refresh=false){
 const id=$('sel-job').value||currentJob;const ticket=++requestVersion;
 selected.clear();updateSelection();$('photo-results').innerHTML=empty('正在读取照片','读取已有整理结果，不会重新运行分析。');$('pager').innerHTML='';
 if(!id){rows=[];filtered=[];$('photo-results').innerHTML=empty('还没有照片结果','先选择一个文件夹开始整理。');$('res-count').textContent='';return;}
 currentJob=id;try{localStorage.setItem('light-album-job',id);}catch{}
 try{
  const data=await api(`/api/jobs/${id}/results?refresh=${refresh?1:0}`);
  if(ticket!==requestVersion)return;rows=data.rows||[];resultRevision=data.report_revision??null;keeperKeys=new Set(rows.filter(r=>r['__abs_best']).map(r=>fileKey(r['__abs_best'])));page=0;applyFilter();
  if(refresh)toast('已刷新已有结果');
 }catch(ex){if(ticket!==requestVersion)return;rows=[];filtered=[];$('photo-results').innerHTML=empty('暂时无法读取照片',ex.message);$('res-count').textContent='';toast(ex.message,true);}
}
function matches(row,type){const label=String(row['建议标签']||'');return type==='all'||({similar:label==='可删-重复'||/相似/.test(label),blurry:label==='可删-模糊',documents:/截图|文档/.test(label),travel:['景点','旅行素材'].includes(label)})[type];}
function labelInfo(row){
 if(row['配对状态'])return [row['建议标签'],row['配对状态'].startsWith('missing_')?'amber':''];
 if(!photoSelectable(row))return ['建议保留，已保护','green'];
 const l=String(row['建议标签']||'');
 if(l==='可删-重复'||/相似/.test(l))return ['相似照片，待确认','amber'];if(l==='可删-模糊')return ['可能模糊','amber'];if(/截图|文档/.test(l))return ['截图 / 文档',''];
 if(l==='景点')return [row['VLM景点']||'景点照片','blue'];if(l==='旅行素材')return ['旅行照片','blue'];
 if(row['失败环节'])return ['需要重新检查','red'];if(rows.some(r=>r['__abs_best']===row['__abs']))return ['推荐保留','green'];
 return [{日常:'日常照片',未评估:'尚未分析',未判重复:'未发现重复',保留候选:'保留候选'}[l]||l||'未分类',''];
}
function renderCategories(){
 $('result-tabs').innerHTML=Object.entries(categoryNames).map(([k,n])=>`<button class="result-tab ${k===category?'active':''}" data-category="${k}">${n}<span>${rows.filter(r=>matches(r,k)).length}</span></button>`).join('');
}
function applyFilter(){
 const q=$('q').value.trim().toLowerCase();filtered=rows.filter(r=>matches(r,category)&&(!q||['相对路径','VLM景点','VLM城市','城市(GPS)','地名','VLM场景','拍摄时间'].some(k=>String(r[k]||'').toLowerCase().includes(q))));
 const order=$('sort-order').value;if(order==='name')filtered.sort((a,b)=>String(a['相对路径']).localeCompare(String(b['相对路径']),'zh'));
 if(order.startsWith('date'))filtered.sort((a,b)=>{const x=String(a['拍摄时间']||''),y=String(b['拍摄时间']||'');if(!x)return 1;if(!y)return -1;return x.localeCompare(y)*(order==='date-desc'?-1:1);});
 renderCategories();renderPhotos();
}
function thumbnail(row){return row['类型']==='video'?icon('play'):`<img src="/media/photo?path=${encodeURIComponent(row['__abs']||'')}" alt="${esc(basename(row['相对路径']))}" loading="lazy" decoding="async">`;}
function renderPhotos(){
 const count=Math.max(1,Math.ceil(filtered.length/pageSize));page=Math.min(page,count-1);const start=page*pageSize;const visible=filtered.slice(start,start+pageSize);
 const content=visible.map((r,n)=>{
  const index=start+n,label=labelInfo(r),selectable=photoSelectable(r);if(!selectable)selected.delete(r['__abs']);const checked=selected.has(r['__abs']);const location=r['VLM城市']||r['城市(GPS)']||r['地名'];const meta=[dateText(r['拍摄时间']),location].filter(Boolean).join(' · ')||(r['类型']==='video'?'视频':'照片');
  const disabled=selectable?'':'disabled title="建议保留项已保护"';
  if(view==='list')return `<tr data-card="${index}" class="${checked?'selected':''}"><td><input type="checkbox" data-check="${index}" aria-label="选择 ${esc(basename(r['相对路径']))}" ${checked?'checked':''} ${disabled}></td><td><div class="list-file">${thumbnail(r)}<button data-preview="${index}">${esc(basename(r['相对路径']))}</button></div></td><td>${esc(dateText(r['拍摄时间']))||'—'}</td><td>${esc(location)||'—'}</td><td><span class="pill ${label[1]}">${esc(label[0])}</span></td><td><button class="link-button" data-preview="${index}">查看</button></td></tr>`;
  return `<article class="photo-card ${checked?'selected':''}" data-card="${index}"><label class="photo-checkbox"><input type="checkbox" data-check="${index}" aria-label="选择 ${esc(basename(r['相对路径']))}" ${checked?'checked':''} ${disabled}></label>${r['类型']==='video'?'<span class="photo-type">'+icon('play')+'视频</span>':''}<button class="photo-image" data-preview="${index}" aria-label="预览 ${esc(basename(r['相对路径']))}">${thumbnail(r)}</button><div class="photo-info"><div class="photo-name" title="${esc(r['相对路径'])}">${esc(basename(r['相对路径']))}</div><span class="pill ${label[1]}">${esc(label[0])}</span><div class="photo-meta">${esc(meta)}</div></div></article>`;
 }).join('');
 $('photo-results').innerHTML=visible.length?(view==='list'?'<div class="list-scroll"><table class="list-view"><thead><tr><th style="width:44px"></th><th>照片</th><th>拍摄时间</th><th>地点</th><th>整理建议</th><th></th></tr></thead><tbody>'+content+'</tbody></table></div>':'<div class="photo-grid">'+content+'</div>'):empty(rows.length?'没有找到符合条件的照片':'还没有可查看的照片',rows.length?'换个关键词或分类试试。':'整理进行中时，可以稍后点击刷新查看结果。');
 $('res-count').textContent=` · ${filtered.length} 项`;
 $('pager').innerHTML=filtered.length>pageSize?`<button data-page-turn="-1" ${page===0?'disabled':''}>上一页</button><span>${page+1} / ${count}</span><button data-page-turn="1" ${page===count-1?'disabled':''}>下一页</button>`:'';
 const eligible=visible.filter(photoSelectable);$('select-page').checked=!!eligible.length&&eligible.every(r=>selected.has(r['__abs']));$('select-page').indeterminate=eligible.some(r=>selected.has(r['__abs']))&&!$('select-page').checked;$('select-page').disabled=!eligible.length;
 $('photo-results').querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{const fallback=document.createElement('span');fallback.innerHTML=icon('photo');img.replaceWith(fallback);}));
 updateSelection();
}
function updateSelection(){
 $('selection-bar').classList.toggle('hidden',!selected.size||currentPage!=='results');$('selection-count').textContent=`已选择 ${selected.size} 项`;
 if(previewMode==='results'&&previewIndex>=0&&filtered[previewIndex])$('preview-select').checked=selected.has(filtered[previewIndex]['__abs']);
}
async function deleteSelected(){
 const bypath=new Map(rows.map(r=>[r['__abs'],r]));selected.forEach(path=>{if(!bypath.has(path)||!photoSelectable(bypath.get(path)))selected.delete(path);});updateSelection();
 if(!selected.size)return;const paths=[...selected];
 if(!confirm(`将选中的 ${paths.length} 个文件移入回收站？\n\n请确认已看过照片。之后可以在 Windows 回收站中还原。`))return;
 $('delete-button').disabled=true;
 try{const data=await api('/api/delete',{job_id:currentJob,paths,report_revision:resultRevision});const ok=new Set(data.results.filter(r=>r.ok).map(r=>r.path));rows=rows.filter(r=>!ok.has(r['__abs']));ok.forEach(p=>selected.delete(p));closeOverlay('preview-overlay');applyFilter();toast(`已移入回收站 ${data.deleted} 项${data.results.some(r=>!r.ok)?'，部分文件被保护或未成功处理':''}`);}catch(ex){toast(ex.message,true);}finally{$('delete-button').disabled=false;}
}
function openOverlay(id){previousFocus=document.activeElement;$(id).classList.remove('hidden');document.body.style.overflow='hidden';$(id).querySelector('button,input')?.focus();}
function closeOverlay(id){$(id).classList.add('hidden');if(id==='preview-overlay'){$('preview-media').innerHTML='';previewIndex=-1;}document.body.style.overflow='';if(previousFocus?.isConnected)previousFocus.focus();}
async function browseFolder(path=''){
 const ticket=++pickerVersion;pickerCurrent='';$('folder-confirm').disabled=true;$('folder-list').innerHTML='<p class="muted small" style="padding:18px">正在读取文件夹…</p>';
 try{const data=await api('/api/folders?path='+encodeURIComponent(path));if(ticket!==pickerVersion)return;pickerCurrent=data.path;pickerParent=data.parent;$('folder-path').value=data.path;$('folder-up').disabled=data.parent===null;$('folder-confirm').disabled=!data.path;$('folder-caption').textContent=data.path?(pickerTarget==='pair-dir'?'选择当前文件夹检查实况 UUID 配对。':'将整理此文件夹及其子文件夹中的照片。'):'选择一个磁盘，进入照片所在的文件夹。';$('folder-list').innerHTML=data.folders.length?data.folders.map(f=>`<button class="picker-folder" data-browse="${esc(f.path)}">${icon('folder')}<span>${esc(f.name)}</span>${icon('chevron')}</button>`).join(''):'<p class="muted small" style="padding:18px">没有子文件夹，可以直接选择当前文件夹。</p>';if(data.truncated)$('folder-caption').textContent='子文件夹较多，仅显示前 500 个；可输入完整路径前往。';}catch(ex){if(ticket!==pickerVersion)return;$('folder-list').innerHTML=empty('无法打开文件夹',ex.message,'folder');}
}
function openPreview(index){previewMode='results';previewIndex=index;renderPreview();openOverlay('preview-overlay');}
function mediaHTML(row,path){return row['类型']==='video'?`<video controls playsinline src="/media/video?path=${encodeURIComponent(path)}"></video>`:`<img src="/media/photo?path=${encodeURIComponent(path)}" alt="照片预览">`;}
function previewRows(){return previewMode==='pairs'?pairFiltered:filtered;}
function renderPreview(){
 const collection=previewRows();const r=collection[previewIndex];if(!r)return;const label=labelInfo(r),best=r['__abs_best'],comparing=previewMode==='results'&&previewCompare&&best;$('preview-title').textContent=comparing?'对比照片':'照片预览';
 $('preview-position').textContent=`${previewIndex+1} / ${collection.length}`;$('preview-select').checked=(previewMode==='pairs'?pairSelected:selected).has(r['__abs']);$('preview-select').disabled=previewMode==='pairs'?!r['__can_delete']:!photoSelectable(r);
 $('preview-media').innerHTML=comparing?`<figure><figcaption>当前照片</figcaption>${mediaHTML(r,r['__abs'])}</figure><figure><figcaption>建议保留</figcaption>${mediaHTML({...r,'类型':/\.(mp4|mov|mkv|avi)$/i.test(best)?'video':'photo'},best)}</figure>`:mediaHTML(r,r['__abs']);
 const fields=[['拍摄时间',dateText(r['拍摄时间'],true)],['拍摄地点',[r['VLM城市']||r['城市(GPS)'],r['地名']].filter(Boolean).join(' · ')],['景点',r['VLM景点']],['照片内容',r['VLM场景']],['配对检查',r['被删的一半']?'已删除：'+r['被删的一半']:''],['提示',r['失败环节']]];
 const detail=Object.entries(r).filter(([k,v])=>!k.startsWith('__')&&v!==''&&v!=null).map(([k,v])=>`<dl class="info-item"><dt>${esc(k)}</dt><dd>${esc(k==='建议标签'?label[0]:v)}</dd></dl>`).join('');
 $('preview-info').innerHTML=`<h3>${esc(basename(r['相对路径']))}</h3><span class="pill ${label[1]}">${esc(label[0])}</span>${fields.filter(([,v])=>v).map(([k,v])=>`<dl class="info-item"><dt>${k}</dt><dd>${esc(v)}</dd></dl>`).join('')}${best?`<button class="secondary" id="compare-button">${previewCompare?'返回单张预览':'对比建议保留的照片'}</button>`:''}<details><summary>文件详情</summary>${detail}</details>`;
 $('preview-media').querySelectorAll('video').forEach(video=>video.addEventListener('error',()=>{const wrap=document.createElement('div');wrap.style.textAlign='center';wrap.innerHTML=`<img src="/media/frame?path=${encodeURIComponent(r['__abs'])}" alt="视频画面"><p class="muted small" style="margin-top:10px">浏览器暂不支持此视频格式</p><button class="secondary" id="transcode-video" style="margin-top:12px">转换后播放</button>`;video.replaceWith(wrap);wrap.querySelector('button').onclick=()=>{wrap.innerHTML=`<video controls autoplay src="/media/transcode?path=${encodeURIComponent(r['__abs'])}"></video>`;};}));
 $('preview-media').querySelectorAll('img').forEach(img=>img.addEventListener('error',()=>{const note=document.createElement('p');note.textContent='暂时无法预览，请检查文件是否仍然存在。';note.className='muted small';img.replaceWith(note);}));
}
function navigatePreview(delta){const collection=previewRows();if(!collection.length)return;previewIndex=(previewIndex+delta+collection.length)%collection.length;renderPreview();}
document.addEventListener('click',async event=>{
 const target=event.target.closest('button,[data-close]');if(!target)return;
 try{
  if(target.dataset.page){showPage(target.dataset.page);return;}
  if(target.dataset.mode){applyMode(target.dataset.mode);return;}
  if(target.dataset.folder){$('dir').value=target.dataset.folder;return;}
  if(target.dataset.action){const id=target.dataset.id;const action=target.dataset.action;if(action==='view')await viewJob(id);else if(action==='reuse')reuseJob(id);else{await api(`/api/jobs/${id}/${action}`,{});toast(action==='cancel'?'已暂停整理，已有结果会保留':'已继续上次整理');await loadJobs();}return;}
  if(target.dataset.category){category=target.dataset.category;page=0;applyFilter();return;}
  if(target.dataset.view){view=target.dataset.view;document.querySelectorAll('[data-view]').forEach(el=>el.classList.toggle('active',el.dataset.view===view));renderPhotos();return;}
  if(target.dataset.pageTurn){page+=Number(target.dataset.pageTurn);renderPhotos();$('photo-results').scrollIntoView({behavior:'smooth',block:'start'});return;}
  if(target.dataset.preview!==undefined){openPreview(Number(target.dataset.preview));return;}
  if(target.dataset.browse!==undefined){await browseFolder(target.dataset.browse);return;}
  if(target.dataset.close){closeOverlay(target.dataset.close);return;}
  switch(target.id){
   case 'choose-folder':pickerTarget='dir';$('folder-title').textContent='选择照片文件夹';openOverlay('folder-overlay');await browseFolder($('dir').value.trim());break;
   case 'folder-up':await browseFolder(pickerParent||'');break;
   case 'folder-go':await browseFolder($('folder-path').value.trim());break;
   case 'folder-confirm':if(pickerCurrent){if(pickerTarget==='pair-dir')setPairFolder(pickerCurrent);else $('dir').value=pickerCurrent;closeOverlay('folder-overlay');}break;
   case 'start-button':await startJob();break;
   case 'activity-results':await viewJob(target.dataset.id);break;
   case 'refresh-results':await loadResults(true);break;
   case 'export-button':if(currentJob)window.location.href=`/api/jobs/${currentJob}/export`;else toast('请先选择一条整理记录');break;
   case 'clear-selection':selected.clear();renderPhotos();break;
   case 'delete-button':await deleteSelected();break;
   case 'preview-prev':navigatePreview(-1);break;
   case 'preview-next':navigatePreview(1);break;
   case 'compare-button':previewCompare=!previewCompare;try{localStorage.setItem('light-album-compare',previewCompare?'1':'0');}catch{}renderPreview();break;
   case 'preview-original':{const r=previewRows()[previewIndex];if(r)window.open(`/media/${r['类型']==='video'?'video':'photo'}?path=${encodeURIComponent(r['__abs'])}`,'_blank','noopener');break;}
  }
 }catch(ex){toast(ex.message,true);}
});
document.addEventListener('change',event=>{
 const el=event.target;
 if(el.name==='feature'||el.id==='vlm-scope')updateMode(el.name==='feature'?el.value:'');
 if(el.dataset.check!==undefined){const r=filtered[Number(el.dataset.check)];if(r&&photoSelectable(r)){el.checked?selected.add(r['__abs']):selected.delete(r['__abs']);renderPhotos();}}
 if(el.id==='select-page'){filtered.slice(page*pageSize,(page+1)*pageSize).filter(photoSelectable).forEach(r=>el.checked?selected.add(r['__abs']):selected.delete(r['__abs']));renderPhotos();}
 if(el.id==='preview-select'){const r=previewRows()[previewIndex];if(r){const chosen=previewMode==='pairs'?pairSelected:selected;if(previewMode==='pairs'?!r['__can_delete']:!photoSelectable(r))return;el.checked?chosen.add(r['__abs']):chosen.delete(r['__abs']);previewMode==='pairs'?renderPairResults():renderPhotos();}}
 if(el.id==='sel-job'){currentJob=el.value;category='all';closeOverlay('preview-overlay');loadResults();}
 if(el.id==='sort-order'){page=0;applyFilter();}
});
$('q').addEventListener('input',()=>{page=0;applyFilter();});
$('folder-path').addEventListener('keydown',e=>{if(e.key==='Enter')browseFolder(e.target.value.trim());});
document.querySelectorAll('.overlay').forEach(el=>el.addEventListener('click',e=>{if(e.target===el)closeOverlay(el.id);}));
document.addEventListener('keydown',e=>{
 const overlay=document.querySelector('.overlay:not(.hidden)');if(!overlay)return;
 if(e.key==='Escape'){closeOverlay(overlay.id);return;}
 if(e.key==='Tab'){const focusable=[...overlay.querySelectorAll('button:not(:disabled),input:not(:disabled),summary,a[href],video')].filter(el=>el.offsetParent!==null);const first=focusable[0],last=focusable.at(-1);if(e.shiftKey&&document.activeElement===first){e.preventDefault();last?.focus();}else if(!e.shiftKey&&document.activeElement===last){e.preventDefault();first?.focus();}}
 if(overlay.id==='preview-overlay'&&!['INPUT','SELECT','TEXTAREA'].includes(e.target.tagName)){if(e.key==='ArrowLeft'){e.preventDefault();navigatePreview(-1);}if(e.key==='ArrowRight'){e.preventDefault();navigatePreview(1);}}
});
updateMode();loadJobs();setInterval(loadJobs,3000);
