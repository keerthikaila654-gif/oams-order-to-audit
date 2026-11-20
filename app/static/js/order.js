/* Mobile order entry with offline queue.
   - orders that cannot be sent are stored in localStorage with a client_uuid
   - they are re-sent automatically when the browser comes back online (server de-duplicates on client_uuid) */
(function () {
  const boot = JSON.parse(document.getElementById('boot').textContent);
  const QKEY = 'oams_offline_orders';
  let cust = boot.customers.length === 1 ? boot.customers[0].id : null;
  const qtys = {};
  const $ = (id) => document.getElementById(id);
  const rupees = (p) => '₹' + (p / 100).toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const uuid = () => (crypto.randomUUID ? crypto.randomUUID() : 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, c => { const r = Math.random() * 16 | 0; return (c === 'x' ? r : (r & 3 | 8)).toString(16); }));
  const getQ = () => { try { return JSON.parse(localStorage.getItem(QKEY) || '[]'); } catch (e) { return []; } };
  const setQ = (q) => { try { localStorage.setItem(QKEY, JSON.stringify(q)); } catch (e) { } };
  const csrf = () => document.querySelector('meta[name=csrf-token]').content;

  function msg(text, cls) { $('msg').innerHTML = text ? '<div class="flash ' + cls + '">' + text + '</div>' : ''; }

  function renderCusts() {
    $('custs').innerHTML = boot.customers.map(c =>
      '<button type="button" class="custcard ' + (c.id === cust ? 'sel' : '') + '" data-id="' + c.id + '"><b>' + c.name + '</b><small>' + c.type + ' · ' + c.state +
      ' · outstanding ' + c.outstanding + '</small></button>').join('') || '<p class="note">No customers assigned to you.</p>';
    $('custs').querySelectorAll('button').forEach(b => b.onclick = () => { cust = +b.dataset.id; renderCusts(); renderProds(); total(); });
  }
  function price(p) { return cust ? (boot.prices[cust] || {})[p.id] : null; }
  function renderProds() {
    $('prods').innerHTML = boot.products.map(p => {
      const pr = price(p);
      return '<div class="prod"><div><div class="nm">' + p.name + '</div><div class="meta">' + (pr == null ? 'select a customer' : rupees(pr) + ' / pack') +
        ' · in stock: ' + (p.available > 0 ? p.available : '<b style="color:#b3261e">none</b>') + '</div></div>' +
        '<div class="step"><button type="button" data-d="-1" data-p="' + p.id + '">−</button><input type="number" inputmode="numeric" min="0" step="1" id="q' + p.id +
        '" value="' + (qtys[p.id] || '') + '" placeholder="0"><button type="button" data-d="1" data-p="' + p.id + '">+</button></div></div>';
    }).join('');
    $('prods').querySelectorAll('button').forEach(b => b.onclick = () => { const i = $('q' + b.dataset.p); i.value = Math.max(0, (+i.value || 0) + (+b.dataset.d) * (b.dataset.d > 0 ? 10 : 10)); qtys[b.dataset.p] = +i.value; total(); });
    $('prods').querySelectorAll('input').forEach(i => i.oninput = () => { qtys[i.id.slice(1)] = +i.value || 0; total(); });
  }
  function total() {
    let t = 0, n = 0;
    boot.products.forEach(p => { const q = qtys[p.id] || 0, pr = price(p); if (q > 0 && pr != null) { t += Math.round(q * pr); n++; } });
    $('total').textContent = rupees(t); $('lines').textContent = n + (n === 1 ? ' product' : ' products');
  }
  function net() { const on = navigator.onLine; const e = $('net'); e.textContent = on ? 'Online' : 'Offline - orders are saved on this phone'; e.className = 'netbadge ' + (on ? 'on' : 'off'); }
  function pendingBanner() { const n = getQ().length; const e = $('pending'); e.style.display = n ? 'block' : 'none'; e.textContent = n ? n + ' order(s) saved offline - will sync automatically when you are online.' : ''; }

  async function send(o) {
    const r = await fetch('/s/api/orders', { method: 'POST', headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrf() }, body: JSON.stringify(o), credentials: 'same-origin' });
    let j = {}; try { j = await r.json(); } catch (e) { }
    return { status: r.status, json: j };
  }
  async function sync() {
    if (!navigator.onLine) return;
    let q = getQ(); if (!q.length) return;
    try { const t = await fetch('/api/csrf', { credentials: 'same-origin' }); if (t.ok) document.querySelector('meta[name=csrf-token]').content = (await t.json()).token; else return; } catch (e) { return; }
    const left = [];
    for (const o of q) {
      try {
        const r = await send(o);
        if (r.status === 200) continue;            // saved (or duplicate) - drop from queue
        if (r.status === 400) { msg('Offline order rejected: ' + (r.json.error || 'invalid'), 'error'); continue; }
        left.push(o);
      } catch (e) { left.push(o); }
    }
    setQ(left); pendingBanner();
    if (left.length < q.length) msg((q.length - left.length) + ' offline order(s) synced to the factory.', 'success');
  }

  $('submit').onclick = async () => {
    if (!cust) { msg('Select a customer first.', 'error'); return; }
    const items = boot.products.filter(p => (qtys[p.id] || 0) > 0).map(p => ({ product_id: p.id, qty: String(qtys[p.id]) }));
    if (!items.length) { msg('Add at least one product.', 'error'); return; }
    const order = { client_uuid: uuid(), customer_id: cust, items, notes: $('notes').value };
    $('submit').disabled = true;
    try {
      if (!navigator.onLine) throw new Error('offline');
      const r = await send(order);
      if (r.status === 200) { msg('Order ' + r.json.order_no + ' submitted to the factory ✔', 'success'); Object.keys(qtys).forEach(k => delete qtys[k]); $('notes').value = ''; renderProds(); total(); }
      else if (r.status === 400) msg(r.json.error || 'Could not submit', 'error');
      else throw new Error('server');
    } catch (e) {
      const q = getQ(); q.push(order); setQ(q); pendingBanner();
      msg('No connection - order saved on this phone and will sync automatically.', 'success');
      Object.keys(qtys).forEach(k => delete qtys[k]); renderProds(); total();
    }
    $('submit').disabled = false; window.scrollTo({ top: 0, behavior: 'smooth' });
  };

  window.addEventListener('online', () => { net(); sync(); }); window.addEventListener('offline', net);
  renderCusts(); renderProds(); total(); net(); pendingBanner(); sync();
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { });
})();
