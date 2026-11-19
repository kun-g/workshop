// 跑报告页脚本用的最小 DOM：只支持 report.js 用到的选择器和属性，不联网、不开浏览器。
// 用法：node fake_dom.js <页面树 JSON> <report.js> <操作 JSON>，stdout 输出每次 snapshot 和复制的文字。
const fs = require('fs');
const [tree, script, steps] = process.argv.slice(2);

class El {
  constructor(node) {
    this.tag = node.tag; this.attrs = {...node.attrs}; this.text = node.text || '';
    this.children = (node.children || []).map(child => new El(child));
    this.listeners = {}; this.style = {};
    this._value = this.attrs.value || ''; this.checked = 'checked' in this.attrs;
  }
  get hidden() { return 'hidden' in this.attrs; }
  set hidden(on) { if (on) this.attrs.hidden = ''; else delete this.attrs.hidden; }
  get dataset() {
    return Object.fromEntries(Object.entries(this.attrs).filter(([k]) => k.startsWith('data-')).map(([k, v]) => [k.slice(5), v]));
  }
  get value() { return this._value; }
  set value(v) { this._value = v; }
  get textContent() { return this.text + this.children.map(c => c.textContent).join(''); }
  set textContent(v) { this.text = v; this.children = []; }
  getAttribute(k) { return k in this.attrs ? this.attrs[k] : null; }
  setAttribute(k, v) { this.attrs[k] = String(v); }
  addEventListener(type, fn) { (this.listeners[type] = this.listeners[type] || []).push(fn); }
  click() { return Promise.all((this.listeners.click || []).map(fn => fn())); }
  matches(selector) {
    const m = selector.match(/^([a-z]+)?(?:\.([\w-]+))?(?:\[([\w-]+)(?:="([^"]*)")?\])?$/);
    if (!m) throw new Error('fake DOM 不支持的选择器：' + selector);
    const [, tag, cls, attr, value] = m;
    return (!tag || this.tag === tag) && (!cls || (this.attrs.class || '').split(/\s+/).includes(cls))
      && (!attr || (attr in this.attrs && (value === undefined || this.attrs[attr] === value)));
  }
  descendants() { return this.children.flatMap(c => [c, ...c.descendants()]); }
  querySelectorAll(selector) { return this.descendants().filter(e => e.matches(selector)); }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  focus() {} select() {} scrollIntoView() {}
}

const root = new El(JSON.parse(fs.readFileSync(tree, 'utf8')));
const all = () => [root, ...root.descendants()];
let copied = null;
global.document = {
  body: all().find(e => e.tag === 'body'),
  querySelectorAll: s => root.querySelectorAll(s), querySelector: s => root.querySelector(s),
  getElementById: id => all().find(e => e.attrs.id === id) || null,
};
// Node 21 起自带只读的 navigator，得用 defineProperty 换掉。
Object.defineProperty(globalThis, 'navigator', {value: {clipboard: {writeText: async text => { copied = text; }}}, configurable: true});
eval(fs.readFileSync(script, 'utf8'));

const row = id => document.querySelectorAll('[data-feedback]').find(r => r.dataset.feedback === id);
(async () => {
  const output = [];
  for (const [action, id, ...rest] of JSON.parse(fs.readFileSync(steps, 'utf8'))) {
    if (action === 'click') await row(id).querySelectorAll('button').find(b => b.textContent === rest[0]).click();
    if (action === 'radio') row(id).querySelectorAll('input[type="radio"]').forEach(r => { r.checked = r.value === rest[0]; });
    if (action === 'type') row(id).querySelector(rest[0]).value = rest[1];
    if (action === 'snapshot') {
      const r = row(id), drop = r.querySelector('.drop'), note = r.querySelector('.note');
      output.push({drop: drop ? !drop.hidden : null, note: note ? !note.hidden : null,
                   scopes: drop ? drop.querySelectorAll('input[type="radio"]').map(x => [x.value, x.checked]) : []});
    }
    if (action === 'copy') { await document.getElementById('copy').click(); output.push(copied); }
  }
  process.stdout.write(JSON.stringify(output));
})();
