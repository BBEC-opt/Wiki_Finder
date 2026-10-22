const {test} = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

// 用可控的异步响应模拟慢网络，不访问服务端或模型。
function workspace() {
  const elements = new Map(), pending = [], listeners = {};
  const element = () => ({
    value: '', dataset: {}, children: [], attributes: {}, html: '',
    classList: {toggle() {}, add() {}},
    get innerHTML() { return this.html; },
    set innerHTML(value) { this.html = value; this.children = []; },
    setAttribute(name, value) { this.attributes[name] = value; },
    append(...children) { this.children.push(...children); },
    replaceChildren(...children) { this.children = children; },
    contains(child) { return this.children.includes(child); },
    showModal() { this.open = true; }, close() { this.open = false; },
    querySelector() { return element(); }, querySelectorAll() { return []; },
    insertAdjacentHTML(_, value) { this.html += value; },
  });
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  const document = {hidden: false, querySelector: get, querySelectorAll: () => [],
    createElement: element, createTextNode: textContent => ({textContent}), addEventListener: (name, fn) => { listeners[name] = fn; }};
  const context = vm.createContext({document, window: {}, location: {search: ''}, URLSearchParams,
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: url => new Promise((resolve, reject) => pending.push({url, reject,
      resolve: data => resolve({ok: true, json: async () => ({data})}),
      respond: (status, message) => resolve({ok: false, status, json: async () => ({error: {message}})})})),
  });
  const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8')
    .replace('switchView(initialView); checkHealth(); loadBases();', '');
  vm.runInContext(source, context);
  const evaluate = code => vm.runInContext(code, context);
  evaluate("state.activeId='kb'; renderMarkdown=()=>document.createElement('article');");
  return {get, pending, document, listeners, evaluate};
}
const page = id => ({id, title: id, page_type: 'topic', status: 'published', content: '', slug: id});

test('等待中和处理中资料禁止重新处理', async () => {
  const w = workspace();
  for (const status of ['pending', 'processing']) {
    w.evaluate(`state.documents=[{id:'a',parse_status:'${status}'}]; renderDocuments();`);
    assert.match(w.get('#documentList').innerHTML, /data-retry="a"[^>]*disabled/);
    await w.evaluate("reprocess('a')");
  }
  assert.equal(w.pending.length, 0);
});

test('重新处理锁跨列表重绘生效，失败后可再次提交', async () => {
  const w = workspace();
  w.evaluate("state.documents=[{id:'a',parse_status:'failed'}];");
  const first = w.evaluate("reprocess('a')");
  w.evaluate('renderDocuments()');
  assert.match(w.get('#documentList').innerHTML, /data-retry="a"[^>]*disabled/);
  await w.evaluate("reprocess('a')");
  assert.equal(w.pending.length, 1);
  w.pending[0].reject(new Error('提交失败')); await first;
  assert.equal(w.get('#toast').textContent, '提交失败');
  assert.doesNotMatch(w.get('#documentList').innerHTML, /data-retry="a"[^>]*disabled/);
  const retry = w.evaluate("reprocess('a')");
  assert.equal(w.pending.length, 2);
  w.pending[1].reject(new Error('断网')); await retry;
});

test('重新处理成功但刷新失败时仍保留等待状态', async () => {
  const w = workspace();
  w.evaluate("state.documents=[{id:'a',parse_status:'completed'}];");
  const submitting = w.evaluate("reprocess('a')");
  w.pending[0].resolve({id: 'a', parse_status: 'pending'});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(w.pending.length, 2);
  w.pending[1].reject(new Error('刷新失败')); await submitting;
  assert.equal(w.evaluate('state.documents[0].parse_status'), 'pending');
  assert.match(w.get('#documentList').innerHTML, /data-retry="a"[^>]*disabled/);
  await w.evaluate("reprocess('a')");
  assert.equal(w.pending.length, 2);
});

test('目录忽略乱序响应，后台刷新保留筛选与已有内容', async () => {
  const w = workspace();
  const older = w.evaluate("loadWikiPages('旧')");
  const newer = w.evaluate("loadWikiPages('新')");
  w.pending[1].resolve([page('new')]); await newer;
  w.pending[0].resolve([page('old')]); await older;
  assert.equal(w.evaluate('state.wikiPages[0].id'), 'new');
  w.get('#wikiSearch').value = '新';
  const children = w.get('#wikiPageList').children;
  const refresh = w.evaluate('loadWikiPages()');
  assert.match(w.pending[2].url, /q=%E6%96%B0$/);
  assert.equal(w.get('#wikiPageList').children, children);
  w.pending[2].reject(new Error('断网')); await refresh;
  assert.equal(w.get('#wikiPageList').children, children);
  assert.match(w.get('#toast').textContent, /目录刷新失败/);
  assert.equal(w.get('#wikiPageList').attributes['aria-busy'], 'false');
});

test('切换知识库后旧目录响应不能写入', async () => {
  const w = workspace();
  const loading = w.evaluate('loadWikiPages()');
  w.evaluate("state.activeId='other'; state.wikiPages=[];");
  w.pending[0].resolve([page('old')]); await loading;
  assert.equal(w.evaluate('state.wikiPages.length'), 0);
});

test('快速切页时来源面板忽略旧页面成功和失败响应', async () => {
  for (const failed of [false, true]) {
    const w = workspace();
    w.evaluate('globalThis.firstPage='+JSON.stringify(page('first')));
    w.evaluate('globalThis.secondPage='+JSON.stringify(page('second')));
    const first = w.evaluate('showWikiPage(firstPage)');
    const second = w.evaluate('showWikiPage(secondPage)');
    w.pending[2].resolve([]); w.pending[3].resolve([]); await second;
    const expected = w.get('#wikiEvidence').innerHTML;
    if (failed) w.pending[0].reject(new Error('旧页面错误'));
    else w.pending[0].resolve([{knowledge_title: '旧来源'}]);
    w.pending[1].resolve([]); await first;
    assert.equal(w.get('#wikiEvidence').innerHTML, expected);
    assert.equal(w.get('#wikiEvidence').children.length, 0);
  }
});

test('轮询不重叠，隐藏时暂停，恢复可见时刷新', async () => {
  const w = workspace();
  w.evaluate("state.documents=[{parse_status:'processing'}]; renderDocuments=()=>{}; renderChatScope=()=>{};");
  const first = w.evaluate('refreshWorkspace()');
  await w.evaluate('refreshWorkspace()');
  assert.equal(w.pending.length, 1);
  w.pending[0].resolve([]); await first;
  w.document.hidden = true;
  await w.evaluate('refreshWorkspace(true)');
  assert.equal(w.pending.length, 1);
  w.document.hidden = false;
  w.listeners.visibilitychange();
  assert.equal(w.pending.length, 2);
  w.pending[1].resolve([]);
});

test('处理进度忽略切换资料前的响应', async () => {
  const w = workspace();
  w.evaluate("state.documents=[{id:'a'},{id:'b'}]; renderWikiProgress=doc=>{globalThis.shown=doc.id;};");
  const first = w.evaluate('loadWikiProgress()');
  w.evaluate("state.progressDocumentId='b';");
  const second = w.evaluate('loadWikiProgress()');
  w.pending[2].resolve([]); w.pending[3].resolve([]); await second;
  w.pending[0].resolve([]); w.pending[1].resolve([]); await first;
  assert.equal(w.evaluate('shown'), 'b');
});

test('旧内链导航不得覆盖之后点击的页面', async () => {
  const w = workspace();
  const old = w.evaluate("openWikiSlug('old')");
  w.evaluate('globalThis.selected='+JSON.stringify(page('selected')));
  const selected = w.evaluate('showWikiPage(selected)');
  w.pending[1].resolve([]); w.pending[2].resolve([]); await selected;
  w.pending[0].resolve(page('old')); await old;
  assert.equal(w.evaluate('state.activeWikiPage.id'), 'selected');
});


const reference = {index: 1, knowledge_id: 'doc', chunk_id: 'chunk', knowledge_base_id: 'kb',
  knowledge_title: '设备指南', heading_path: '使用', page_number: 3, content: '命中片段',
  evidence_content: '模型实际读取的上下文 <script>示例</script>'};
const sourceChunk = {id: 'chunk', chunk_index: 2, content: '命中片段', parent_content: '前文命中片段后文', heading_path: '使用', page_number: 3};
function openReference(w, ref=reference) {
  return w.evaluate('showChatReference('+JSON.stringify(ref)+')');
}

test('引用显示实际证据快照，展开原文时高亮命中片段', async () => {
  const w = workspace(), opening = openReference(w);
  assert.equal(w.get('#viewerTitle').textContent, '[1] 设备指南');
  assert.match(w.get('#viewerBody').children[0].textContent, /使用 · 第 3 页/);
  assert.equal(w.get('#viewerBody').children[2].textContent, reference.evidence_content);
  w.pending[0].resolve([sourceChunk]); await opening;
  const details = w.get('#viewerBody').children[4];
  assert.equal(details.children[0].textContent, '查看原文上下文');
  assert.match(details.children[1].textContent, /原文分片 3/);
  assert.deepEqual(details.children[2].children.map(node => node.textContent), ['前文', '命中片段', '后文']);
});

test('旧引用回退子块快照，重处理后不展示当前无关来源', async () => {
  const w = workspace(), opening = openReference(w, {...reference, evidence_content: undefined});
  w.pending[0].resolve([{...sourceChunk, id: 'new-chunk'}]); await opening;
  const body = w.get('#viewerBody');
  assert.equal(body.children[2].textContent, reference.content);
  assert.match(body.children[3].textContent, /重新处理/);
  assert.equal(body.children.length, 4);
});

test('删除来源与网络失败分别提示，快照始终保留', async () => {
  for (const status of [404, 503]) {
    const w = workspace(), opening = openReference(w);
    w.pending[0].respond(status, '请求失败'); await opening;
    assert.match(w.get('#viewerBody').children[3].textContent, status === 404 ? /已删除/ : /暂时无法核对/);
    assert.equal(w.get('#viewerBody').children[2].textContent, reference.evidence_content);
  }
});

test('切换引用、会话或关闭弹窗后忽略旧来源响应', async () => {
  for (const action of ['reference', 'session', 'close']) {
    for (const failed of [false, true]) {
      const w = workspace(), first = openReference(w);
      const oldStatus = w.get('#viewerBody').children[3];
      let second;
      if (action === 'reference') second = openReference(w, {...reference, index: 2});
      if (action === 'session') w.evaluate('state.chatEpoch++');
      if (action === 'close') w.get('#viewerDialog').close();
      if (failed) w.pending[0].reject(new Error('断网'));
      else w.pending[0].resolve([sourceChunk]);
      await first;
      assert.equal(oldStatus.textContent, '正在核对来源…');
      assert.equal(w.get('#viewerBody').children.length, 4);
      if (second) { w.pending[1].resolve([sourceChunk]); await second; }
    }
  }
});

test('缺少来源标识的历史引用只显示快照', async () => {
  const w = workspace();
  await openReference(w, {...reference, chunk_id: undefined});
  assert.equal(w.pending.length, 0);
  assert.match(w.get('#viewerBody').children[3].textContent, /缺少来源标识/);
});
