(() => {
  const rows = Array.from(document.querySelectorAll('[data-feedback]'));
  // 选中某个按钮时，只显示 data-for 里写了这个按钮的输入：备注跟「升」「判错了」，范围和原因跟「不用盯」。
  const extras = row => Array.from(row.querySelectorAll('[data-for]'));
  const collect = () => rows.flatMap(row => {
    const button = row.querySelector('[aria-pressed="true"]');
    if (!button) return [];
    const choice = button.textContent;
    const drop = row.querySelector('.drop');
    if (choice === '不用盯' && drop) {
      const scope = Array.from(drop.querySelectorAll('input[type="radio"]')).find(radio => radio.checked);
      return [{id: row.dataset.feedback, choice, note: drop.querySelector('.reason').value.trim(),
               scope: scope.value, scopeLabel: scope.dataset.label, label: row.dataset.label}];
    }
    const input = row.querySelector('.note');
    const note = input && !input.hidden ? input.value.trim() : '';
    return [{id: row.dataset.feedback, choice, note, label: row.dataset.label}];
  });
  const update = () => {
    const count = collect().length;
    document.getElementById('count').textContent = count ? `已选 ${count} 项，还剩 ${rows.length-count} 项` : '还没选';
  };
  rows.forEach(row => {
    row.querySelectorAll('button').forEach(button => button.addEventListener('click', () => {
      const selected = button.getAttribute('aria-pressed') === 'true';
      row.querySelectorAll('button').forEach(other => other.setAttribute('aria-pressed', 'false'));
      button.setAttribute('aria-pressed', String(!selected));
      extras(row).forEach(extra => {
        extra.hidden = selected || !extra.dataset.for.split(' ').includes(button.textContent);
      });
      update();
    }));
  });
  const line = x => {
    if (x.scope) return `- ${x.label}：${x.choice}（范围：${x.scopeLabel}${x.note ? `；原因：${x.note}` : ''}）`;
    return `- ${x.label}：${x.choice}${x.note ? `（${x.note}）` : ''}`;
  };
  document.getElementById('copy').addEventListener('click', async () => {
    const chosen = collect(), left = rows.length-chosen.length;
    const report = document.body.dataset.report;
    const lines = chosen.map(line);
    const payload = {report, items: chosen.map(({id, choice, note, scope}) => scope ? {id, choice, note, scope} : {id, choice, note})};
    const text = `# 盯更新反馈 ${report}\n${lines.join('\n')}${left ? `\n还有 ${left} 项没选，下周接着问。` : ''}\n\n\`\`\`json\n${JSON.stringify(payload)}\n\`\`\`\n`;
    const out = document.getElementById('out'), textarea = document.getElementById('outText');
    textarea.value = text; out.style.display = 'block';
    try {
      await navigator.clipboard.writeText(text);
      document.getElementById('msg').textContent = '已复制，去飞书贴给 Ash，或贴给项目经理。';
    } catch (_) {
      textarea.focus(); textarea.select();
      document.getElementById('msg').textContent = '上面的文字已选中，请手动复制。';
    }
    out.scrollIntoView({block:'end'});
  });
})();
