(() => {
  const $ = (id) => document.getElementById(id);

  const els = {
    modeBadge: $("modeBadge"),
    authBar: $("authBar"),
    tokenInput: $("tokenInput"),
    tokenBtn: $("tokenBtn"),
    authMsg: $("authMsg"),
    symbol: $("symbol"),
    balance: $("balance"),
    balanceField: $("balanceField"),
    mark: $("mark"),
    risk: $("risk"),
    qty: $("qty"),
    notional: $("notional"),
    stopInput: $("stopInput"),
    btnMa: $("btnMa"),
    btnSt: $("btnSt"),
    maParams: $("maParams"),
    stParams: $("stParams"),
    indicatorMsg: $("indicatorMsg"),
    tfGroup: $("tfGroup"),
    btnLong: $("btnLong"),
    btnShort: $("btnShort"),
    btnOpen: $("btnOpen"),
    result: $("result"),
    resultTitle: $("resultTitle"),
    resultBody: $("resultBody"),
    watchInput: $("watchInput"),
    btnWatchAdd: $("btnWatchAdd"),
    watchMsg: $("watchMsg"),
    watchFilter: $("watchFilter"),
    watchList: $("watchList"),
    unprotectedAlarm: $("unprotectedAlarm"),
    positionList: $("positionList"),
    fillHint: $("fillHint"),
    logList: $("logList"),
    macdCross: $("macdCross"),
    macdDiv: $("macdDiv"),
    macdDetail: $("macdDetail"),
    apiMode: $("apiMode"),
    apiKey: $("apiKey"),
    apiSecret: $("apiSecret"),
    apiFields: $("apiFields"),
    btnApiSave: $("btnApiSave"),
    btnApiClear: $("btnApiClear"),
    apiStatus: $("apiStatus"),
    egressIp: $("egressIp"),
    regionBanner: $("regionBanner"),
    storageBanner: $("storageBanner"),
    liveNote: $("liveNote"),
    keyStorageNote: $("keyStorageNote"),
  };

  const TOKEN_KEY = "open-controller-token";
  const SYMBOL_KEY = "open-controller-symbol";
  const INTERVAL_KEY = "open-controller-interval";
  const ALLOWED_INTERVALS = ["15m", "1h", "4h", "1d"];
  let side = null;
  let authRequired = false;
  let previewTimer = null;
  let lastPreview = null;
  let currentSymbol = sessionStorage.getItem(SYMBOL_KEY) || "";
  let watchlist = [];
  let pollTimer = null;
  let interval = ALLOWED_INTERVALS.includes(sessionStorage.getItem(INTERVAL_KEY) || "")
    ? sessionStorage.getItem(INTERVAL_KEY)
    : "15m";
  let stopSource = "manual"; // manual | ma | supertrend
  let applyingFill = false;
  let indicatorBusy = false;
  let macdSeq = 0;
  let lastMacd = null;

  // Per-browser identity and the user's own Binance API (stored only here).
  const CLIENT_ID_KEY = "open-controller-client-id";
  const API_STORE_KEY = "open-controller-api";
  let liveAllowed = false;

  function makeClientId() {
    const bytes = new Uint8Array(24);
    crypto.getRandomValues(bytes);
    let bin = "";
    bytes.forEach((b) => (bin += String.fromCharCode(b)));
    return btoa(bin).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
  }

  function clientId() {
    let id = localStorage.getItem(CLIENT_ID_KEY) || "";
    if (!/^[A-Za-z0-9_-]{16,64}$/.test(id)) {
      id = makeClientId();
      localStorage.setItem(CLIENT_ID_KEY, id);
    }
    return id;
  }

  function loadApi() {
    try {
      const raw = JSON.parse(localStorage.getItem(API_STORE_KEY) || "{}");
      const mode = ["mock", "testnet", "live"].includes(raw.mode) ? raw.mode : "mock";
      return { mode, key: raw.key || "", secret: raw.secret || "" };
    } catch (_) {
      return { mode: "mock", key: "", secret: "" };
    }
  }

  function saveApi(api) {
    localStorage.setItem(API_STORE_KEY, JSON.stringify(api));
  }

  function maskKey(key) {
    if (!key) return "";
    return key.length <= 8 ? "*".repeat(key.length) : `${key.slice(0, 4)}…${key.slice(-4)}`;
  }

  function token() {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  }

  function headers(json = false) {
    const h = {};
    if (json) h["Content-Type"] = "application/json";
    const t = token();
    if (t) h["X-App-Token"] = t;
    h["X-Client-Id"] = clientId();
    const cfg = loadApi();
    h["X-Bn-Mode"] = cfg.mode;
    if (cfg.mode !== "mock" && cfg.key && cfg.secret) {
      h["X-Bn-Key"] = cfg.key;
      h["X-Bn-Secret"] = cfg.secret;
    }
    return h;
  }

  function fmt(value, digits) {
    if (value === null || value === undefined || value === "" || value === "—") return "—";
    const n = Number(value);
    if (!Number.isFinite(n)) return String(value);
    return n.toLocaleString("zh-CN", {
      maximumFractionDigits: digits,
      minimumFractionDigits: 0,
    });
  }

  function modeLabel(mode) {
    if (mode === "mock") return "模拟模式";
    if (mode === "testnet") return "测试网";
    if (mode === "live") return "实盘";
    return mode || "—";
  }

  function setModeBadge(mode) {
    els.modeBadge.textContent = modeLabel(mode);
    els.modeBadge.className = "badge " + (mode || "");
  }

  function setResult(kind, title, body) {
    els.result.hidden = false;
    els.result.className = "result " + kind;
    els.resultTitle.textContent = title;
    els.resultBody.textContent = body;
  }

  function clearComputed() {
    els.risk.textContent = "—";
    els.qty.textContent = "—";
    els.notional.textContent = "—";
    lastPreview = null;
    updateOpenEnabled();
  }

  function applyPreview(preview, balance) {
    lastPreview = preview;
    if (!preview) {
      if (balance != null) {
        const risk = Number(balance) * 0.05;
        els.risk.textContent = Number.isFinite(risk) ? fmt(risk, 4) : "—";
      } else {
        els.risk.textContent = "—";
      }
      els.qty.textContent = "—";
      els.notional.textContent = "—";
      updateOpenEnabled();
      return;
    }
    if (preview.risk_usd) els.risk.textContent = fmt(preview.risk_usd, 4);
    else if (balance != null) els.risk.textContent = fmt(Number(balance) * 0.05, 4);
    else els.risk.textContent = "—";
    els.qty.textContent = preview.quantity ? fmt(preview.quantity, 8) : "—";
    els.notional.textContent = preview.notional ? fmt(preview.notional, 4) : "—";
    updateOpenEnabled();
  }

  function inWatchlist(symbol) {
    const target = String(symbol || "").toUpperCase();
    return watchlist.some((item) => item === target);
  }

  function updateOpenEnabled() {
    const stop = els.stopInput.value.trim();
    els.btnOpen.disabled = !(stop && side && currentSymbol && inWatchlist(currentSymbol));
  }

  function setIndicatorMsg(kind, text) {
    els.indicatorMsg.textContent = text || "";
    els.indicatorMsg.className = "indicator-msg" + (kind ? " " + kind : "");
  }

  function macdTone(bias) {
    if (!bias || !side) return "neutral";
    if (bias === side) return "align";
    return "against";
  }

  function setMacdTag(el, label, bias, failed) {
    if (!el) return;
    el.textContent = label || "—";
    el.className = "macd-tag " + (failed ? "fail" : macdTone(bias));
  }

  function renderMacd(data) {
    if (!els.macdCross) return;
    lastMacd = data || null;
    const failed = !data || data.fetch_failed;
    if (!data) {
      setMacdTag(els.macdCross, "MACD：获取失败", null, true);
      setMacdTag(els.macdDiv, "小级别：获取失败", null, true);
      if (els.macdDetail) els.macdDetail.textContent = "仅供参考，不影响开单";
      return;
    }
    const cross = data.cross || {};
    const div = data.divergence || {};
    const crossFail = !!cross.ok === false && (cross.type_zh === "获取失败" || (cross.label_zh || "").endsWith("获取失败"));
    const divFail = !!div.ok === false && (div.type_zh === "获取失败" || (div.label_zh || "").endsWith("获取失败"));
    setMacdTag(els.macdCross, cross.label_zh || "MACD：获取失败", crossFail ? null : cross.bias, crossFail);
    setMacdTag(els.macdDiv, div.label_zh || "小级别：获取失败", divFail ? null : div.bias, divFail);
    const bits = [];
    if (data.params_label) bits.push(data.params_label);
    if (cross.reason_zh) bits.push(cross.reason_zh);
    if (div.reason_zh) bits.push(div.reason_zh);
    bits.push(data.note_zh || "仅供参考，不影响开单");
    if (els.macdDetail) els.macdDetail.textContent = bits.join(" · ");
  }

  function renderMacdFail(reason) {
    setMacdTag(els.macdCross, `${interval} MACD：获取失败`, null, true);
    const lowerMap = { "1d": "4h", "4h": "1h", "1h": "15m", "15m": "5m" };
    const lower = lowerMap[interval] || "小级别";
    setMacdTag(els.macdDiv, `${lower} 小级别：获取失败`, null, true);
    if (els.macdDetail) {
      els.macdDetail.textContent = (reason || "获取失败") + "。仅供参考，不影响开单";
    }
  }

  async function refreshMacd() {
    const seq = ++macdSeq;
    if (!els.macdCross) return;
    if (!currentSymbol) {
      setMacdTag(els.macdCross, "MACD：—", null, false);
      setMacdTag(els.macdDiv, "小级别：—", null, false);
      if (els.macdDetail) els.macdDetail.textContent = "请先选择自选中的交易对。仅供参考，不影响开单";
      return;
    }
    try {
      const params = new URLSearchParams();
      params.set("symbol", currentSymbol);
      params.set("interval", interval);
      const data = await api(`/api/macd?${params}`, { headers: headers() });
      if (seq !== macdSeq) return;
      renderMacd(data);
    } catch (err) {
      if (seq !== macdSeq) return;
      if (err.code === 401) return;
      const detail = err.data && err.data.detail;
      renderMacdFail((detail && detail.reason_zh) || err.message || "获取失败");
    }
  }

  function applyIndicatorParams(data) {
    const indicators = (data && data.indicators) || {};
    if (indicators.ma && indicators.ma.params_label && els.maParams) {
      els.maParams.textContent = indicators.ma.params_label;
    }
    if (indicators.supertrend && indicators.supertrend.params_label && els.stParams) {
      els.stParams.textContent = indicators.supertrend.params_label;
    }
  }

  function syncIntervalButtons() {
    const buttons = els.tfGroup ? els.tfGroup.querySelectorAll("[data-interval]") : [];
    buttons.forEach((btn) => {
      btn.setAttribute("aria-pressed", String(btn.getAttribute("data-interval") === interval));
    });
  }

  function syncSourceButtons() {
    if (els.btnMa) els.btnMa.setAttribute("aria-pressed", String(stopSource === "ma"));
    if (els.btnSt) els.btnSt.setAttribute("aria-pressed", String(stopSource === "supertrend"));
  }

  function selectInterval(next) {
    if (!ALLOWED_INTERVALS.includes(next)) return;
    interval = next;
    sessionStorage.setItem(INTERVAL_KEY, interval);
    syncIntervalButtons();
    if (stopSource === "ma" || stopSource === "supertrend") {
      refreshIndicator();
    }
    refreshMacd();
  }

  function selectIndicatorSource(source) {
    stopSource = source;
    syncSourceButtons();
    refreshIndicator();
  }

  async function refreshIndicator() {
    if (stopSource !== "ma" && stopSource !== "supertrend") return;
    if (!currentSymbol) {
      applyingFill = true;
      els.stopInput.value = "";
      applyingFill = false;
      setIndicatorMsg("err", "请先选择自选中的交易对，再读取指标");
      updateOpenEnabled();
      return;
    }
    indicatorBusy = true;
    setIndicatorMsg("info", "正在读取已收盘 K 线并计算指标…");
    try {
      const params = new URLSearchParams();
      params.set("source", stopSource);
      params.set("interval", interval);
      params.set("symbol", currentSymbol);
      if (side) params.set("side", side);
      const data = await api(`/api/indicator?${params}`, { headers: headers() });
      const fill = data && data.fill;
      applyingFill = true;
      if (fill) {
        els.stopInput.value = String(fill);
        setIndicatorMsg("ok", data.reason_zh || data.reason || "已填入止损");
      } else {
        els.stopInput.value = "";
        setIndicatorMsg("err", data.reason_zh || data.reason || "指标无法作为当前止损，未填写");
      }
      applyingFill = false;
      updateOpenEnabled();
      schedulePreview();
    } catch (err) {
      applyingFill = true;
      els.stopInput.value = "";
      applyingFill = false;
      const detail = err.data && err.data.detail;
      setIndicatorMsg("err", (detail && detail.reason_zh) || err.message || "读取指标失败");
      updateOpenEnabled();
      schedulePreview();
    } finally {
      indicatorBusy = false;
    }
  }

  function pickSymbol(symbol, { refresh = true } = {}) {
    currentSymbol = String(symbol || "").toUpperCase();
    if (currentSymbol) sessionStorage.setItem(SYMBOL_KEY, currentSymbol);
    els.symbol.textContent = currentSymbol || "—";
    renderWatchlist();
    updateOpenEnabled();
    if (refresh) {
      if (stopSource === "ma" || stopSource === "supertrend") refreshIndicator();
      else schedulePreview();
      refreshMacd();
    }
  }

  function renderWatchlist() {
    const filter = (els.watchFilter.value || "").trim().toUpperCase();
    const items = watchlist.filter((sym) => !filter || sym.includes(filter));
    els.watchList.innerHTML = "";
    if (!watchlist.length) {
      els.watchList.innerHTML = '<p class="watch-empty">自选为空。请先加入交易对，才能开单。</p>';
      return;
    }
    if (!items.length) {
      els.watchList.innerHTML = '<p class="watch-empty">自选中没有匹配项（仅扫描自选，不会搜索全市场）。</p>';
      return;
    }
    for (const sym of items) {
      const row = document.createElement("div");
      row.className = "watch-item" + (sym === currentSymbol ? " selected" : "");
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", String(sym === currentSymbol));
      const pick = document.createElement("button");
      pick.type = "button";
      pick.className = "pick";
      pick.textContent = sym;
      pick.addEventListener("click", () => pickSymbol(sym));
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "remove";
      remove.setAttribute("aria-label", "移出 " + sym);
      remove.textContent = "×";
      remove.addEventListener("click", (ev) => {
        ev.stopPropagation();
        removeWatch(sym);
      });
      row.append(pick, remove);
      els.watchList.appendChild(row);
    }
  }

  function renderPositions(positions, hasUnprotected, fillHint) {
    const list = Array.isArray(positions) ? positions : [];
    els.unprotectedAlarm.classList.toggle("hidden", !hasUnprotected);
    els.fillHint.textContent = fillHint || "";
    if (!list.length) {
      els.positionList.innerHTML = '<p class="empty">当前没有持仓</p>';
      return;
    }
    els.positionList.innerHTML = "";
    for (const pos of list) {
      const card = document.createElement("div");
      const protectedPos = !!pos.protected;
      card.className = "pos-card" + (protectedPos ? "" : " unprotected");
      const pnl = Number(pos.unrealized_pnl);
      const pnlClass = Number.isFinite(pnl) ? (pnl > 0 ? "up" : pnl < 0 ? "down" : "") : "";
      const sideClass = pos.side === "SHORT" ? "short" : "long";
      const sideText = pos.side === "SHORT" ? "空" : "多";
      const stopFlag = protectedPos && pos.stop_resting
        ? '<div class="pos-flag ok">保护性止损已挂上（reduce-only）</div>'
        : '<div class="pos-flag bad">未受保护：没有挂着的保护性止损</div>';
      card.innerHTML = `
        <div class="pos-top">
          <span class="pos-symbol">${escapeHtml(pos.symbol || "")}</span>
          <span class="pos-side ${sideClass}">${sideText}</span>
        </div>
        <div class="pos-grid">
          <div class="k">数量</div><div class="v">${escapeHtml(fmt(pos.quantity, 8))}</div>
          <div class="k">开仓价</div><div class="v">${escapeHtml(fmt(pos.entry, 8))}</div>
          <div class="k">标记价</div><div class="v">${escapeHtml(fmt(pos.mark, 8))}</div>
          <div class="k">未实现盈亏</div><div class="v pnl ${pnlClass}">${escapeHtml(fmt(pos.unrealized_pnl, 4))} USDT</div>
          <div class="k">止损价</div><div class="v">${escapeHtml(pos.stop ? fmt(pos.stop, 8) : "—")}</div>
          <div class="k">止损挂单</div><div class="v">${protectedPos && pos.stop_resting ? "是" : "否"}</div>
        </div>
        ${stopFlag}
      `;
      els.positionList.appendChild(card);
    }
  }

  const EVENT_LABELS = {
    preview: "预览",
    reject: "拒绝",
    open: "开仓",
    stop_placed: "已挂止损",
    stop_failed: "止损失败",
    stop_triggered: "止损触发平仓",
    flatten: "失败路径平仓",
    watchlist_add: "加入自选",
    watchlist_remove: "移出自选",
  };

  function formatLogTime(ts) {
    if (!ts) return "—";
    const date = new Date(ts);
    if (Number.isNaN(date.getTime())) return String(ts);
    const parts = new Intl.DateTimeFormat("zh-CN", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hour12: false,
    }).formatToParts(date);
    const get = (type) => (parts.find((p) => p.type === type) || {}).value || "";
    return `${get("year")}-${get("month")}-${get("day")} ${get("hour")}:${get("minute")}:${get("second")}`;
  }

  function renderLogs(logs) {
    const list = Array.isArray(logs) ? logs : [];
    if (!list.length) {
      els.logList.innerHTML = '<p class="empty">暂无记录</p>';
      return;
    }
    els.logList.innerHTML = "";
    for (const item of list) {
      const row = document.createElement("div");
      const event = item.event || "";
      row.className = "log-item " + event;
      const label = EVENT_LABELS[event] || event;
      const symbol = item.symbol ? ` · ${item.symbol}` : "";
      const body = item.reason_zh || "";
      row.innerHTML = `
        <div class="log-meta">
          <span class="log-event">${escapeHtml(label)}</span>
          <span>${escapeHtml(formatLogTime(item.ts))}</span>
        </div>
        <div>${escapeHtml((symbol ? item.symbol + "：" : "") + body)}</div>
      `;
      els.logList.appendChild(row);
    }
  }

  function escapeHtml(value) {
    return String(value ?? "")
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  async function api(path, options = {}) {
    const resp = await fetch(path, options);
    let data = null;
    try {
      data = await resp.json();
    } catch (_) {
      data = null;
    }
    if (resp.status === 401) {
      const err = new Error("UNAUTHORIZED");
      err.code = 401;
      err.data = data;
      throw err;
    }
    if (!resp.ok) {
      const detail = (data && data.detail) || data || {};
      const err = new Error(detail.reason_zh || detail.message || "请求失败");
      err.data = data;
      throw err;
    }
    return data;
  }

  async function loadMeta() {
    const meta = await api("/api/meta", { headers: headers() });
    authRequired = !!meta.auth_required;
    setModeBadge(meta.mode);
    watchlist = Array.isArray(meta.watchlist) ? meta.watchlist.map((s) => String(s).toUpperCase()) : [];
    if (!currentSymbol || !inWatchlist(currentSymbol)) {
      currentSymbol = (inWatchlist(meta.symbol) ? meta.symbol : watchlist[0]) || "";
    }
    els.symbol.textContent = currentSymbol || "—";
    applyIndicatorParams(meta);
    syncIntervalButtons();
    syncSourceButtons();
    renderWatchlist();
    if (authRequired && !token()) {
      els.authBar.classList.remove("hidden");
      return false;
    }
    if (authRequired) els.authBar.classList.remove("hidden");
    return true;
  }

  function applyState(data) {
    if (Array.isArray(data.watchlist)) {
      watchlist = data.watchlist.map((s) => String(s).toUpperCase());
    }
    if (data.symbol && !currentSymbol) currentSymbol = data.symbol;
    els.symbol.textContent = currentSymbol || data.symbol || "—";
    els.balance.textContent = fmt(data.balance, 4);
    els.balanceField.textContent = data.balance_field ? `取值字段：${data.balance_field}` : "";
    els.mark.textContent = fmt(data.mark, 8);
    setModeBadge(data.mode);
    applyIndicatorParams(data);
    const stop = els.stopInput.value.trim();
    if (data.preview && stop && side) {
      applyPreview(data.preview, data.balance);
    } else {
      applyPreview(null, data.balance);
    }
    renderWatchlist();
    renderPositions(data.positions, data.has_unprotected, data.fill_assumption_zh);
    renderLogs(data.logs);
    updateOpenEnabled();
  }

  async function refreshState() {
    const stop = els.stopInput.value.trim();
    const params = new URLSearchParams();
    if (stop) params.set("stop", stop);
    if (side) params.set("side", side);
    if (currentSymbol) params.set("symbol", currentSymbol);
    const qs = params.toString() ? `?${params}` : "";
    const data = await api(`/api/state${qs}`, { headers: headers() });
    applyState(data);
    return data;
  }

  function setWatchMsg(kind, text) {
    els.watchMsg.textContent = text || "";
    els.watchMsg.className = "watch-msg" + (kind ? " " + kind : "");
  }

  async function addWatch() {
    const symbol = els.watchInput.value.trim().toUpperCase();
    if (!symbol) {
      setWatchMsg("err", "请输入要加入自选的交易对");
      return;
    }
    els.btnWatchAdd.disabled = true;
    try {
      const data = await api("/api/watchlist", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({ symbol }),
      });
      watchlist = data.watchlist || watchlist;
      els.watchInput.value = "";
      setWatchMsg("ok", data.reason_zh || "已加入自选");
      if (data.symbol) pickSymbol(data.symbol, { refresh: true });
      else renderWatchlist();
      await refreshState();
    } catch (err) {
      const detail = err.data && err.data.detail;
      setWatchMsg("err", (detail && detail.reason_zh) || err.message || "加入自选失败");
    } finally {
      els.btnWatchAdd.disabled = false;
    }
  }

  async function removeWatch(symbol) {
    try {
      const data = await api(`/api/watchlist/${encodeURIComponent(symbol)}`, {
        method: "DELETE",
        headers: headers(),
      });
      watchlist = data.watchlist || watchlist.filter((s) => s !== symbol);
      setWatchMsg("ok", data.reason_zh || "已移出自选");
      if (currentSymbol === symbol) {
        currentSymbol = watchlist[0] || "";
        if (currentSymbol) sessionStorage.setItem(SYMBOL_KEY, currentSymbol);
        else sessionStorage.removeItem(SYMBOL_KEY);
      }
      renderWatchlist();
      updateOpenEnabled();
      await refreshState();
    } catch (err) {
      const detail = err.data && err.data.detail;
      setWatchMsg("err", (detail && detail.reason_zh) || err.message || "移出自选失败");
    }
  }

  function selectSide(next) {
    side = next;
    els.btnLong.setAttribute("aria-pressed", String(next === "LONG"));
    els.btnShort.setAttribute("aria-pressed", String(next === "SHORT"));
    updateOpenEnabled();
    if (stopSource === "ma" || stopSource === "supertrend") refreshIndicator();
    else schedulePreview();
    if (lastMacd) renderMacd(lastMacd);
    refreshMacd();
  }

  function schedulePreview() {
    clearTimeout(previewTimer);
    previewTimer = setTimeout(() => {
      refreshState().catch((err) => {
        if (err.code === 401) {
          els.authBar.classList.remove("hidden");
          els.authMsg.textContent = "访问令牌无效，请重新输入";
        }
      });
    }, 200);
  }

  async function openTrade() {
    const stop = els.stopInput.value.trim();
    if (!currentSymbol || !inWatchlist(currentSymbol)) {
      setResult("reject", "已拒绝", "该交易对不在自选中，请先加入自选后再开单");
      return;
    }
    if (!stop) {
      setResult("reject", "已拒绝", "止损价为必填项，不填不允许开单");
      return;
    }
    if (!side) {
      setResult("reject", "已拒绝", "请选择开仓方向");
      return;
    }
    els.btnOpen.disabled = true;
    try {
      const data = await api("/api/open", {
        method: "POST",
        headers: headers(true),
        body: JSON.stringify({ side, stop, symbol: currentSymbol }),
      });
      const decision = data.decision || {};
      if (data.status === "PASS" && data.opened) {
        const lines = [
          data.reason_zh || "开仓成功，保护性止损已挂上",
          data.symbol ? `交易对：${data.symbol}` : "",
          data.entry_order_id ? `开仓单号：${data.entry_order_id}` : "",
          data.stop_order_id ? `止损单号：${data.stop_order_id}` : "",
          decision.quantity ? `数量：${decision.quantity}` : "",
          decision.risk_usd ? `风险金额：${decision.risk_usd} USDT` : "",
        ].filter(Boolean);
        setResult("pass", "开单成功", lines.join("\n"));
      } else {
        const reason = data.reason_zh || decision.reason_zh || "已拒绝";
        const extra = data.entry_order_id || (data.entry_order && data.entry_order.orderId);
        setResult("reject", "已拒绝", extra ? `${reason}\n开仓单号：${extra}` : reason);
      }
      if (data.positions || data.logs) {
        renderPositions(data.positions, data.has_unprotected, els.fillHint.textContent);
        renderLogs(data.logs);
      }
      await refreshState();
    } catch (err) {
      if (err.code === 401) {
        els.authBar.classList.remove("hidden");
        els.authMsg.textContent = "访问令牌无效";
        setResult("reject", "已拒绝", "需要有效的访问令牌");
      } else {
        const detail = err.data && err.data.detail;
        const msg = (detail && detail.reason_zh) || err.message || "开单失败";
        setResult("reject", "已拒绝", msg);
      }
    } finally {
      updateOpenEnabled();
    }
  }

  function setApiStatus(kind, text) {
    els.apiStatus.textContent = text || "";
    els.apiStatus.className = "watch-msg" + (kind ? " " + kind : "");
  }

  function renderApiPanel() {
    const cfg = loadApi();
    els.apiMode.value = cfg.mode;
    els.apiKey.value = "";
    els.apiSecret.value = "";
    els.apiKey.placeholder = cfg.key ? `已保存：${maskKey(cfg.key)}（留空则沿用）` : "粘贴你的 API Key";
    els.apiSecret.placeholder = cfg.secret ? "已保存（不显示，留空则沿用）" : "粘贴你的 API Secret";
    syncApiModeNote();
    if (cfg.mode === "mock") {
      setApiStatus("", cfg.key ? `当前：模拟模式（浏览器里存有 Key ${maskKey(cfg.key)}，模拟模式不使用）` : "当前：模拟模式");
    } else {
      setApiStatus("", `当前：${modeLabel(cfg.mode)}，Key ${maskKey(cfg.key) || "未配置"}`);
    }
  }

  function syncApiModeNote() {
    const m = els.apiMode.value;
    els.apiFields.style.opacity = m === "mock" ? "0.6" : "1";
    if (m === "live" && !liveAllowed) {
      els.liveNote.textContent = "服务端未开启实盘：需要运行服务的人设置 ALLOW_LIVE=yes 并重启，网页上无法打开。";
    } else if (m === "live") {
      els.liveNote.textContent = "实盘会真实下单，请确认 Key 只勾了「允许合约」。";
    } else if (m === "testnet") {
      els.liveNote.textContent = "测试网使用 testnet.binancefuture.com 的独立 Key。";
    } else {
      els.liveNote.textContent = "";
    }
  }

  function renderRegion(region) {
    if (!els.regionBanner || !region) return;
    if (region.status === "blocked") {
      els.regionBanner.textContent = "⚠ " + (region.message_zh || "币安合约接口在本服务器所在地区受限（451/403）");
      els.regionBanner.classList.remove("hidden");
    } else {
      els.regionBanner.classList.add("hidden");
      els.regionBanner.textContent = "";
    }
  }

  let regionTimer = null;
  async function pollRegion() {
    try {
      const region = await api("/api/region-check");
      renderRegion(region);
      if (region.status === "pending") {
        regionTimer = setTimeout(pollRegion, 3000);
        return;
      }
    } catch (_) { /* ignore; retry later */ }
    regionTimer = setTimeout(pollRegion, 120000);
  }

  async function loadServerInfo() {
    try {
      const info = await api("/api/server-info", { headers: headers() });
      liveAllowed = !!info.live_allowed;
      renderRegion(info.region);
      if (els.storageBanner) {
        if (info.ephemeral && info.storage_note_zh) {
          els.storageBanner.textContent = info.storage_note_zh;
          els.storageBanner.classList.remove("hidden");
        } else {
          els.storageBanner.classList.add("hidden");
        }
      }
      if (!regionTimer) pollRegion();
      els.egressIp.textContent = info.egress_ip || "获取失败";
      if (info.key_storage_zh) els.keyStorageNote.textContent = info.key_storage_zh;
      syncApiModeNote();
    } catch (_) {
      els.egressIp.textContent = "获取失败";
    }
  }

  function resetTradingView() {
    lastPreview = null;
    clearComputed();
    els.result.hidden = true;
  }

  async function saveApiSettings() {
    const prev = loadApi();
    const mode = els.apiMode.value;
    const key = els.apiKey.value.trim() || prev.key;
    const secret = els.apiSecret.value.trim() || prev.secret;
    if (mode === "live" && !liveAllowed) {
      setApiStatus("err", "服务端未开启实盘（ALLOW_LIVE=yes），未切换");
      return;
    }
    if (mode !== "mock") {
      if (!/^[A-Za-z0-9]{16,128}$/.test(key) || !/^[A-Za-z0-9]{16,128}$/.test(secret)) {
        setApiStatus("err", "请填写完整的 API Key 和 Secret（只含字母数字）");
        return;
      }
      if (location.protocol !== "https:" && !["localhost", "127.0.0.1"].includes(location.hostname)) {
        if (!confirm("当前不是 HTTPS 连接，Key 会明文经过网络。确定继续？")) return;
      }
    }
    els.btnApiSave.disabled = true;
    const next = { mode, key: mode === "mock" && !els.apiKey.value.trim() ? prev.key : key, secret: mode === "mock" && !els.apiSecret.value.trim() ? prev.secret : secret };
    try {
      if (mode === "mock") {
        saveApi(next);
        renderApiPanel();
        setApiStatus("ok", "已切换到模拟模式");
      } else {
        // Verify with the new values before keeping them.
        const tmpHeaders = headers(true);
        tmpHeaders["X-Bn-Mode"] = mode;
        tmpHeaders["X-Bn-Key"] = key;
        tmpHeaders["X-Bn-Secret"] = secret;
        const res = await api("/api/account/verify", { method: "POST", headers: tmpHeaders });
        if (!res.ok) {
          setApiStatus("err", `验证失败（未切换）：${res.reason_zh}${res.binance_message ? "\n币安返回：" + res.binance_message : ""}`);
          return;
        }
        saveApi(next);
        renderApiPanel();
        setApiStatus("ok", `${res.reason_zh}（${modeLabel(mode)}，Key ${res.key_masked || maskKey(key)}，USDT 余额 ${res.usdt_balance ?? "—"}）`);
      }
      resetTradingView();
      await loadMeta();
      await refreshState();
      refreshMacd();
    } catch (err) {
      const detail = err.data && err.data.detail;
      setApiStatus("err", (detail && detail.reason_zh) || err.message || "保存失败");
    } finally {
      els.btnApiSave.disabled = false;
    }
  }

  async function clearApiSettings() {
    localStorage.removeItem(API_STORE_KEY);
    renderApiPanel();
    setApiStatus("ok", "已清除浏览器里保存的 Key/Secret，已回到模拟模式");
    resetTradingView();
    try {
      await refreshState();
    } catch (_) {}
  }

  function pollMs() {
    // Signed modes hit Binance with the shared server IP; poll slower.
    return loadApi().mode === "mock" ? 3000 : 8000;
  }

  function startPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = setInterval(() => {
      if (authRequired && !token()) return;
      refreshState().catch(() => {});
      refreshMacd();
    }, pollMs());
  }

  els.btnLong.addEventListener("click", () => selectSide("LONG"));
  els.btnShort.addEventListener("click", () => selectSide("SHORT"));
  els.stopInput.addEventListener("input", () => {
    if (!applyingFill && stopSource !== "manual") {
      stopSource = "manual";
      syncSourceButtons();
      setIndicatorMsg("info", "已改为手动输入止损");
    }
    updateOpenEnabled();
    schedulePreview();
  });
  if (els.tfGroup) {
    els.tfGroup.addEventListener("click", (ev) => {
      const btn = ev.target.closest("[data-interval]");
      if (!btn) return;
      selectInterval(btn.getAttribute("data-interval"));
    });
  }
  if (els.btnMa) els.btnMa.addEventListener("click", () => selectIndicatorSource("ma"));
  if (els.btnSt) els.btnSt.addEventListener("click", () => selectIndicatorSource("supertrend"));
  els.btnOpen.addEventListener("click", openTrade);
  els.btnWatchAdd.addEventListener("click", addWatch);
  els.watchInput.addEventListener("keydown", (ev) => {
    if (ev.key === "Enter") {
      ev.preventDefault();
      addWatch();
    }
  });
  els.watchFilter.addEventListener("input", renderWatchlist);
  els.apiMode.addEventListener("change", syncApiModeNote);
  els.btnApiSave.addEventListener("click", async () => {
    await saveApiSettings();
    startPolling();
  });
  els.btnApiClear.addEventListener("click", async () => {
    await clearApiSettings();
    startPolling();
  });
  els.tokenBtn.addEventListener("click", async () => {
    sessionStorage.setItem(TOKEN_KEY, els.tokenInput.value.trim());
    els.authMsg.textContent = "";
    try {
      await refreshState();
      els.authMsg.textContent = "已验证";
      els.authMsg.style.color = "#4ade80";
    } catch (err) {
      els.authMsg.style.color = "";
      els.authMsg.textContent = err.code === 401 ? "访问令牌无效" : err.message;
    }
  });

  (async () => {
    renderApiPanel();
    loadServerInfo();
    try {
      const ok = await loadMeta();
      if (!ok) return;
      await refreshState();
      refreshMacd();
      startPolling();
    } catch (err) {
      if (err.code === 401) {
        els.authBar.classList.remove("hidden");
        els.authMsg.textContent = "请输入访问令牌";
        return;
      }
      setResult("reject", "无法加载账户状态", err.message || "请检查网络后刷新");
    }
  })();
})();
