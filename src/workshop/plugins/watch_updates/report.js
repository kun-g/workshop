(() => {
  const rows = Array.from(document.querySelectorAll('[data-feedback]'));
  const collect = () => rows.flatMap(row => {
    const button = row.querySelector('[aria-pressed="true"]');
    if (!button) return [];
    const input = row.querySelector('input');
    const note = input && !input.hidden ? input.value.trim() : '';
    return [{id: row.dataset.feedback, choice: button.textContent, note, label: row.dataset.label}];
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
      const input = row.querySelector('input');
      if (input) input.hidden = selected || !['升', '判错了'].includes(button.textContent);
      update();
    }));
  });
  document.getElementById('copy').addEventListener('click', async () => {
    const chosen = collect(), left = rows.length-chosen.length;
    const report = document.body.dataset.report;
    const lines = chosen.map(x => `- ${x.label}：${x.choice}${x.note ? `（${x.note}）` : ''}`);
    const payload = {report, items: chosen.map(({id, choice, note}) => ({id, choice, note}))};
    const text = `# 盯更新反馈 ${report}\n${lines.join('\n')}${left ? `\n还有 ${left} 项没选，下周接着问。` : ''}\n\n\`\`\`json\n${JSON.stringify(payload)}\n\`\`\`\n`;
    const out = document.getElementById('out'), textarea = document.getElementById('outText');
    textarea.value = text; out.style.display = 'block';
    try {
      await navigator.clipboard.writeText(text);
      document.getElementById('msg').textContent = '已复制，去飞书贴给 Ash，或贴给负责会话。';
    } catch (_) {
      textarea.focus(); textarea.select();
      document.getElementById('msg').textContent = '上面的文字已选中，请手动复制。';
    }
    out.scrollIntoView({block:'end'});
  });
})();
