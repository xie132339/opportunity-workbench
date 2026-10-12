(() => {
  'use strict';
  const initial = JSON.parse(document.getElementById('basket-initial').textContent);
  const key = 'festival-basket-v1';
  const localTime = () => new Date(Date.now() + 8 * 3600000).toISOString().slice(0, 16);
  const empty = () => ({items: [], rules: [], stack_pairs: [], at: localTime(), example: false});
  const valid = x => x && typeof x === 'object' && Array.isArray(x.items) && Array.isArray(x.rules) && Array.isArray(x.stack_pairs) && x.items.length <= 8 && x.rules.length <= 12 && x.items.every(i => i && typeof i === 'object') && x.rules.every(r => r && typeof r === 'object' && typeof r.kind === 'string' && typeof r.id === 'string');
  let state = initial.payload;
  if (!valid(state)) {
    try { state = JSON.parse(localStorage.getItem(key)); } catch (_) { state = null; }
  }
  if (!valid(state)) state = empty();
  const note = document.getElementById('basket-draft-note');
  function save(changed = true) {
    if (changed) document.querySelectorAll('.calc-result').forEach(e => { e.hidden = true; });
    try { localStorage.setItem(key, JSON.stringify(state)); note.textContent = '草稿保存在本浏览器；修改后请重新计算。运费未知时请先核对，包邮填 0。'; }
    catch (_) { note.textContent = '浏览器未保存草稿；当前页仍可计算。'; }
  }
  function uid(prefix, rows) { let n = 1; while (rows.some(r => r.id === prefix + n)) n++; return prefix + n; }
  const addItem = () => ({id: uid('P', state.items), title: '', shop: '', price: '', quantity: 1, shipping: ''});
  if (initial.imported) {
    if (state.example) state = empty();
    state.items = state.items.filter(i => i.title || i.price || i.shop || i.shipping);
    const found = state.items.findIndex(i => i.id === initial.imported.item.id);
    if (found >= 0) state.items[found] = initial.imported.item;
    else if (state.items.length < 8) state.items.push(initial.imported.item);
    if (state.items.some(i => i.id === initial.imported.item.id)) {
      initial.imported.rules.forEach(rule => {
        const old = state.rules.findIndex(r => r.id === rule.id);
        if (old >= 0) state.rules[old] = rule;
        else if (state.rules.length < 12) state.rules.push(rule);
      });
      state.at = localTime();
    } else note.textContent = '清单已满 8 种，请先移除一项再导入。';
  }
  if (!state.items.length) state.items.push(addItem());
  const labels = {item_coupon: '单品券', shop_coupon: '店铺满减券', shop_discount: '店铺多件折扣', platform_coupon: '平台券', red_packet: '2026双11超级红包', official_path: '官方完整单件路径'};
  function el(tag, text, parent) { const e = document.createElement(tag); if (text != null) e.textContent = text; if (parent) parent.append(e); return e; }
  function field(parent, row, name, label, type = 'text', options = null, required = true) {
    const wrapper = el('label', label, parent);
    const input = el(options ? 'select' : 'input', null, wrapper);
    if (options) options.forEach(([value, title]) => { const opt = el('option', title, input); opt.value = value; });
    else { input.type = type; if (type === 'datetime-local') input.step = '1'; if (type === 'number') { input.min = '0'; input.step = name.includes('quantity') ? '1' : '0.01'; } }
    input.value = row[name] == null ? '' : row[name]; input.required = required;
    input.addEventListener('change', () => { row[name] = input.value; save(); if (name === 'kind') { if (row.kind === 'red_packet') { row.mode = 'minus'; row.basis = 'current'; row.source = 'https://1111.alimama.com/alimamamain/page_20211227_144813'; } render(); } if (['name','id','title'].includes(name)) renderPairs(); });
    input.addEventListener('input', () => {
      if (name === 'id' && row[name] !== input.value) state.stack_pairs = state.stack_pairs.filter(pair => !pair.includes(row[name]));
      row[name] = input.value; save();
    });
    return input;
  }
  function check(parent, text, checked, callback) {
    const label = el('label', null, parent); label.className = 'calc-check';
    const box = el('input', null, label); box.type = 'checkbox'; box.checked = checked;
    el('span', text, label); box.addEventListener('change', () => { callback(box.checked); save(); });
  }
  function removeButton(parent, callback) { const button = el('button', '移除', parent); button.type = 'button'; button.className = 'secondary'; button.onclick = callback; }
  function renderPairs() {
    const parent = document.getElementById('basket-pairs'); parent.replaceChildren();
    for (let a = 0; a < state.rules.length; a++) for (let b = a + 1; b < state.rules.length; b++) {
      const left = state.rules[a], right = state.rules[b];
      if (left.kind === 'red_packet' || right.kind === 'red_packet' || left.kind === 'official_path' || right.kind === 'official_path' || (left.kind.startsWith('shop_') && right.kind.startsWith('shop_'))) continue;
      const match = pair => pair.includes(left.id) && pair.includes(right.id);
      check(parent, `${left.name || left.id} + ${right.name || right.id}：规则明确允许叠加`, state.stack_pairs.some(match), enabled => {
        state.stack_pairs = state.stack_pairs.filter(p => !match(p));
        if (enabled) state.stack_pairs.push([left.id, right.id]);
      });
    }
  }
  function render() {
    const items = document.getElementById('basket-items'); items.replaceChildren();
    state.items.forEach(item => {
      const row = el('div', null, items); row.className = 'calc-row';
      const fields = el('div', null, row); fields.className = 'calc-fields';
      field(fields, item, 'title', `商品 ${item.id}`); field(fields, item, 'price', '当前单价（元）', 'number');
      field(fields, item, 'quantity', '购买数量', 'number'); field(fields, item, 'shop', '同店子订单组（同组填一样）');
      field(fields, item, 'shipping', '该子订单运费（元，只计一次）', 'number');
      if (item.source) el('small', `已导入官方报价，采集于 ${item.observed_at || '未知'}；请核对所选规格、订单分组与运费。`, row);
      removeButton(row, () => { state.items = state.items.filter(i => i !== item); state.rules.forEach(r => { r.items = (r.items || []).filter(id => id !== item.id); }); save(); render(); });
    });
    const rules = document.getElementById('basket-rules'); rules.replaceChildren();
    state.rules.forEach(rule => {
      const row = el('div', null, rules); row.className = 'calc-row';
      const fields = el('div', null, row); fields.className = 'calc-fields';
      field(fields, rule, 'name', `优惠 ${rule.id}`); field(fields, rule, 'kind', '类型', 'text', Object.entries(labels));
      field(fields, rule, 'mode', '计算方式', 'text', [['minus', '满额减一次'], ['repeat', '每满额减一次'], ['percent', '折后百分比']]);
      field(fields, rule, 'threshold', '满多少元可用（无门槛填0）', 'number');
      field(fields, rule, 'value', '减多少元 / 折后百分比（9折填90）', 'number');
      const scope = el('div', null, row); el('small', '适用商品（只计入已选商品的金额和件数）：', scope);
      state.items.forEach(item => check(scope, item.title || item.id, (rule.items || []).includes(item.id), enabled => {
        rule.items = (rule.items || []).filter(id => id !== item.id); if (enabled) rule.items.push(item.id);
      }));
      const details = el('details', null, row); el('summary', '有效期、使用条件与规则来源（必填）', details);
      const extra = el('div', null, details); extra.className = 'calc-fields';
      field(extra, rule, 'id', '券编号（同一张券用同一编号）');
      field(extra, rule, 'start', '开始（北京时间）', 'datetime-local', null, false); field(extra, rule, 'end', '结束（北京时间，不含此刻）', 'datetime-local', null, false);
      field(extra, rule, 'min_quantity', '最低件数', 'number'); field(extra, rule, 'basis', '门槛按什么金额', 'text', [['original','本次输入单价 × 数量'],['current','此前优惠扣减后的金额']]);
      field(extra, rule, 'cap', '最多减多少元（无封顶留空）', 'number', null, false);
      field(extra, rule, 'source', '商品/券或规则页面链接', 'url', null, false);
      check(row, '已核对：我可用、适用所选商品，输入价格尚未扣除本优惠；红包已实际领取。', rule.confirmed === true, value => { rule.confirmed = value; });
      for (const [label, delta] of [['上移', -1], ['下移', 1]]) {
        const button = el('button', label, row); button.type = 'button'; button.className = 'secondary';
        const index = state.rules.indexOf(rule); button.disabled = index + delta < 0 || index + delta >= state.rules.length;
        button.onclick = () => { [state.rules[index], state.rules[index + delta]] = [state.rules[index + delta], state.rules[index]]; save(); render(); };
      }
      removeButton(row, () => { state.rules = state.rules.filter(r => r !== rule); state.stack_pairs = state.stack_pairs.filter(p => !p.includes(rule.id)); save(); render(); });
    });
    renderPairs();
    document.getElementById('add-item').disabled = state.items.length >= 8;
    document.getElementById('add-rule').disabled = state.rules.length >= 12;
    document.getElementById('basket-at').value = (state.at || localTime()).slice(0, 16);
  }
  document.getElementById('add-item').onclick = () => { if (state.items.length < 8) { state.items.push(addItem()); save(); render(); } };
  document.getElementById('add-rule').onclick = () => {
    if (state.rules.length >= 12) return;
    state.rules.push({id: uid('R', state.rules), name: '', kind: 'shop_coupon', mode: 'minus', threshold: '0', value: '', min_quantity: 1, basis: 'original', cap: '', items: [], start: '', end: '', source: '', confirmed: false});
    save(); render();
  };
  document.getElementById('clear-basket').onclick = () => { state = empty(); state.items.push(addItem()); save(); render(); };
  document.getElementById('basket-at').onchange = event => { state.at = event.target.value; save(); };
  document.getElementById('basket-form').addEventListener('invalid', event => { const details = event.target.closest('details'); if (details) details.open = true; }, true);
  document.getElementById('basket-form').onsubmit = () => { document.getElementById('basket-json').value = JSON.stringify(state); save(false); };
  render(); save(false);
})();
