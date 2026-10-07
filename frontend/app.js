const API = (window.MY_WIKI_API_BASE || document.querySelector('meta[name="api-base"]')?.content || '/api/v1').replace(/\/$/, '');
const requestedView = new URLSearchParams(location.search).get('view');
const initialView = ['documents','wiki','ask'].includes(requestedView) ? requestedView : 'documents';
const state = { bases: [], activeId: null, documents: [], wikiPages: [], wikiAllPages: [], activeWikiPage: null, progressDocumentId: null, wikiMode: 'reader', view: initialView, tab: 'file', sessionId: null, sessions: [], legacySessions: [], chatMessages: [], chatRun: null, chatEpoch: 0, chatLoading: false, hasOlder: false };

const $ = (selector) => document.querySelector(selector);
const escapeHtml = (value = '') => String(value).replace(/[&<>'"]/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;',"'":'&#39;','"':'&quot;'}[c]));
const toast = (message, error = false) => { const el=$('#toast'); el.textContent=message; el.className=error?'show error':'show'; clearTimeout(toast.timer); toast.timer=setTimeout(()=>el.className='',2600); };

async function withSubmitLock(form, action) {
  const submit = form.querySelector('button[type="submit"], button:not([type])');
  if (form.dataset.submitting === 'true') return;
  form.dataset.submitting = 'true';
  if (submit) submit.disabled = true;
  try { await action(); }
  finally {
    delete form.dataset.submitting;
    if (submit) submit.disabled = false;
  }
}

async function request(path, options = {}) {
  const response = await fetch(API + path, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(payload.error?.message || payload.detail?.[0]?.msg || '请求失败');
  return payload.data;
}

async function requestText(path) {
  const response=await fetch(API+path);
  if (!response.ok) {
    const payload=await response.json().catch(()=>({}));
    throw new Error(payload.error?.message || '内容加载失败');
  }
  return response.text();
}

function renderInlineMarkdown(value) {
  let html=escapeHtml(value);
  html=html.replace(/`([^`]+)`/g,'<code>$1</code>');
  html=html.replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');
  html=html.replace(/__([^_]+)__/g,'<strong>$1</strong>');
  html=html.replace(/(^|[^*])\*([^*]+)\*/g,'$1<em>$2</em>');
  html=html.replace(/\[\[([a-z0-9/_-]+)(?:\|([^\]]+))?\]\]/g,(_,slug,label)=>`<button class="wiki-link" data-wiki-slug="${slug}">${label||slug}</button>`);
  return html;
}

function renderMarkdown(markdown, documentId) {
  const root=document.createElement('article'); root.className='wiki-content';
  const lines=markdown.replace(/\r\n?/g,'\n').split('\n');
  let index=0;
  while(index<lines.length) {
    const line=lines[index];
    if(!line.trim()){ index+=1; continue; }
    const page=line.match(/^<!--\s*page:(\d+)\s*-->$/);
    if(page){ const marker=document.createElement('div'); marker.className='wiki-page'; marker.textContent=`第 ${page[1]} 页`; root.append(marker); index+=1; continue; }
    if(line.startsWith('```')) {
      const language=line.slice(3).trim(), code=[]; index+=1;
      while(index<lines.length && !lines[index].startsWith('```')) code.push(lines[index++]);
      if(index<lines.length) index+=1;
      const pre=document.createElement('pre'), content=document.createElement('code');
      if(language) content.dataset.language=language;
      content.textContent=code.join('\n'); pre.append(content); root.append(pre); continue;
    }
    const image=line.trim().match(/^!\[([^\]]*)\]\(artifact:\/\/([a-zA-Z0-9_-]+)\)$/);
    if(image) {
      const figure=document.createElement('figure'), img=document.createElement('img');
      img.src=`${API}/artifacts/${encodeURIComponent(documentId)}/images/by-id/${encodeURIComponent(image[2])}`;
      img.alt=image[1] || '文档图片'; img.loading='lazy'; figure.append(img);
      if(image[1]) { const caption=document.createElement('figcaption'); caption.textContent=image[1]; figure.append(caption); }
      root.append(figure); index+=1; continue;
    }
    const heading=line.match(/^(#{1,6})\s+(.+)$/);
    if(heading){ const title=document.createElement(`h${heading[1].length}`); title.innerHTML=renderInlineMarkdown(heading[2]); root.append(title); index+=1; continue; }
    if(line.includes('|') && index+1<lines.length && /^\s*\|?(?:\s*:?-+:?\s*\|)+\s*:?-+:?\s*\|?\s*$/.test(lines[index+1])) {
      const rows=[];
      const cells=value=>value.trim().replace(/^\||\|$/g,'').split('|').map(cell=>cell.trim());
      rows.push(cells(line)); index+=2;
      while(index<lines.length && lines[index].includes('|') && lines[index].trim()) rows.push(cells(lines[index++]));
      const wrapper=document.createElement('div'); wrapper.className='wiki-table-wrap';
      const table=document.createElement('table'), head=document.createElement('thead'), headRow=document.createElement('tr');
      rows[0].forEach(value=>{ const cell=document.createElement('th'); cell.innerHTML=renderInlineMarkdown(value); headRow.append(cell); });
      head.append(headRow); table.append(head);
      if(rows.length>1){ const body=document.createElement('tbody'); rows.slice(1).forEach(row=>{ const tr=document.createElement('tr'); row.forEach(value=>{ const cell=document.createElement('td'); cell.innerHTML=renderInlineMarkdown(value); tr.append(cell); }); body.append(tr); }); table.append(body); }
      wrapper.append(table); root.append(wrapper); continue;
    }
    const list=line.match(/^\s*(?:([-+*])|(\d+)\.)\s+(.+)$/);
    if(list) {
      const ordered=Boolean(list[2]), container=document.createElement(ordered?'ol':'ul');
      while(index<lines.length) {
        const item=lines[index].match(/^\s*(?:([-+*])|(\d+)\.)\s+(.+)$/);
        if(!item || Boolean(item[2])!==ordered) break;
        const li=document.createElement('li'); li.innerHTML=renderInlineMarkdown(item[3]); container.append(li); index+=1;
      }
      root.append(container); continue;
    }
    if(line.startsWith('> ')) { const quote=document.createElement('blockquote'); quote.innerHTML=renderInlineMarkdown(line.slice(2)); root.append(quote); index+=1; continue; }
    const paragraph=[];
    while(index<lines.length && lines[index].trim()) {
      const current=lines[index];
      if(paragraph.length && (/^(#{1,6})\s+/.test(current) || current.startsWith('```') || /^<!--\s*page:/.test(current) || /^\s*(?:[-+*]|\d+\.)\s+/.test(current))) break;
      paragraph.push(current); index+=1;
    }
    const text=document.createElement('p'); text.innerHTML=renderInlineMarkdown(paragraph.join(' ')); root.append(text);
  }
  return root;
}

async function checkHealth() {
  try { await request('/health/ready'); $('#healthDot').classList.add('online'); $('#healthText').textContent='服务在线'; }
  catch { $('#healthText').textContent='连接失败'; }
}

async function loadBases() {
  try {
    state.bases = await request('/knowledge-bases');
    renderBases();
    if (!state.activeId && state.bases.length) selectBase(state.bases[0].id);
  } catch (error) { toast(error.message, true); }
}

function renderBases() {
  $('#kbList').innerHTML = state.bases.length ? state.bases.map(item => `
    <button class="kb-item ${item.id===state.activeId?'active':''}" data-id="${item.id}">
      <strong>${escapeHtml(item.name)}</strong><span>→</span><small>${escapeHtml(item.description || '无描述')}</small>
    </button>`).join('') : '<p class="muted">还没有知识库，点击右上角 ＋ 创建。</p>';
  document.querySelectorAll('.kb-item').forEach(el => el.onclick=()=>selectBase(el.dataset.id));
}

async function selectBase(id) {
  if (state.activeId !== id) { detachChat(); state.sessions=[]; state.legacySessions=[]; renderSessions(); renderChat(); }
  state.activeId=id; state.progressDocumentId=null; renderBases();
  const base=state.bases.find(x=>x.id===id); $('#activeKbName').textContent=base?.name || '知识库';
  $('#workspaceTitle').textContent=base?.name || '知识库';
  $('#addDocumentBtn').disabled=false; $('#question').disabled=false; $('#chatForm button').disabled=false;
  await loadDocuments();
  if (state.activeId !== id) return;
  await loadSessions(true);
  if(state.view==='wiki') loadWikiProgress();
}

async function loadDocuments() {
  try { const kbId=state.activeId, documents=await request(`/knowledge-bases/${kbId}/knowledge`); if(kbId!==state.activeId)return; state.documents=documents; renderDocuments(); renderChatScope(); }
  catch(error){ toast(error.message,true); }
}

function renderDocuments() {
  const root=$('#documentList'); root.className='document-list'+(state.documents.length?'':' empty-state');
  $('#clearKbBtn').disabled=!state.activeId || !state.documents.length || state.documents.some(doc=>['pending','processing'].includes(doc.parse_status));
  root.innerHTML=state.documents.length ? state.documents.map((doc,index)=>`
    <article class="document-row" style="animation-delay:${index*40}ms">
      <span class="doc-icon">${doc.source_type==='file'?'▤':'¶'}</span><div><div class="doc-title" title="${escapeHtml(doc.title)}">${escapeHtml(doc.title)}</div>
      <div class="doc-meta"><button class="status-tag ${doc.parse_status}" data-stages="${doc.id}" title="查看处理阶段">${statusName(doc.parse_status)}</button> · ${doc.chunk_count} 个片段</div></div>
      <div class="doc-actions"><button data-wiki="${doc.id}" title="查看关联 Wiki" ${doc.parse_status==='completed'?'':'disabled'}>关联页</button><button data-chunks="${doc.id}" title="查看分片" ${doc.parse_status==='completed'?'':'disabled'}>分片</button><button data-retry="${doc.id}" title="重新处理">↻</button><button data-delete="${doc.id}" title="删除">×</button></div>
    </article>`).join('') : '<span class="empty-mark">⌁</span><p>这里还没有资料</p>';
  root.querySelectorAll('[data-delete]').forEach(el=>el.onclick=()=>removeDocument(el.dataset.delete));
  root.querySelectorAll('[data-retry]').forEach(el=>el.onclick=()=>reprocess(el.dataset.retry));
  root.querySelectorAll('[data-wiki]').forEach(el=>el.onclick=()=>openWikiForDocument(el.dataset.wiki));
  root.querySelectorAll('[data-chunks]').forEach(el=>el.onclick=()=>viewChunks(el.dataset.chunks));
  root.querySelectorAll('[data-stages]').forEach(el=>el.onclick=()=>viewStages(el.dataset.stages));
}
const statusName = status => ({pending:'等待中',processing:'处理中',completed:'已完成',failed:'失败'}[status] || status);

async function removeDocument(id) {
  if (!confirm('确定删除这份资料吗？此操作不可撤销。')) return;
  try { await request(`/knowledge/${id}`,{method:'DELETE'}); toast('资料已删除'); await loadDocuments(); }
  catch(error){ toast(error.message,true); }
}
async function reprocess(id) {
  try { await request(`/knowledge/${id}/reprocess`,{method:'POST'}); toast('已加入处理队列'); await loadDocuments(); }
  catch(error){ toast(error.message,true); }
}

async function clearKnowledgeBase() {
  if (!state.activeId || !state.documents.length) return;
  const base=state.bases.find(item=>item.id===state.activeId);
  const confirmed=confirm(`确定清理“${base?.name || '当前知识库'}”吗？\n\n其中 ${state.documents.length} 份资料及解析结果将被永久删除，知识库本身会保留。`);
  if (!confirmed) return;
  const button=$('#clearKbBtn'); button.disabled=true;
  try {
    const result=await request(`/knowledge-bases/${state.activeId}/knowledge`,{method:'DELETE'});
    state.documents=[]; renderDocuments();
    toast(`已清理 ${result.deleted_count} 份资料`);
  } catch(error){ toast(`清理失败：${error.message}`,true); renderDocuments(); }
}

function openViewer(documentId, eyebrow) {
  const item=state.documents.find(document=>document.id===documentId);
  $('#viewerEyebrow').textContent=eyebrow;
  $('#viewerTitle').textContent=item?.title || '资料详情';
  $('#viewerBody').innerHTML='<p class="muted">正在加载…</p>';
  const dialog=$('#viewerDialog');
  if (!dialog.open) dialog.showModal();
  return $('#viewerBody');
}

async function viewWiki(documentId) {
  const body=openViewer(documentId,'KNOWLEDGE WIKI');
  try {
    const pages=await request(`/knowledge-bases/${state.activeId}/wiki/pages?knowledge_id=${encodeURIComponent(documentId)}`);
    body.innerHTML='';
    if(!pages.length){ body.innerHTML='<p class="muted">这份资料尚未生成 Wiki 页面。</p>'; return; }
    const shell=document.createElement('div'); shell.className='wiki-reader-layout';
    const nav=document.createElement('nav'); nav.className='wiki-page-nav'; nav.setAttribute('aria-label','Wiki 页面');
    const content=document.createElement('section'); content.className='wiki-page-reader';
    const showPage=page=>{
      nav.querySelectorAll('button').forEach(button=>button.classList.toggle('active',button.dataset.pageId===page.id));
      content.innerHTML='';
      const meta=document.createElement('div'); meta.className='wiki-page-meta';
      meta.textContent=page.page_type==='summary'?'概览':page.page_type==='topic'?'主题页':'原文';
      const summary=document.createElement('p'); summary.className='wiki-page-summary'; summary.textContent=page.summary;
      content.append(meta,summary,renderMarkdown(page.content,page.knowledge_id));
    };
    pages.forEach(page=>{
      const button=document.createElement('button'); button.type='button'; button.dataset.pageId=page.id;
      const title=document.createElement('strong'); title.textContent=page.title;
      const summary=document.createElement('span'); summary.textContent=page.summary;
      button.append(title,summary); button.onclick=()=>showPage(page); nav.append(button);
    });
    shell.append(nav,content); body.append(shell); showPage(pages[0]);
  } catch(error){ body.innerHTML=''; const message=document.createElement('p'); message.className='muted'; message.textContent=error.message; body.append(message); }
}

function switchView(view) {
  state.view=view;
  document.querySelectorAll('.view-tabs button').forEach(button=>button.classList.toggle('active',button.dataset.view===view));
  document.querySelectorAll('[data-view-panel]').forEach(panel=>panel.classList.toggle('active',panel.dataset.viewPanel===view));
  if(view==='wiki' && state.activeId) { loadWikiPages(); loadWikiProgress(); }
}

const PIPELINE_STEPS = [
  ['parsing','文档解析','读取版面、页码、表格与图片'],
  ['chunking','Chunk 切分','建立可检索的语义片段'],
  ['embedding','向量索引','生成真实模型向量'],
  ['indexing','索引写入','写入稠密与关键词索引'],
  ['candidate_extraction','候选抽取','识别实体与核心概念'],
  ['citation_mapping','引文映射','把候选绑定到 Chunk 证据'],
  ['deduplication','页面去重','复用已有规范页面'],
  ['taxonomy','目录规划','生成稳定的两级目录'],
  ['reduce','多源合并','综合多份来源生成正文'],
  ['persist','持久化','原子更新页面与贡献'],
  ['finalize','链接收束','重建索引、内链与反链'],
  ['quality','质量检查','检查死链、孤页与来源'],
];

const compactMetric = value => {
  if(typeof value==='string') {
    try { value=JSON.parse(value); } catch { return ''; }
  }
  if(!value || typeof value!=='object') return '';
  const preferred=['chunks','vectors','candidates','candidates_with_evidence','citations','pages','folders','issues','total_pages','dead_links'];
  return preferred.filter(key=>value[key]!==undefined).slice(0,2).map(key=>`${key} ${value[key]}`).join(' · ');
};

async function loadWikiProgress() {
  const root=$('#wikiPipeline');
  if(!state.activeId || !state.documents.length){
    root.innerHTML='<div class="pipeline-empty"><span>PROCESS MAP</span><p>添加资料后，这里会显示 Wiki 构建全过程。</p></div>';
    return;
  }
  const selected=state.documents.find(doc=>doc.id===state.progressDocumentId);
  const processing=state.documents.find(doc=>['pending','processing'].includes(doc.parse_status));
  const document=selected || processing || state.documents[0];
  state.progressDocumentId=document.id;
  try {
    const [stages,builds]=await Promise.all([
      request(`/knowledge/${document.id}/stages`),
      request(`/knowledge/${document.id}/wiki-builds`),
    ]);
    const build=builds[0];
    const detail=build ? await request(`/wiki/builds/${build.id}`) : null;
    renderWikiProgress(document,stages,detail);
  } catch(error) {
    root.innerHTML=`<div class="pipeline-empty error"><span>PIPELINE ERROR</span><p>${escapeHtml(error.message)}</p></div>`;
  }
}

function renderWikiProgress(document,stages,build) {
  const root=$('#wikiPipeline');
  const latestParsing=[...stages].reverse().find(stage=>stage.stage==='parsing');
  const attemptStarted=latestParsing?.started_at || '';
  const attemptStages=attemptStarted ? stages.filter(stage=>stage.started_at>=attemptStarted) : stages;
  const currentBuild=build && (!attemptStarted || build.started_at>=attemptStarted) ? build : null;
  const stageMap=new Map(attemptStages.map(stage=>[stage.stage,stage]));
  const buildMap=new Map((currentBuild?.stages||[]).map(stage=>[stage.stage,stage]));
  const steps=PIPELINE_STEPS.map(([key,label,description],index)=>{
    const source=index<4 ? stageMap.get(key) : buildMap.get(key);
    let status=source?.status || 'pending';
    if(document.parse_status==='completed' && !source) status='completed';
    if(document.parse_status==='failed' && !source) status='blocked';
    const output=source?.output || source?.output_summary || {};
    return {key,label,description,status,metric:compactMetric(output),error:source?.error_message||''};
  });
  let blocked=false;
  for(const step of steps) {
    if(blocked && step.status==='pending') step.status='blocked';
    if(step.status==='failed') blocked=true;
  }
  const completed=steps.filter(step=>step.status==='completed').length;
  const failed=steps.find(step=>step.status==='failed');
  const running=steps.find(step=>step.status==='running') || steps.find(step=>step.status==='pending');
  const percent=document.parse_status==='completed' ? 100 : Math.round(completed/steps.length*100);
  const buildTone=failed || document.parse_status==='failed' ? 'failed' : document.parse_status==='completed' ? 'completed' : 'running';
  const mode=document.parse_status==='failed' ? 'ATTEMPT FAILED' : currentBuild?.mode==='model' ? 'REAL MODEL' : currentBuild ? 'DEGRADED' : 'PREPARING';
  root.innerHTML=`
    <header class="pipeline-head">
      <div><small>WIKI ASSEMBLY LINE / ${escapeHtml(mode)}</small><h2>${escapeHtml(document.title)}</h2></div>
      <div class="pipeline-switcher" aria-label="选择资料">${state.documents.map(doc=>`<button class="${doc.id===document.id?'active':''}" data-progress-doc="${doc.id}" title="${escapeHtml(doc.title)}">${escapeHtml(doc.title)}</button>`).join('')}</div>
      <div class="pipeline-meter ${buildTone}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${percent}">
        <strong>${String(percent).padStart(2,'0')}<i>%</i></strong><span>${failed?'构建失败':document.parse_status==='completed'?'构建完成':`正在${running?.label||'准备'}`}</span>
      </div>
    </header>
    <div class="pipeline-track" style="--progress:${percent}%">
      ${steps.map((step,index)=>`<article class="pipeline-step ${step.status}" title="${escapeHtml(step.description)}">
        <div class="step-node"><span>${String(index+1).padStart(2,'0')}</span></div>
        <div class="step-copy"><strong>${escapeHtml(step.label)}</strong><small>${escapeHtml(step.metric||step.description)}</small>${step.error?`<em title="${escapeHtml(step.error)}">${escapeHtml(step.error)}</em>`:''}</div>
      </article>`).join('')}
    </div>`;
  root.querySelectorAll('[data-progress-doc]').forEach(button=>button.onclick=()=>{
    state.progressDocumentId=button.dataset.progressDoc; loadWikiProgress();
  });
}

async function loadWikiPages(query='') {
  const root=$('#wikiPageList'); root.innerHTML='<p class="muted">正在整理目录…</p>';
  try {
    state.wikiPages=await request(`/knowledge-bases/${state.activeId}/wiki/pages${query?`?q=${encodeURIComponent(query)}`:''}`);
    if(!query) state.wikiAllPages=state.wikiPages;
    const groups={index:'索引',entity:'实体',concept:'概念',topic:'主题',summary:'文档概览',source:'原文'};
    root.innerHTML='';
    for(const [type,label] of Object.entries(groups)) {
      const pages=state.wikiPages.filter(page=>page.page_type===type && page.status!=='archived');
      if(!pages.length) continue;
      const section=document.createElement('section'); section.innerHTML=`<small>${label}</small>`;
      pages.forEach(page=>{ const button=document.createElement('button'); button.dataset.pageId=page.id; button.innerHTML=`<strong>${escapeHtml(page.title)}</strong><span>${escapeHtml(page.summary||page.slug)}</span>`; button.onclick=()=>showWikiPage(page); section.append(button); });
      root.append(section);
    }
    if(!root.children.length) root.innerHTML='<p class="muted">没有匹配的 Wiki 页面。</p>';
    if(state.wikiMode==='graph' && !query) renderWikiGraph();
  } catch(error){ root.innerHTML=`<p class="muted">${escapeHtml(error.message)}</p>`; }
}

function setWikiMode(mode) {
  state.wikiMode=mode;
  document.querySelectorAll('[data-wiki-mode]').forEach(button=>button.classList.toggle('active',button.dataset.wikiMode===mode));
  $('#wikiArticle').hidden=mode==='graph'; $('#wikiEvidence').hidden=mode==='graph'; $('#wikiGraph').hidden=mode!=='graph';
  if(mode==='graph') {
    $('#wikiSearch').value='';
    if(state.wikiAllPages.length) renderWikiGraph(); else loadWikiPages();
  }
}

function renderWikiGraph() {
  const root=$('#graphCanvas');
  const pages=state.wikiAllPages.filter(page=>page.status!=='archived');
  const bySlug=new Map(pages.map(page=>[page.slug,page]));
  const edges=[]; const linkPattern=/\[\[([a-z0-9][a-z0-9/_-]*)(?:\|[^\]]+)?\]\]/gi;
  pages.forEach(page=>{
    const targets=new Set(); let match;
    while((match=linkPattern.exec(page.content||''))) if(bySlug.has(match[1]) && match[1]!==page.slug) targets.add(match[1]);
    targets.forEach(target=>edges.push({source:page.slug,target}));
  });
  $('#graphSummary').textContent=`${pages.length} 个页面 · ${edges.length} 条内部关系`;
  if(!pages.length) { root.innerHTML='<div class="graph-empty"><span>∅</span><p>生成 Wiki 页面后，关系会在这里浮现。</p></div>'; return; }

  const width=Math.max(root.clientWidth,720), height=Math.max(root.clientHeight,520);
  const typeOrder={index:0,entity:1,concept:2,topic:3,summary:4,source:5};
  const nodes=pages.map((page,index)=>({page,index,x:width/2+Math.cos(index*2.4)*Math.min(width,height)*.28,y:height/2+Math.sin(index*2.4)*Math.min(width,height)*.28,vx:0,vy:0}));
  const nodeBySlug=new Map(nodes.map(node=>[node.page.slug,node]));
  const svg=document.createElementNS('http://www.w3.org/2000/svg','svg');
  svg.setAttribute('viewBox',`0 0 ${width} ${height}`); svg.setAttribute('role','img'); svg.setAttribute('aria-label','Wiki 页面关系图谱');
  const viewport=document.createElementNS(svg.namespaceURI,'g'); viewport.classList.add('graph-viewport'); svg.append(viewport);
  const edgeLayer=document.createElementNS(svg.namespaceURI,'g'); edgeLayer.classList.add('graph-edges'); viewport.append(edgeLayer);
  const edgeEls=edges.map(edge=>{ const line=document.createElementNS(svg.namespaceURI,'line'); edgeLayer.append(line); return {line,edge}; });
  const nodeLayer=document.createElementNS(svg.namespaceURI,'g'); nodeLayer.classList.add('graph-nodes'); viewport.append(nodeLayer);
  const nodeEls=nodes.map(node=>{
    const group=document.createElementNS(svg.namespaceURI,'g'); group.classList.add('graph-node'); group.dataset.type=node.page.page_type; group.setAttribute('tabindex','0'); group.setAttribute('role','button'); group.setAttribute('aria-label',`打开 ${node.page.title}`);
    const radius=node.page.page_type==='index'?17:node.page.page_type==='source'||node.page.page_type==='summary'?10:13;
    const circle=document.createElementNS(svg.namespaceURI,'circle'); circle.setAttribute('r',radius);
    const label=document.createElementNS(svg.namespaceURI,'text'); label.setAttribute('y',radius+17); label.textContent=node.page.title.length>14?`${node.page.title.slice(0,14)}…`:node.page.title;
    group.append(circle,label); nodeLayer.append(group);
    const open=()=>{ if(node.dragMoved){ node.dragMoved=false; return; } setWikiMode('reader'); showWikiPage(node.page); };
    group.onclick=open; group.onkeydown=event=>{ if(event.key==='Enter'||event.key===' '){ event.preventDefault(); open(); } };
    return {group,node};
  });
  root.replaceChildren(svg);

  let scale=1, tx=0, ty=0, dragged=null;
  const applyTransform=()=>viewport.setAttribute('transform',`translate(${tx} ${ty}) scale(${scale})`);
  const draw=()=>{
    edgeEls.forEach(({line,edge})=>{ const a=nodeBySlug.get(edge.source), b=nodeBySlug.get(edge.target); line.setAttribute('x1',a.x); line.setAttribute('y1',a.y); line.setAttribute('x2',b.x); line.setAttribute('y2',b.y); });
    nodeEls.forEach(({group,node})=>group.setAttribute('transform',`translate(${node.x} ${node.y})`));
  };
  for(let tick=0;tick<180;tick++) {
    nodes.forEach(node=>{ node.vx+=(width/2-node.x)*.0008; node.vy+=(height/2-node.y)*.0008; });
    for(let a=0;a<nodes.length;a++) for(let b=a+1;b<nodes.length;b++) { const dx=nodes[b].x-nodes[a].x, dy=nodes[b].y-nodes[a].y, d2=Math.max(dx*dx+dy*dy,100), force=950/d2; nodes[a].vx-=dx*force*.02; nodes[a].vy-=dy*force*.02; nodes[b].vx+=dx*force*.02; nodes[b].vy+=dy*force*.02; }
    edges.forEach(edge=>{ const a=nodeBySlug.get(edge.source), b=nodeBySlug.get(edge.target), dx=b.x-a.x, dy=b.y-a.y, distance=Math.max(Math.hypot(dx,dy),1), force=(distance-125)*.0015; a.vx+=dx/distance*force; a.vy+=dy/distance*force; b.vx-=dx/distance*force; b.vy-=dy/distance*force; });
    nodes.forEach(node=>{ node.vx*=.82; node.vy*=.82; node.x=Math.max(35,Math.min(width-35,node.x+node.vx)); node.y=Math.max(35,Math.min(height-45,node.y+node.vy)); });
  }
  draw();
  svg.onwheel=event=>{ event.preventDefault(); scale=Math.max(.55,Math.min(2.4,scale*(event.deltaY>0?.9:1.1))); applyTransform(); };
  nodeEls.forEach(({group,node})=>group.onpointerdown=event=>{ node.dragMoved=false; dragged={node}; group.setPointerCapture(event.pointerId); });
  svg.onpointermove=event=>{ if(!dragged)return; const point=svg.createSVGPoint(); point.x=event.clientX; point.y=event.clientY; const local=point.matrixTransform(viewport.getScreenCTM().inverse()); dragged.node.x=local.x; dragged.node.y=local.y; dragged.node.dragMoved=true; draw(); };
  svg.onpointerup=()=>{ dragged=null; };
  $('#graphResetBtn').onclick=()=>{ scale=1; tx=0; ty=0; applyTransform(); };
}

async function showWikiPage(page) {
  if(state.wikiMode!=='reader') setWikiMode('reader');
  state.activeWikiPage=page;
  document.querySelectorAll('.wiki-directory button').forEach(button=>button.classList.toggle('active',button.dataset.pageId===page.id));
  const article=$('#wikiArticle'); article.className='wiki-article'; article.innerHTML='';
  const head=document.createElement('header');
  head.innerHTML=`<div><small>${escapeHtml(page.page_type.toUpperCase())} · V${page.version}</small><h2>${escapeHtml(page.title)}</h2><p>${escapeHtml(page.summary||'')}</p></div><button class="button" id="editWikiPageBtn">编辑</button>`;
  const content=renderMarkdown(page.content,page.knowledge_id); article.append(head,content);
  if(page.knowledge_id) showWikiBuildStatus(page.knowledge_id,head);
  article.querySelector('#editWikiPageBtn').onclick=()=>openWikiEditor(page);
  article.querySelectorAll('[data-wiki-slug]').forEach(link=>link.onclick=()=>openWikiSlug(link.dataset.wikiSlug));
  const evidence=$('#wikiEvidence'); evidence.innerHTML='<small>SOURCES</small><h3>证据与链接</h3><p class="muted">正在加载…</p>';
  try {
    const [sources,backlinks]=await Promise.all([
      request(`/knowledge-bases/${state.activeId}/wiki/pages/${page.id}/sources`),
      request(`/knowledge-bases/${state.activeId}/wiki/pages/${page.id}/backlinks`),
    ]);
    evidence.innerHTML='<small>SOURCES</small><h3>证据与链接</h3>';
    if(!sources.length) evidence.insertAdjacentHTML('beforeend','<p class="muted">这是人工页面，尚未绑定来源。</p>');
    sources.forEach(source=>{ const card=document.createElement('button'); card.className='source-card'; card.innerHTML=`<strong>${escapeHtml(source.knowledge_title)}</strong><span>${source.page_number?`第 ${source.page_number} 页 · `:''}${escapeHtml(source.heading_path||'文档证据')}</span><p>${escapeHtml(source.excerpt||'')}</p>`; card.onclick=()=>viewChunks(source.knowledge_id); evidence.append(card); });
    if(backlinks.length){ evidence.insertAdjacentHTML('beforeend','<small class="backlink-label">BACKLINKS</small>'); backlinks.forEach(link=>{ const button=document.createElement('button'); button.className='source-card'; button.innerHTML=`<strong>← ${escapeHtml(link.title)}</strong><span>${escapeHtml(link.slug)}</span>`; button.onclick=()=>openWikiSlug(link.slug); evidence.append(button); }); }
  } catch(error){ evidence.insertAdjacentHTML('beforeend',`<p class="muted">${escapeHtml(error.message)}</p>`); }
}

async function showWikiBuildStatus(knowledgeId,head) {
  try {
    const builds=await request(`/knowledge/${knowledgeId}/wiki-builds`); const latest=builds[0];
    if(!latest || latest.status==='completed') return;
    const warning=document.createElement('div'); warning.className='wiki-build-warning';
    warning.textContent=latest.mode==='offline'
      ? '当前页面由离线规则生成：未配置模型，实体、概念和跨文档综合质量会明显受限。'
      : `最近一次 Wiki 构建状态：${latest.status}`;
    head.after(warning);
  } catch {}
}

async function openWikiSlug(slug) {
  try { showWikiPage(await request(`/knowledge-bases/${state.activeId}/wiki/pages/by-slug/${encodeURIComponent(slug)}`)); }
  catch(error){ toast(error.message,true); }
}

async function openWikiForDocument(documentId) {
  switchView('wiki');
  const pages=await request(`/knowledge-bases/${state.activeId}/wiki/pages?knowledge_id=${encodeURIComponent(documentId)}`);
  if(pages.length) showWikiPage(pages[0]); else toast('这份资料尚未生成关联页面',true);
}

function openWikiEditor(page=null) {
  const form=$('#wikiEditorForm'); form.reset(); form.elements.page_id.value=page?.id||'';
  form.elements.title.value=page?.title||''; form.elements.slug.value=page?.slug||'concept/';
  form.elements.summary.value=page?.summary||''; form.elements.content.value=page?.content||'';
  $('#wikiEditorTitle').textContent=page?'编辑页面':'新建页面'; $('#wikiEditorDialog').showModal();
}

async function viewChunks(documentId) {
  const body=openViewer(documentId,'KNOWLEDGE CHUNKS');
  try {
    const chunks=await request(`/knowledge/${documentId}/chunks`);
    body.innerHTML='';
    if (!chunks.length) { body.innerHTML='<p class="muted">这份资料还没有分片。</p>'; return; }
    for (const chunk of chunks) {
      const card=document.createElement('article'); card.className='chunk-card';
      const index=document.createElement('span'); index.className='chunk-index'; index.textContent=String(chunk.chunk_index+1).padStart(2,'0');
      const detail=document.createElement('div');
      const meta=document.createElement('div'); meta.className='chunk-meta';
      meta.textContent=[chunk.heading_path || '无标题路径',chunk.page_number?`第 ${chunk.page_number} 页`:null,`${chunk.content.length} 字符`].filter(Boolean).join(' · ');
      const content=document.createElement('p'); content.className='chunk-content'; content.textContent=chunk.content;
      detail.append(meta,content); card.append(index,detail); body.append(card);
    }
  } catch(error){ body.innerHTML=''; const message=document.createElement('p'); message.className='muted'; message.textContent=error.message; body.append(message); }
}

async function viewStages(documentId) {
  const body=openViewer(documentId,'PROCESSING LOG');
  try {
    const stages=await request(`/knowledge/${documentId}/stages`); body.innerHTML='';
    if(!stages.length){ body.innerHTML='<p class="muted">尚未开始处理。</p>'; return; }
    stages.forEach((stage,index)=>{ const card=document.createElement('article'); card.className='chunk-card'; card.innerHTML=`<span class="chunk-index">${String(index+1).padStart(2,'0')}</span><div><div class="chunk-meta">${escapeHtml(stage.status)} · ${escapeHtml(stage.started_at)}</div><strong>${escapeHtml(stage.stage)}</strong>${stage.error_message?`<p class="chunk-content">${escapeHtml(stage.error_message)}</p>`:''}</div>`; body.append(card); });
  } catch(error){ body.innerHTML=`<p class="muted">${escapeHtml(error.message)}</p>`; }
}


function activeSession() {
  return [...state.sessions, ...state.legacySessions].find(item=>item.id===state.sessionId);
}

function detachChat() {
  state.chatEpoch+=1;
  if(state.chatRun) state.chatRun.controller.abort();
  clearTimeout(loadChatHistory.timer);
  state.chatRun=null; state.chatLoading=false; state.sessionId=null; state.chatMessages=[]; state.hasOlder=false;
}

function resetChat() {
  detachChat();
  if(state.activeId) localStorage.removeItem('wiki-session:'+state.activeId);
  renderSessions(); renderChatScope(); renderChat();
}

async function loadSessions(restore=false) {
  const kbId=state.activeId, epoch=state.chatEpoch;
  if(!kbId)return;
  try {
    const [sessions, legacy]=await Promise.all([request('/sessions?knowledge_base_id='+encodeURIComponent(kbId)),request('/sessions?legacy=true')]);
    if(kbId!==state.activeId||epoch!==state.chatEpoch)return;
    state.sessions=sessions; state.legacySessions=legacy; renderSessions();
    if(restore) {
      const saved=localStorage.getItem('wiki-session:'+kbId);
      if(sessions.some(item=>item.id===saved)) await selectSession(saved);
      else resetChat();
    } else updateChatControls();
  } catch(error){ toast('会话加载失败：'+error.message,true); }
}

function renderSessions() {
  for(const [selector,sessions] of [['#sessionList',state.sessions],['#legacySessionList',state.legacySessions]]) {
    const list=$(selector); list.replaceChildren();
    if(!sessions.length) { const p=document.createElement('p'); p.className='muted'; p.textContent='暂无会话'; list.append(p); }
    for(const session of sessions) {
      const button=document.createElement('button'); button.className='session-item'+(session.id===state.sessionId?' active':'');
      button.setAttribute('aria-current',String(session.id===state.sessionId));
      const title=document.createElement('strong'),meta=document.createElement('small'); title.textContent=session.title;
      meta.textContent=(session.active_turn?'正在生成 · ':'')+new Date(session.updated_at).toLocaleDateString('zh-CN');
      button.append(title,meta); button.onclick=()=>selectSession(session.id); list.append(button);
    }
  }
}

async function selectSession(id) {
  detachChat(); state.sessionId=id;
  const session=activeSession();
  if(session?.knowledge_base_id) localStorage.setItem('wiki-session:'+state.activeId,id);
  renderSessions(); renderChatScope(); renderChat();
  await loadChatHistory();
}

async function loadChatHistory(older=false) {
  const sid=state.sessionId,epoch=state.chatEpoch;
  if(!sid)return;
  state.chatLoading=true; updateChatControls();
  const cursor=older&&state.chatMessages.length?'&before='+encodeURIComponent(state.chatMessages[0].id):'';
  try {
    const messages=await request('/sessions/'+encodeURIComponent(sid)+'/messages?limit=30'+cursor);
    if(epoch!==state.chatEpoch||sid!==state.sessionId)return;
    const previousHeight=$('#chatLog').scrollHeight,previousTop=$('#chatLog').scrollTop;
    state.chatMessages=older?[...messages,...state.chatMessages]:messages;
    state.hasOlder=messages.length===30;
    renderChat();
    if(older)$('#chatLog').scrollTop=previousTop+$('#chatLog').scrollHeight-previousHeight;
    const generating=state.chatMessages.some(m=>['generating','stopping'].includes(m.status));
    const session=activeSession(); if(session&&!generating) session.active_turn=null;
    if(generating&&!state.chatRun) loadChatHistory.timer=setTimeout(()=>loadChatHistory(),1500);
  } catch(error){ if(epoch===state.chatEpoch)toast('聊天记录加载失败：'+error.message,true); }
  finally { if(epoch===state.chatEpoch){state.chatLoading=false;updateChatControls();} }
}

function renderChatScope() {
  const session=activeSession(), root=$('#scopeDocuments');
  const previous=[...root.querySelectorAll('input:checked')].map(input=>input.value);
  const selected=session?.knowledge_ids||previous;
  root.replaceChildren();
  for(const doc of state.documents) {
    const label=document.createElement('label'),input=document.createElement('input');
    input.type='checkbox'; input.value=doc.id; input.checked=selected.includes(doc.id); input.disabled=Boolean(session)||Boolean(state.chatRun)||doc.parse_status!=='completed';
    input.onchange=updateScopeSummary;
    label.append(input,document.createTextNode(doc.title+(doc.parse_status==='completed'?'':'（尚不可检索）')));root.append(label);
  }
  if(!state.documents.length)root.textContent='尚无资料，请先到“资料”上传文档。';
  if(session?.knowledge_ids.some(id=>!state.documents.some(doc=>doc.id===id))) {
    const p=document.createElement('p');p.textContent='部分限定资料已删除，请新建会话选择范围。';root.append(p);
  }
  updateScopeSummary();
}

function updateScopeSummary() {
  const session=activeSession();
  const count=session?session.knowledge_ids.length:$('#scopeDocuments').querySelectorAll('input:checked').length;
  $('#scopeSummary').textContent=session&&!session.knowledge_base_id?'旧会话 · 范围未知，仅供查看':'检索范围：'+(count?count+' 份指定资料':'当前知识库全部资料');
}

function updateChatControls() {
  const session=activeSession(), legacy=session&&!session.knowledge_base_id;
  const busy=Boolean(state.chatRun)||state.chatMessages.some(m=>['generating','stopping'].includes(m.status));
  const unavailable=!state.activeId||Boolean(legacy)||busy||state.chatLoading;
  $('#question').disabled=unavailable; $('#sendQuestionBtn').disabled=unavailable;
  $('#sendQuestionBtn').hidden=busy; $('#stopAnswerBtn').hidden=!busy;
  $('#stopAnswerBtn').disabled=!state.sessionId;
  for(const id of ['renameSessionBtn','clearChatBtn','deleteSessionBtn']) $('#'+id).disabled=!session||busy||state.chatLoading;
  $('#newSessionBtn').disabled=!state.activeId;
  $('#sessionTitle').textContent=session?.title||'新的研究问题';
  $('#olderMessagesBtn').hidden=!state.hasOlder; $('#olderMessagesBtn').disabled=busy||state.chatLoading;
  $('#chatStatus').textContent=legacy?'旧记录保留原始内容；新提问请新建会话。':busy?'正在查阅资料并生成回答…':!state.documents.length?'当前知识库尚无资料，请先上传。':'答案基于所选资料；离线模式提供摘取式回答。';
}

function renderChat() {
  const log=$('#chatLog'),scrollTop=log.scrollTop,nearBottom=log.scrollHeight-scrollTop-log.clientHeight<80;
  log.replaceChildren();
  if(!state.chatMessages.length) {
    const empty=document.createElement('div');empty.className='chat-empty';
    empty.innerHTML='<span>“</span><h3>从一个问题开始</h3><p>查找事实、梳理概念，或继续追问。<br>回答中的引用会带你回到原始资料。</p>';
    log.append(empty);
  }
  for(const message of state.chatMessages) log.append(renderChatMessage(message));
  log.scrollTop=nearBottom?log.scrollHeight:scrollTop;
  updateChatControls();
}

function renderChatMessage(message) {
  const article=document.createElement('article');article.className='message '+message.role;
  const avatar=document.createElement('span');avatar.className='avatar';avatar.textContent=message.role==='user'?'你':'W';
  const body=document.createElement('div');body.className='message-body';
  let references=[];try{references=JSON.parse(message.references_json||'[]');}catch{}
  const content=message.role==='assistant'?renderMarkdown(message.content||''):document.createElement('p');
  if(message.role==='user')content.textContent=message.content;
  // Only transform text nodes outside code blocks; model output never becomes raw HTML.
  if(message.role==='assistant') {
    const walker=document.createTreeWalker(content,NodeFilter.SHOW_TEXT),nodes=[];
    while(walker.nextNode())if(!walker.currentNode.parentElement.closest('code,pre,button'))nodes.push(walker.currentNode);
    for(const node of nodes) {
      const matches=[...node.textContent.matchAll(/\[(\d+)\]/g)];if(!matches.length)continue;
      const fragment=document.createDocumentFragment();let offset=0;
      for(const match of matches){fragment.append(document.createTextNode(node.textContent.slice(offset,match.index)));const ref=references.find(r=>r.index===Number(match[1]));
        if(ref){const button=document.createElement('button');button.className='citation';button.textContent=match[0];button.setAttribute('aria-label','查看引用 '+match[1]);button.onclick=()=>showChatReference(ref);fragment.append(button);}
        else fragment.append(document.createTextNode(match[0]));offset=match.index+match[0].length;
      }
      fragment.append(document.createTextNode(node.textContent.slice(offset)));node.replaceWith(fragment);
    }
    content.querySelectorAll('[data-wiki-slug]').forEach(button=>{button.disabled=!activeSession()?.knowledge_base_id;button.onclick=()=>{switchView('wiki');openWikiSlug(button.dataset.wikiSlug);};});
  }
  body.append(content);
  if(references.length) {
    const refs=document.createElement('div');refs.className='references';
    for(const ref of references){const button=document.createElement('button');button.className='reference';button.textContent='['+ref.index+'] '+ref.knowledge_title+(ref.page_number?' · P'+ref.page_number:'');button.onclick=()=>showChatReference(ref);refs.append(button);}body.append(refs);
  }
  if(message.role==='assistant') {
    const actions=document.createElement('div');actions.className='message-actions';
    const status=document.createElement('span');status.textContent=({generating:'生成中',stopping:'正在停止',completed:'已完成',failed:'生成失败 · 已保留内容',stopped:'已停止 · 内容可能不完整'})[message.status]||'';actions.append(status);
    const copy=document.createElement('button');copy.className='text-btn';copy.textContent='复制';copy.disabled=!message.content;
    copy.onclick=async()=>{try{await navigator.clipboard.writeText(message.content);toast('已复制回答');}catch{toast('复制失败，请手动选择文本',true);}};actions.append(copy);
    if(['failed','stopped'].includes(message.status)&&message===state.chatMessages.at(-1)&&activeSession()?.knowledge_base_id) {
      const retry=document.createElement('button');retry.className='text-btn';retry.textContent='重新生成';retry.disabled=Boolean(state.chatRun);
      retry.onclick=()=>{const user=state.chatMessages.find(m=>m.turn_id===message.turn_id&&m.role==='user');if(user)ask(user.content,message.id);};actions.append(retry);
    }
    body.append(actions);
  }
  article.append(avatar,body);return article;
}

async function showChatReference(ref) {
  const dialog=$('#viewerDialog'),body=$('#viewerBody');
  $('#viewerTitle').textContent='['+ref.index+'] '+ref.knowledge_title;
  body.replaceChildren();
  const meta=document.createElement('p');meta.className='chunk-meta';meta.textContent=[ref.heading_path,ref.page_number?'第 '+ref.page_number+' 页':''].filter(Boolean).join(' · ');
  const excerpt=document.createElement('pre');excerpt.className='chunk-content';excerpt.textContent=ref.content||'无片段快照';
  const availability=document.createElement('p');availability.className='muted';availability.textContent='正在核对来源…';
  body.append(meta,excerpt,availability);if(!dialog.open)dialog.showModal();
  const epoch=state.chatEpoch;
  try {
    const chunks=await request('/knowledge/'+encodeURIComponent(ref.knowledge_id)+'/chunks');
    if(epoch!==state.chatEpoch||!body.contains(availability))return;
    const valid=chunks.some(chunk=>chunk.id===ref.chunk_id);
    availability.textContent=valid?'以上为回答时的证据片段。':'来源已重新处理或该片段已移除；以上保留回答时的快照。';
    if(valid&&ref.wiki_slug&&ref.knowledge_base_id===state.activeId){const button=document.createElement('button');button.className='button';button.textContent='阅读关联 Wiki';button.onclick=()=>{dialog.close();switchView('wiki');openWikiSlug(ref.wiki_slug);};body.append(button);}
  }catch{availability.textContent='原始来源已删除或暂不可访问；以上保留回答时的快照。';}
}

async function ask(question,retryId=null) {
  if(state.chatRun||state.chatLoading)return;
  const epoch=state.chatEpoch,controller=new AbortController(),run={controller};state.chatRun=run;updateChatControls();
  let answer;
  try {
    if(!state.sessionId) {
      const ids=[...$('#scopeDocuments').querySelectorAll('input:checked')].map(input=>input.value);
      const session=await request('/sessions',{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify({knowledge_base_id:state.activeId,knowledge_ids:ids})});
      if(epoch!==state.chatEpoch)return;
      state.sessions.unshift(session);state.sessionId=session.id;localStorage.setItem('wiki-session:'+state.activeId,session.id);renderSessions();renderChatScope();
    }
    const session=activeSession();
    if(!session?.knowledge_base_id)throw Error('旧会话仅供查看，请新建会话');
    const response=await fetch(API+'/chat',{method:'POST',headers:{'Content-Type':'application/json'},signal:controller.signal,body:JSON.stringify({session_id:session.id,query:question,knowledge_base_ids:[session.knowledge_base_id],knowledge_ids:session.knowledge_ids,retry_message_id:retryId})});
    if(!response.ok){const payload=await response.json();throw Error(payload.error?.message||'问答请求失败');}
    if(epoch!==state.chatEpoch)return;
    if(!response.body)throw Error('浏览器未提供流式响应');
    $('#question').value='';
    if(retryId){answer=state.chatMessages.find(m=>m.id===retryId);answer.content='';answer.references_json='[]';answer.status='generating';}
    else {state.chatMessages.push({role:'user',content:question,status:'completed'});answer={role:'assistant',content:'',status:'generating'};state.chatMessages.push(answer);}
    renderChat();
    const reader=response.body.getReader(),decoder=new TextDecoder();let buffer='',terminal=false;
    const consume=block=>{
      const lines=block.split('\n'),type=lines.find(line=>line.startsWith('event:'))?.slice(6).trim();
      const raw=lines.filter(line=>line.startsWith('data:')).map(line=>line.slice(5).trimStart()).join('\n');if(!raw)return;
      const data=JSON.parse(raw);
      if(type==='start'){answer.id=data.message_id;answer.turn_id=data.turn_id;const user=state.chatMessages.at(-2);if(user?.role==='user')user.turn_id=data.turn_id;}
      if(type==='references')answer.references_json=JSON.stringify(data.references);
      if(type==='answer')answer.content+=data.delta;
      if(type==='done'||type==='error'){terminal=true;answer.status=data.status||(type==='error'?'failed':'completed');if(type==='error')toast(data.message,true);}
    };
    while(true){const {value,done}=await reader.read();if(epoch!==state.chatEpoch){await reader.cancel();return;}
      buffer+=decoder.decode(value||new Uint8Array(),{stream:!done});buffer=buffer.replace(/\r\n/g,'\n');
      let separator;while((separator=buffer.indexOf('\n\n'))!==-1){consume(buffer.slice(0,separator));buffer=buffer.slice(separator+2);}
      if(done){if(buffer.trim())consume(buffer);break;}renderChat();
    }
    if(!terminal)throw Error('连接已中断，正在恢复已保存的回答');
  } catch(error) {
    if(epoch===state.chatEpoch){if(answer&&answer.status==='generating')answer.status=error.name==='AbortError'?'stopped':'failed';if(error.name!=='AbortError')toast(error.message,true);}
  } finally {
    if(epoch===state.chatEpoch&&state.chatRun===run){state.chatRun=null;renderChat();await loadSessions();if(state.sessionId)await loadChatHistory();}
  }
}

async function stopAnswer() {
  if(!state.sessionId)return;
  const sid=state.sessionId;
  try { await request('/sessions/'+encodeURIComponent(sid)+'/stop',{method:'POST'});if(sid===state.sessionId)$('#chatStatus').textContent='正在停止并保存回答…'; }
  catch(error){toast('停止失败：'+error.message,true);}
}

async function clearChat() {
  if(!state.sessionId||!confirm('清空当前会话的全部消息？'))return;
  const sid=state.sessionId,epoch=state.chatEpoch;
  try{await request('/sessions/'+encodeURIComponent(sid)+'/messages',{method:'DELETE'});if(epoch===state.chatEpoch)await loadChatHistory();}catch(error){toast(error.message,true);}
}

async function renameSession() {
  const session=activeSession();if(!session)return;const title=prompt('会话名称',session.title)?.trim();if(!title)return;
  try{await request('/sessions/'+encodeURIComponent(session.id),{method:'PATCH',headers:{'Content-Type':'application/json'},body:JSON.stringify({title})});await loadSessions();}catch(error){toast(error.message,true);}
}

async function deleteSession() {
  if(!state.sessionId||!confirm('删除当前会话及全部消息？'))return;
  const sid=state.sessionId,epoch=state.chatEpoch;
  try{await request('/sessions/'+encodeURIComponent(sid),{method:'DELETE'});if(epoch===state.chatEpoch){resetChat();await loadSessions();}}catch(error){toast(error.message,true);}
}

$('#newKbBtn').onclick=()=>$('#kbDialog').showModal();
$('#addDocumentBtn').onclick=()=>$('#documentDialog').showModal();
$('#clearKbBtn').onclick=clearKnowledgeBase;
$('#clearChatBtn').onclick=clearChat;
$('#newSessionBtn').onclick=resetChat;
$('#renameSessionBtn').onclick=renameSession;
$('#deleteSessionBtn').onclick=deleteSession;
$('#stopAnswerBtn').onclick=stopAnswer;
$('#olderMessagesBtn').onclick=()=>loadChatHistory(true);
document.querySelectorAll('.view-tabs button').forEach(button=>button.onclick=()=>switchView(button.dataset.view));
$('#newWikiPageBtn').onclick=()=>openWikiEditor();
document.querySelectorAll('[data-wiki-mode]').forEach(button=>button.onclick=()=>setWikiMode(button.dataset.wikiMode));
let wikiSearchTimer; $('#wikiSearch').oninput=event=>{ clearTimeout(wikiSearchTimer); wikiSearchTimer=setTimeout(()=>loadWikiPages(event.target.value.trim()),250); };
document.querySelectorAll('[data-close-dialog]').forEach(button=>button.onclick=()=>button.closest('dialog').close());
document.querySelectorAll('.tab').forEach(tab=>tab.onclick=()=>{ state.tab=tab.dataset.tab; document.querySelectorAll('.tab,.tab-content').forEach(x=>x.classList.remove('active')); tab.classList.add('active'); $(`#${state.tab}Tab`).classList.add('active'); });

$('#kbForm').onsubmit=async event=>{ event.preventDefault(); const form=event.currentTarget;
  await withSubmitLock(form, async()=>{ const data=Object.fromEntries(new FormData(form));
    try { const created=await request('/knowledge-bases',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)}); form.reset(); $('#kbDialog').close(); await loadBases(); await selectBase(created.id); toast('知识库已创建'); }
    catch(error){ toast(error.message,true); }
  });
};

$('#documentForm').onsubmit=async event=>{ event.preventDefault();
  const form=event.currentTarget;
  await withSubmitLock(form, async()=>{
    try { let options;
      if(state.tab==='file'){ const file=form.elements.file.files[0]; if(!file)throw new Error('请选择文件'); const data=new FormData(); data.append('file',file); options={method:'POST',body:data}; }
      else { const title=form.elements.title.value.trim(), content=form.elements.content.value.trim(); if(!title||!content)throw new Error('请填写标题和正文'); options={method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({title,content})}; }
      await request(`/knowledge-bases/${state.activeId}/knowledge/${state.tab}`,options); form.reset(); $('#documentDialog').close(); toast('资料已进入处理队列'); await loadDocuments(); setTimeout(loadDocuments,1200);
    } catch(error){ toast(error.message,true); }
  });
};

$('#wikiEditorForm').onsubmit=async event=>{ event.preventDefault(); const form=event.currentTarget;
  await withSubmitLock(form,async()=>{ try { const raw=Object.fromEntries(new FormData(form)); const pageId=raw.page_id; delete raw.page_id; raw.page_type=state.activeWikiPage?.page_type==='entity'?'entity':state.activeWikiPage?.page_type==='concept'?'concept':'topic'; raw.status='published'; raw.folder_id=null;
    const page=await request(`/knowledge-bases/${state.activeId}/wiki/pages${pageId?`/${pageId}`:''}`,{method:pageId?'PUT':'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(raw)});
    $('#wikiEditorDialog').close(); await loadWikiPages(); await showWikiPage(page); toast('Wiki 页面已保存');
  } catch(error){ toast(error.message,true); } });
};

$('#chatForm').onsubmit=event=>{ event.preventDefault(); const input=$('#question'), question=input.value.trim(); if(!question||!state.activeId||state.chatRun||state.chatLoading)return; ask(question); };
$('#question').onkeydown=event=>{ if(event.key==='Enter'&&!event.shiftKey&&!event.isComposing){ event.preventDefault(); $('#chatForm').requestSubmit(); } };

switchView(initialView); checkHealth(); loadBases(); setInterval(async()=>{
  if(state.activeId && state.documents.some(x=>['pending','processing'].includes(x.parse_status))) {
    await loadDocuments();
    if(state.view==='wiki') { loadWikiProgress(); loadWikiPages(); }
  }
},2500);
