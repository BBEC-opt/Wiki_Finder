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
    querySelector() { return element(); }, querySelectorAll() { return []; },
    insertAdjacentHTML(_, value) { this.html += value; },
  });
  const get = selector => {
    if (!elements.has(selector)) elements.set(selector, element());
    return elements.get(selector);
  };
  const document = {hidden: false, querySelector: get, querySelectorAll: () => [],
    createElement: element, addEventListener: (name, fn) => { listeners[name] = fn; }};
  const context = vm.createContext({document, window: {}, location: {search: ''}, URLSearchParams,
    setTimeout: () => 1, clearTimeout() {}, setInterval() {},
    fetch: url => new Promise((resolve, reject) => pending.push({url, reject,
      resolve: data => resolve({ok: true, json: async () => ({data})})})),
  });
  const source = fs.readFileSync(path.join(__dirname, '../frontend/app.js'), 'utf8')
    .replace('switchView(initialView); checkHealth(); loadBases();', '');
  vm.runInContext(source, context);
  const evaluate = code => vm.runInContext(code, context);
  evaluate("state.activeId='kb'; renderMarkdown=()=>document.createElement('article');");
  return {get, pending, document, listeners, evaluate};
}
const page = id => ({id, title: id, page_type: 'topic', status: 'published', content: '', slug: id});

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
