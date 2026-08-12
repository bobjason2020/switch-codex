    async function loadOverview() {
      loadModelAvailability();
      try {
        const range = $("statsRange").value || "today";
        await loadPageFilterOptions("stats");
        const model = $("statsModel").value || "";
        const pool = $("statsPool").value || "";
        const upstream = $("statsUpstream").value || "";
        const statsUrl = "/api/logs/stats?range=" + encodeURIComponent(range) +
          (model ? "&model=" + encodeURIComponent(model) : "") +
          (pool ? "&pool=" + encodeURIComponent(pool) : "") +
          (upstream ? "&upstream=" + encodeURIComponent(upstream) : "");
        const [health, stats, recent] = await Promise.all([
          api("/health"),
          api(statsUrl),
          api("/api/logs?limit=10"),
        ]);
        renderStats(health, stats);
        const tb = $("recentBody");
        tb.innerHTML = "";
        if (!recent.data.length) {
          tb.innerHTML = '<tr><td colspan="10" class="muted">暂无请求记录</td></tr>';
        }
        for (const e of recent.data) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td class="mono">${fmtTs(e.ts)}</td>
            ${modelCell(e)}
            ${upstreamCell(e)}
            ${sessionIdCell(e)}
            <td>${statusPill(e.status, e.error_log_id, e.attempts)}</td>
            ${timeCell(e)}
            ${tpsCell(e)}
            ${tokensCell(e)}
            ${feeCell(e)}
            <td>${fmtRealCost(e.real_cost_cny)}</td>`;
          const feeTd = tr.querySelector('[data-fee]');
          if (feeTd) feeTd.onclick = () => openFeeModal(e);
          tb.appendChild(tr);
        }
      } catch (e) { showMsg(e.message, false); }
    }

    $("btnStatsApply").onclick = async () => {
      const btn = $("btnStatsApply");
      btn.disabled = true;
      btn.textContent = "刷新中…";
      try {
        await loadOverview();
      } finally {
        btn.disabled = false;
        btn.textContent = "筛选";
      }
    };

    async function loadUpstreams() {
      try {
        const data = await api("/api/upstreams");
        const tb = $("tbody");
        tb.innerHTML = "";
        if (!data.data.length) {
          tb.innerHTML = '<tr><td colspan="6" class="muted">暂无上游，点击右上角「添加上游」</td></tr>';
          return;
        }
        for (const u of data.data) {
          const inScope = u.model === activeModel;
          const tr = document.createElement("tr");
          if (inScope && u.enabled) tr.style.background = "rgba(59,130,246,0.06)";
          const checkedHint = u.probe_checked_at
            ? `<div class="muted" style="font-size:0.7rem;margin-top:4px">${escapeHtml(fmtTs(u.probe_checked_at))}</div>`
            : "";
          const mmList = u.model_map || [];
          const reqHint = mmList.length
            ? `<div class="muted" style="font-size:0.72rem;margin-top:4px">实际模型：${escapeHtml(mmList.map((e) => e.actual || e.model).join(", "))}</div>`
            : "";
          tr.innerHTML = `
            <td><strong>${escapeHtml(u.name)}</strong></td>
            <td>
              <span class="pill model">${escapeHtml(u.model)}</span>
              ${reqHint}
              <div class="muted" style="font-size:0.72rem;margin-top:4px">${inScope ? "当前范围" : ""}</div>
            </td>
            <td><input class="inline-edit" type="number" step="1" value="${u.priority}" data-edit="priority" title="修改后回车或失焦保存" /></td>
            <td><input class="inline-edit" type="number" min="0" step="0.001" value="${Number(u.multiplier ?? 1).toFixed(6).replace(/0+$/, "").replace(/\.$/, "")}" data-edit="multiplier" title="修改后回车或失焦保存" /></td>
            <td>${healthStatusPill(u)}${checkedHint}</td>
            <td class="actions">
              <button class="btn-ghost btn-sm" data-act="edit">编辑</button>
              <button class="btn-ghost btn-sm" data-act="test">测试</button>
              <button class="btn-danger btn-sm" data-act="del">删除</button>
            </td>`;
          tr.querySelector('[data-act="edit"]').onclick = () => {
            openUpstreamModal(u);
          };
          tr.querySelectorAll("input[data-edit]").forEach((inp) => {
            const field = inp.dataset.edit;
            const orig = inp.value;
            const save = async () => {
              const raw = inp.value.trim();
              if (raw === orig) return;
              if (raw === "") { inp.value = orig; return; }
              const num = field === "priority" ? Math.round(Number(raw)) : Number(raw);
              if (!Number.isFinite(num) || (field === "multiplier" && num < 0)) {
                showMsg(field === "multiplier" ? "倍率需为非负数字" : "优先级需为数字", false);
                inp.value = orig;
                return;
              }
              try {
                await api("/api/upstreams/" + u.id, { method: "PUT", body: JSON.stringify({ [field]: num }) });
                showMsg("已更新 " + u.name + "（" + (field === "priority" ? "优先级" : "倍率") + "=" + num + "）", true);
                await loadUpstreams();
              } catch (e) {
                showMsg(e.message, false);
                inp.value = orig;
              }
            };
            inp.addEventListener("keydown", (ev) => {
              if (ev.key === "Enter") { ev.preventDefault(); inp.blur(); }
            });
            inp.addEventListener("blur", save);
          });
          tr.querySelector('[data-act="del"]').onclick = async () => {
            if (!confirm("删除 " + u.name + " ?")) return;
            try {
              await api("/api/upstreams/" + u.id, { method: "DELETE" });
              showMsg("已删除 " + u.name, true);
              await refreshModels();
              await Promise.all([loadUpstreams(), loadOverview()]);
            } catch (e) { showMsg(e.message, false); }
          };
          tr.querySelector('[data-act="test"]').onclick = async () => {
            try {
              showMsg("测试中 " + u.name + " ...", true);
              const r = await api("/api/upstreams/" + u.id + "/test", { method: "POST" });
              showMsg(
                (r.ok ? "OK " : "FAIL ") + u.name +
                " [" + (r.probe_model || "?") + "] HTTP " +
                (r.status_code || "-") + " " + (r.body_preview || r.error || "").slice(0, 100),
                !!r.ok
              );
              await loadUpstreams();
            } catch (e) { showMsg(e.message, false); }
          };
          tb.appendChild(tr);
        }
      } catch (e) { showMsg(e.message, false); }
    }

    async function loadLogs(reset) {
      if (reset) logOffset = 0;
      try {
        if (reset) await loadPageFilterOptions("log");
        const params = new URLSearchParams({ limit: PAGE_SIZE, offset: logOffset });
        appendLogRangeParams(params, "log");
        const pool = $("logPool").value;
        const model = $("logModel").value;
        const st = $("logStatus").value;
        const upstream = $("logUpstream").value || "";
        const q = $("logQ").value.trim();
        if (pool) params.set("pool", pool);
        if (model) params.set("model", model);
        if (st) params.set("status", st);
        if (upstream) params.set("upstream", upstream);
        if (q) params.set("q", q);
        const data = await api("/api/logs?" + params.toString());
        logTotal = data.total;
        const tb = $("logBody");
        tb.innerHTML = "";
        if (!data.data.length) {
          tb.innerHTML = '<tr><td colspan="10" class="muted">暂无请求日志</td></tr>';
        }
        for (const e of data.data) {
          const tr = document.createElement("tr");
          tr.innerHTML = `
            <td class="mono">${fmtTs(e.ts)}</td>
            ${modelCell(e)}
            ${upstreamCell(e)}
            ${sessionIdCell(e)}
            <td>${statusPill(e.status, e.error_log_id, e.attempts)}</td>
            ${timeCell(e)}
            ${tpsCell(e)}
            ${tokensCell(e)}
            ${feeCell(e)}
            <td>${fmtRealCost(e.real_cost_cny)}</td>`;
          const errLink = tr.querySelector('[data-error-id]');
          if (errLink) {
            errLink.onclick = (ev) => {
              ev.preventDefault();
              jumpToError(errLink.dataset.errorId);
            };
          }
          const feeTd = tr.querySelector('[data-fee]');
          if (feeTd) feeTd.onclick = () => openFeeModal(e);
          tb.appendChild(tr);
        }
        $("logInfo").textContent = `共 ${logTotal} 条，显示 ${Math.min(logOffset + PAGE_SIZE, logTotal)} 条`;
        syncPager("log");
      } catch (e) { showMsg(e.message, false); }
    }

    function localDateTimeValue(d) {
      const pad = (n) => String(n).padStart(2, "0");
      return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}` +
        `T${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }

    function syncRangeUI(kind) {
      const isLog = kind === "log";
      const sel = $(isLog ? "logRange" : "errRange");
      const custom = $(isLog ? "logCustomRange" : "errCustomRange");
      const show = sel.value === "custom";
      custom.style.display = show ? "flex" : "none";
      if (show) {
        const now = new Date();
        const startInput = $(isLog ? "logStart" : "errStart");
        const endInput = $(isLog ? "logEnd" : "errEnd");
        if (!startInput.value) {
          const start = new Date(now);
          start.setHours(0, 0, 0, 0);
          startInput.value = localDateTimeValue(start);
        }
        if (!endInput.value) endInput.value = localDateTimeValue(now);
      }
    }

    function appendLogRangeParams(params, kind) {
      const isLog = kind === "log";
      const range = $(isLog ? "logRange" : "errRange").value;
      if (range === "custom") {
        params.set("range", "custom");
        const start = $(isLog ? "logStart" : "errStart").value;
        const end = $(isLog ? "logEnd" : "errEnd").value;
        if (start) params.set("start", start);
        if (end) params.set("end", end);
      } else {
        params.set("range", range);
      }
    }

    function syncPager(kind) {
      const isLog = kind === "log";
      const total = isLog ? logTotal : errTotal;
      const offset = isLog ? logOffset : errOffset;
      const page = Math.floor(offset / PAGE_SIZE) + 1;
      const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      $((isLog ? "log" : "err") + "PageInfo").textContent = `第 ${page} / ${pages} 页`;
      $((isLog ? "log" : "err") + "PageInput").value = page;
      $((isLog ? "log" : "err") + "PageInput").max = pages;
      $(isLog ? "btnLogPrev" : "btnErrPrev").disabled = page <= 1;
      $(isLog ? "btnLogNext" : "btnErrNext").disabled = page >= pages;
    }

    function goToPage(kind, raw) {
      const isLog = kind === "log";
      const total = isLog ? logTotal : errTotal;
      const n = Number.parseInt(raw, 10);
      if (!Number.isFinite(n)) return;
      const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
      const page = Math.min(Math.max(1, n), pages);
      if (isLog) {
        logOffset = (page - 1) * PAGE_SIZE;
        loadLogs(false);
      } else {
        errOffset = (page - 1) * PAGE_SIZE;
        loadErrors(false);
      }
    }

    async function jumpToError(id) {
      switchPage("errors");
      try {
        await openErrDetail(id);
      } catch (e) { showMsg(e.message, false); }
    }

    function attemptsSummary(e) {
      const a = e.attempts || [];
      if (!a.length) return "";
      return a.map((x) => {
        const st = x.status == null ? "ERR" : x.status;
        return escapeHtml((x.upstream || "?") + "(" + st + ")");
      }).join(" → ");
    }

    async function loadErrors(reset) {
      if (reset) errOffset = 0;
      try {
        if (reset) await loadPageFilterOptions("err");
        const params = new URLSearchParams({ limit: PAGE_SIZE, offset: errOffset });
        appendLogRangeParams(params, "err");
        const pool = $("errPool").value;
        const model = $("errModel").value;
        const upstream = $("errUpstream").value || "";
        const q = $("errQ").value.trim();
        if (pool) params.set("pool", pool);
        if (model) params.set("model", model);
        if (upstream) params.set("upstream", upstream);
        if (q) params.set("q", q);
        const data = await api("/api/errors?" + params.toString());
        errTotal = data.total;
        const tb = $("errBody");
        tb.innerHTML = "";
        if (!data.data.length) {
          tb.innerHTML = '<tr><td colspan="9" class="muted">暂无错误日志（仅记录失败请求）</td></tr>';
        }
        for (const e of data.data) {
          const tr = document.createElement("tr");
          const bodyTag = e.has_body
            ? (e.request_body_truncated
                ? `<span class="box warn">${fmtBytes(e.request_body_len)} 截断</span>`
                : `<span class="box green">${fmtBytes(e.request_body_len)}</span>`)
            : "";
          tr.innerHTML = `
            <td class="mono">${fmtTs(e.ts)}</td>
            <td class="mono">${escapeHtml(e.pool || "")}</td>
            <td class="mono">${escapeHtml(e.client_model || "")}</td>
            <td>${statusPill(e.status)}</td>
            <td class="mono muted" title="${escapeHtml(attemptsSummary(e))}">${attemptsSummary(e).slice(0, 80)}</td>
            <td class="mono muted" title="${escapeHtml(e.error || "")}">${escapeHtml((e.error || "").slice(0, 90))}</td>
            <td>${bodyTag}</td>
            <td>${hasValue(e.duration_ms) ? fmtDur(e.duration_ms) : ""}</td>
            <td><button class="btn-ghost btn-sm" data-act="detail" type="button">详情</button></td>`;
          tr.querySelector('[data-act="detail"]').onclick = () => openErrDetail(e.id);
          tb.appendChild(tr);
        }
        $("errInfo").textContent =
          `共 ${errTotal} 条（自动保留 24h），显示 ${Math.min(errOffset + PAGE_SIZE, errTotal)} 条`;
        syncPager("err");
      } catch (e) { showMsg(e.message, false); }
    }

    function prettyBody(body) {
      if (body == null) return "（无请求体）";
      if (typeof body === "string") return body;
      try { return JSON.stringify(body, null, 2); } catch { return String(body); }
    }

    function collapsibleError(text) {
      const s = String(text || "");
      const escaped = escapeHtml(s);
      if (s.length <= 600 && s.split("\n").length <= 10) {
        return `<div class="attempt-body">${escaped}</div>`;
      }
      const id = "errclp" + Math.random().toString(36).slice(2, 8);
      return `<div class="err-collapse-wrap">
        <div class="attempt-body err-collapse collapsed" id="${id}">${escaped}</div>
        <button type="button" class="btn-ghost btn-sm err-collapse-toggle" data-target="${id}">展开</button>
      </div>`;
    }

    document.addEventListener("click", (ev) => {
      const btn = ev.target && ev.target.closest ? ev.target.closest(".err-collapse-toggle") : null;
      if (!btn) return;
      const target = document.getElementById(btn.dataset.target || "");
      if (!target) return;
      const expanded = target.classList.toggle("expanded");
      target.classList.toggle("collapsed", !expanded);
      btn.textContent = expanded ? "收起" : "展开";
    });

    function attemptCard(a, i) {
      const st = a.status == null
        ? '<span class="pill off">ERR</span>'
        : statusPill(a.status);
      const flag = a.failover
        ? '<span class="failover-yes">✓ 已切换下一个上游</span>'
        : '<span class="failover-no">✗ 未切换（直接返回错误）</span>';
      const cap = a.capacity_error
        ? '<span class="box warn">capacity</span>'
        : "";
      return `<div class="attempt-card">
        <div class="attempt-head">
          <span class="pill model">#${i + 1} ${escapeHtml(a.upstream || "?")}</span>
          ${st}
          ${a.priority == null ? "" : `<span class="muted">priority=${escapeHtml(a.priority)}</span>`}
          ${flag}
          ${cap}
        </div>
        ${a.url ? `<div class="muted">${escapeHtml(a.url)}</div>` : ""}
        ${a.error ? collapsibleError(a.error) : ""}
      </div>`;
    }

    async function openErrDetail(id) {
      try {
        const r = await api("/api/errors/" + id);
        const e = r.data;
        const attempts = (e.attempts || []).map(attemptCard).join("");
        const body = prettyBody(e.request_body);
        $("errDetail").innerHTML = `
          <div class="err-meta">
            <div>ID <b class="mono">${escapeHtml(e.id)}</b></div>
            <div>时间 <b>${fmtTs(e.ts)}</b></div>
            <div>池 <b>${escapeHtml(e.pool || "")}</b></div>
            <div>客户端模型 <b>${escapeHtml(e.client_model || "")}</b></div>
            <div>状态 <b>${e.status == null ? "" : escapeHtml(e.status)}</b></div>
            <div>用时 <b>${hasValue(e.duration_ms) ? fmtDur(e.duration_ms) : ""}</b></div>
            <div>客户端 IP <b>${escapeHtml(e.client_ip || "")}</b></div>
            <div>请求体 <b>${fmtBytes(e.request_body_len)}${e.request_body_truncated ? "（已截断）" : ""}</b></div>
          </div>
          <div class="err-section">
            <h4>错误信息</h4>
            ${collapsibleError(e.error || "")}
          </div>
          <div class="err-section">
            <h4>上游尝试记录（${(e.attempts || []).length}）</h4>
            ${attempts || '<div class="muted">无</div>'}
          </div>
          <div class="err-section">
            <h4>请求体（保留 24 小时）</h4>
            <pre class="err-json">${escapeHtml(body)}</pre>
          </div>`;
        $("errModal").style.display = "flex";
      } catch (e) { showMsg(e.message, false); }
    }

    function closeErrModal() {
      $("errModal").style.display = "none";
    }

    async function loadPricing() {
      try {
        const d = await api("/api/pricing");
        pricing = d.pricing || {};
        pricingModels = d.models || Object.keys(pricing);
        renderPricingRows();
      } catch (e) { showMsg(e.message, false); }
    }

    function renderPricingRows() {
      const models = [...new Set([...(pricingModels || []), ...Object.keys(pricing)])];
      $("pricingRows").innerHTML = models.map((m) => {
        const p = pricing[m] || {};
        const val = (k) => (p[k] == null ? "" : p[k]);
        return `
          <div class="pricing-row" data-model="${escapeHtml(m)}">
            <div class="pool-name"><span class="pill model">${escapeHtml(m)}</span></div>
            <div><label>输入（缓存未命中）</label><input data-k="input_per_m" type="number" step="0.001" min="0" value="${escapeHtml(val("input_per_m"))}" placeholder="0.28" /></div>
            <div><label>缓存读</label><input data-k="cache_read_per_m" type="number" step="0.001" min="0" value="${escapeHtml(val("cache_read_per_m"))}" placeholder="0.07" /></div>
            <div><label>输出</label><input data-k="output_per_m" type="number" step="0.001" min="0" value="${escapeHtml(val("output_per_m"))}" placeholder="1.10" /></div>
          </div>`;
      }).join("");
    }

    let newapiProbes = [];
    let newapiUpstreamNames = [];

    function newapiStatusCell(st) {
      if (!st || !st.checked_at) {
        return '<span class="pill disabled">未探测</span>';
      }
      const ts = `<div class="muted" style="font-size:0.72rem;margin-top:3px">${escapeHtml(fmtTs(st.checked_at))} · ${escapeHtml(st.source || "")}</div>`;
      if (st.ok) {
        return `<span class="pill ok">成功 ×${st.ratio}</span>${ts}`;
      }
      const msg = st.enabled === false ? "已停用" : String(st.error || "失败");
      return `<span class="pill error">失败</span><div class="muted" style="font-size:0.72rem;margin-top:3px">${escapeHtml(fmtTs(st.checked_at))} · ${escapeHtml(msg.slice(0, 40))}</div>`;
    }

    function biasLabel(b) {
      const n = Number(b == null ? 0 : b);
      if (n < 0) return '<span class="pill ok">优先 -0.1</span>';
      if (n > 0) return '<span class="pill off">靠后 +0.1</span>';
      return '<span class="pill">同级 0</span>';
    }

    async function loadNewapiProbes() {
      try {
        const d = await api("/api/newapi-probes");
        newapiProbes = d.data || [];
        newapiUpstreamNames = d.upstreams || [];
        renderNewapiProbes();
      } catch (e) { showMsg(e.message, false); }
    }

    function renderNewapiProbes() {
      const tb = $("newapiProbeBody");
      tb.innerHTML = "";
      if (!newapiProbes.length) {
        tb.innerHTML = '<tr><td colspan="9" class="muted">暂无 NewAPI 探测，点击右上角「新增探测」</td></tr>';
        return;
      }
      for (const p of newapiProbes) {
        const st = p.state || {};
        const enabled = p.enabled !== false;
        const tr = document.createElement("tr");
        tr.innerHTML = `
          <td>
            <strong>${escapeHtml(p.name)}</strong>
            <div class="muted" style="font-size:0.72rem;margin-top:3px">
              Token ${p.access_token_set ? "已配置" : "未配置"}
            </div>
          </td>
          <td>${enabled ? '<span class="pill on">启用</span>' : '<span class="pill off">停用</span>'}</td>
          <td><span class="muted">${escapeHtml(p.base_url || "")}</span></td>
          <td><span class="muted">${escapeHtml(p.group || "")}</span></td>
          <td><span class="muted">${escapeHtml(p.upstream_name || "")}</span></td>
          <td>${p.interval_sec || 600} 秒</td>
          <td>${biasLabel(p.priority_bias)}</td>
          <td>${newapiStatusCell(st)}</td>
          <td class="actions">
            <button class="btn-ghost btn-sm" data-act="run">探测</button>
            <button class="btn-ghost btn-sm" data-act="edit">编辑</button>
            <button class="btn-danger btn-sm" data-act="del">删除</button>
          </td>`;
        tr.querySelector('[data-act="run"]').onclick = async (ev) => {
          await runNewapiProbe(p, ev.currentTarget);
        };
        tr.querySelector('[data-act="edit"]').onclick = () => openNewapiProbeModal(p);
        tr.querySelector('[data-act="del"]').onclick = async () => {
          if (!confirm("删除探测「" + p.name + "」？")) return;
          try {
            await api("/api/newapi-probes/" + p.id, { method: "DELETE" });
            showMsg("已删除 " + p.name, true);
            await loadNewapiProbes();
          } catch (e) { showMsg(e.message, false); }
        };
        tb.appendChild(tr);
      }
    }

    function openNewapiProbeModal(p) {
      $("newapiProbeEditId").value = p ? p.id : "";
      $("newapiProbeModalTitle").textContent = p ? "编辑 NewAPI 探测：" + p.name : "新增 NewAPI 探测";
      $("npName").value = p ? p.name : "";
      $("npBaseUrl").value = p ? p.base_url : "";
      $("npGroup").value = p ? p.group : "";
      $("npInterval").value = p ? (p.interval_sec || 600) : 600;
      $("npBias").value = String(Number(p && p.priority_bias != null ? p.priority_bias : 0));
      $("npEnabled").checked = p ? p.enabled !== false : true;
      $("npToken").value = "";
      $("npClearToken").checked = false;
      $("npClearToken").style.display = p && p.access_token_set ? "" : "none";
      $("npToken").placeholder = p && p.access_token_set
        ? ("留空保持不变（当前 " + (p.access_token_masked || "已配置") + "）")
        : "留空则使用公开接口";
      const sel = $("npUpstream");
      sel.innerHTML = "";
      const names = [...new Set([...(newapiUpstreamNames || []), p ? p.upstream_name : ""].filter(Boolean))];
      if (!names.length) {
        const opt = document.createElement("option");
        opt.value = "";
        opt.textContent = "暂无上游，请先在上游管理添加";
        opt.disabled = true;
        opt.selected = true;
        sel.appendChild(opt);
      } else {
        for (const n of names) {
          const opt = document.createElement("option");
          opt.value = n;
          opt.textContent = n;
          if (p && p.upstream_name === n) opt.selected = true;
          sel.appendChild(opt);
        }
      }
      $("newapiProbeModal").style.display = "flex";
      $("npName").focus();
    }

    function closeNewapiProbeModal() {
      $("newapiProbeModal").style.display = "none";
    }

    async function saveNewapiProbeForm() {
      const id = $("newapiProbeEditId").value;
      const payload = {
        name: $("npName").value.trim(),
        enabled: $("npEnabled").checked,
        interval_sec: Math.max(15, Math.min(86400, Math.round(Number($("npInterval").value) || 600))),
        base_url: stripUrlScheme($("npBaseUrl").value.trim()),
        group: $("npGroup").value.trim(),
        upstream_name: $("npUpstream").value.trim(),
        priority_bias: Number($("npBias").value),
      };
      if (!payload.name) { showMsg("请填写名称", false); return; }
      if (!payload.base_url) { showMsg("请填写供应商 Base URL", false); return; }
      if (!payload.group) { showMsg("请填写分组名称", false); return; }
      if (!payload.upstream_name) { showMsg("请选择目标上游", false); return; }
      const token = $("npToken").value.trim();
      if (token) payload.access_token = token;
      if ($("npClearToken").checked) payload.clear_access_token = true;
      try {
        if (id) {
          await api("/api/newapi-probes/" + id, { method: "PUT", body: JSON.stringify(payload) });
          showMsg("已更新探测", true);
        } else {
          await api("/api/newapi-probes", { method: "POST", body: JSON.stringify(payload) });
          showMsg("已新增探测", true);
        }
        closeNewapiProbeModal();
        await loadNewapiProbes();
      } catch (e) { showMsg(e.message, false); }
    }

    async function runNewapiProbe(p, btn) {
      if (!p) return;
      const prev = btn ? btn.textContent : "";
      if (btn) { btn.disabled = true; btn.textContent = "探测中…"; }
      try {
        showMsg("正在探测 " + p.name + " …", true);
        const r = await api("/api/newapi-probes/" + p.id + "/run", { method: "POST" });
        if (r.ok) {
          const synced = r.applied && r.applied.updated;
          showMsg(
            "探测成功：" + p.name + " 倍率 " + r.ratio + "（来源 " + (r.source || "?") + "）" +
            (synced ? "，已同步上游" : "，上游未变化"),
            true
          );
        } else {
          showMsg("探测失败：" + (r.error || "未知错误"), false);
        }
        await Promise.all([loadNewapiProbes(), loadUpstreams(), loadOverview()]);
      } catch (e) { showMsg(e.message, false); }
      finally {
        if (btn) { btn.disabled = false; btn.textContent = prev || "探测"; }
      }
    }

    // ---------- 公网调用 ----------

    let publicSettings = {};

    function splitIpTextarea(el) {
      return String(el.value || "")
        .split(/[\n,;]+/)
        .map((s) => s.trim())
        .filter(Boolean);
    }

    function publicPayload() {
      return {
        enabled: $("publicEnabled").checked,
        public_url: $("publicUrlInput").value.trim(),
        mode: $("publicMode").value,
        allow_loopback: $("publicAllowLoopback").checked,
        trust_proxy_headers: $("publicTrustProxy").checked,
        blocked: splitIpTextarea($("publicBlocked")),
        allowed: splitIpTextarea($("publicAllowed")),
      };
    }

    function renderPublicStatus() {
      const pill = $("publicStatusPill");
      if (!pill) return;
      pill.className = "pill " + (publicSettings.enabled ? "ok" : "off");
      pill.textContent = publicSettings.enabled ? "已启用" : "已关闭";
    }

    async function loadPublicAccess() {
      try {
        const range = $("publicRange").value || "7d";
        const q = $("publicQ").value.trim();
        const params = new URLSearchParams({ range });
        if (q) params.set("q", q);
        const [settings, stats] = await Promise.all([
          api("/api/public/settings"),
          api("/api/public/ip-stats?" + params.toString()),
        ]);
        publicSettings = settings;
        $("publicEnabled").checked = !!settings.enabled;
        $("publicUrlInput").value = settings.public_url || "";
        $("publicKeyText").textContent = settings.key || "（未配置）";
        $("publicKeyText").title = settings.key || "";
        $("publicMode").value = settings.mode || "blacklist";
        $("publicAllowLoopback").checked = settings.allow_loopback !== false;
        $("publicTrustProxy").checked = !!settings.trust_proxy_headers;
        $("publicBlocked").value = (settings.blocked || []).join("\n");
        $("publicAllowed").value = (settings.allowed || []).join("\n");
        renderPublicStatus();
        renderPublicIpStats(stats.data || []);
      } catch (e) { showMsg(e.message, false); }
    }

    function renderPublicIpStats(rows) {
      const tb = $("publicIpBody");
      tb.innerHTML = "";
      if (!rows.length) {
        tb.innerHTML = '<tr><td colspan="10" class="muted">暂无公网调用记录</td></tr>';
        return;
      }
      const mode = publicSettings.mode || "blacklist";
      for (const r of rows) {
        const blocked = !!r.blocked;
        const tr = document.createElement("tr");
        const statusHtml = blocked
          ? '<span class="pill error">已拦截</span>'
          : (mode === "whitelist" && (publicSettings.allowed || []).some((x) => x === r.ip))
            ? '<span class="pill ok">白名单</span>'
            : '<span class="pill ok">放行</span>';
        const actions = [];
        const isBlocked = (publicSettings.blocked || []).some((x) => x === r.ip);
        const isAllowedRule = (publicSettings.allowed || []).some((x) => x === r.ip);
        actions.push(`<button class="btn-danger btn-sm" data-ip="${escapeHtml(r.ip)}" data-act="block" type="button">放入黑名单</button>`);
        actions.push(`<button class="btn-ghost btn-sm" data-ip="${escapeHtml(r.ip)}" data-act="allow" type="button">放入白名单</button>`);
        if (isBlocked || isAllowedRule) {
          actions.push(`<button class="btn-ghost btn-sm" data-ip="${escapeHtml(r.ip)}" data-act="unblock" type="button">解除</button>`);
        }
        tr.innerHTML = `
          <td class="mono">${escapeHtml(r.ip)}</td>
          <td>${statusHtml}</td>
          <td>${r.requests}</td>
          <td class="ok">${r.success}</td>
          <td class="err">${r.errors}</td>
          <td class="mono">${fmtTok(r.total_tokens)}</td>
          <td class="mono">${fmtCost(r.cost_usd)}</td>
          <td class="mono">${fmtRealCost(r.real_cost_cny)}</td>
          <td class="muted" style="font-size:0.75rem">${escapeHtml(fmtTs(r.first_seen) + " / " + fmtTs(r.last_seen))}</td>
          <td class="actions">${actions.join("")}</td>`;
        tr.querySelectorAll("button[data-act]").forEach((btn) => {
          btn.onclick = () => quickPublicIpAction(btn.dataset.ip, btn.dataset.act);
        });
        tb.appendChild(tr);
      }
    }

    async function savePublicSettings() {
      try {
        await api("/api/public/settings", {
          method: "PUT",
          body: JSON.stringify(publicPayload()),
        });
        showMsg("公网调用设置已保存", true);
        await loadPublicAccess();
      } catch (e) { showMsg(e.message, false); }
    }

    async function quickPublicIpAction(ip, action) {
      const p = publicPayload();
      p.blocked = p.blocked.filter((x) => x !== ip);
      p.allowed = p.allowed.filter((x) => x !== ip);
      if (action === "block") p.blocked.push(ip);
      if (action === "allow") p.allowed.push(ip);
      try {
        await api("/api/public/settings", {
          method: "PUT",
          body: JSON.stringify(p),
        });
        showMsg("IP 规则已更新：" + ip, true);
        await loadPublicAccess();
      } catch (e) { showMsg(e.message, false); }
    }

    function renderCodexStatus() {
      const pill = $("codexModePill");
      if (!pill) return;
      const st = codexStatus || {};
      const mode = st.mode || "";
      let text, cls;
      if (mode === "local-direct") { text = "本机原配置"; cls = "ok"; }
      else if (mode === "openai-all") { text = "openai（走 4100）"; cls = "ok"; }
      else if (mode === "deepseek") { text = (st.config_model || "deepseek") + "（走 4100）"; cls = "ok"; }
      else if (mode === "routing-only") { text = "仅路由（未改配置）"; cls = "disabled"; }
      else { text = "未配置"; cls = ""; }
      pill.className = "pill" + (cls ? " " + cls : "");
      pill.textContent = text;
      const label = $("codexModelLabel");
      if (label) {
        const m = st.config_model || st.provider || "";
        label.textContent = m || "";
      }
      const changes = $("codexChanges");
      if (changes) {
        changes.textContent =
          (mode ? "当前模式：" + text : "未介入配置") +
          (st.applied_at ? " · 最近应用 " + fmtTs(st.applied_at) : "");
      }
    }

    function renderClaudeStatus() {
      const pill = $("claudeModePill");
      if (!pill) return;
      const st = claudeStatus || {};
      const mode = st.mode || "";
      let text, cls;
      if (mode === "local-direct") { text = "本机原配置"; cls = "ok"; }
      else if (mode === "openai-all") { text = "openai（走 4100）"; cls = "ok"; }
      else if (mode === "deepseek") { text = (st.config_model || "deepseek") + "（走 4100）"; cls = "ok"; }
      else { text = "未配置"; cls = ""; }
      pill.className = "pill" + (cls ? " " + cls : "");
      pill.textContent = text;
      const label = $("claudeModelLabel");
      if (label) {
        label.textContent = st.settings_exists
          ? "base: " + (st.config_base_url || "")
          : "settings.json 不存在";
      }
      const changes = $("claudeChanges");
      if (changes) {
        changes.textContent =
          (mode ? "当前模式：" + text : "未介入配置") +
          (st.applied_at ? " · 最近应用 " + fmtTs(st.applied_at) : "");
      }
      const tgl = $("bridgeToggle");
      if (tgl) tgl.checked = !!(st.bridge && st.bridge.installed);
      const info = $("bridgeInfo");
      if (info) {
        if (!st.bridge) info.textContent = "";
        else if (st.bridge.installed)
          info.textContent = "hook 已装" + (st.bridge.rules_present ? "" : " · rules.json 缺失") + (st.bridge.command ? " · " + st.bridge.command : "");
        else info.textContent = "hook 未安装";
      }
    }

    $("btnApplyClaude").onclick = async () => {
      const v = ($("claudeModel").value || "local-direct").trim();
      const isDeepseek = v !== "local-direct" && v !== DEFAULT_MODEL;
      const mode = v === "local-direct" ? "local-direct" : isDeepseek ? "deepseek" : "openai-all";
      const payload = { mode };
      if (mode === "deepseek") payload.model = v;
      try {
        showMsg("正在应用 Claude Code 配置 " + v + " …", true);
        const r = await api("/api/claude/config", { method: "PUT", body: JSON.stringify(payload) });
        claudeStatus = r.claude || {};
        if (r.active_model) activeModel = r.active_model;
        const changes = (r.changes || []).map((x) => "· " + x).join("\n");
        const modeText =
          mode === "local-direct" ? "已恢复本机原配置" :
          mode === "openai-all" ? "已配置走 4100（openai 池）" : "已配置走 4100（" + v + " 池）";
        showMsg("Claude Code：" + modeText + "\n" + changes + "\n请新开 Claude Code 会话使配置生效。", true);
        renderClaudeStatus();
        await refreshModels();
      } catch (e) { showMsg(e.message, false); }
    };

    $("bridgeToggle").onchange = async () => {
      const tgl = $("bridgeToggle");
      const target = tgl.checked;
      try {
        showMsg("正在" + (target ? "安装" : "移除") + " auto-mode hook …", true);
        const r = await api("/api/claude/bridge", { method: "PUT", body: JSON.stringify({ enabled: target }) });
        claudeStatus = r.claude || claudeStatus || {};
        renderClaudeStatus();
        const changes = (r.changes || []).map((x) => "· " + x).join("\n");
        showMsg((target ? "已安装 auto-mode hook" : "已移除 auto-mode hook") + "\n" + changes + "\n请新开 Claude Code 会话生效。", true);
      } catch (e) {
        tgl.checked = !target; // 失败回滚
        showMsg(e.message, false);
      }
    };

    $("btnApplyActive").onclick = async () => {
      const m = ($("activeModel").value || DEFAULT_MODEL).trim();
      try {
        showMsg("正在应用 " + m + " 并同步 Codex 配置…", true);
        const r = await api("/api/active-model", {
          method: "PUT",
          body: JSON.stringify({ active_model: m }),
        });
        activeModel = r.active_model;
        codexStatus = r.codex_status || r.codex || {};
        const mode = (r.codex && r.codex.mode) || "";
        const extra =
          mode === "local-direct"
            ? "\nCodex: 已恢复本机原配置，直连不经 4100"
            : mode === "deepseek"
            ? "\nCodex: 已按官方规则写入 model/reasoning/catalog + models.json"
            : mode === "openai-all"
              ? "\nCodex: 已配置走 4100（openai 池）+ auth.json"
              : mode === "routing-only"
                ? "\nCodex: 未改动（仅路由）"
                : "";
        showMsg(
          "已切换 → " + activeModel + "（池内 " + r.upstreams_in_scope + " 条）" + extra +
          "\n请新开 Codex 会话使配置生效。",
          true
        );
        await refreshModels();
        await Promise.all([loadUpstreams(), loadOverview()]);
      } catch (e) { showMsg(e.message, false); }
    };

    $("btnSave").onclick = async () => {
      const id = $("editId").value;
      const model = resolveTypeModel();
      const payload = {
        name: $("name").value.trim(),
        base_url: storedUrl($("base_url").value),
        priority: Number($("priority").value || 100),
        enabled: $("enabled").checked,
        chat_completions: $("chatCompletions").checked,
        anthropic_messages: $("anthropicMessages").checked,
        model,
        model_map: collectModelMap(),
      };
      const multiplier = $("multiplier").value.trim();
      if (multiplier !== "") payload.multiplier = Number(multiplier);
      const key = $("api_key").value.trim();
      try {
        if (!payload.name) throw new Error("请填写名称");
        if (!payload.base_url) throw new Error("请填写 Base URL");
        if ($("upstreamType").value === "custom" && !payload.model) {
          throw new Error("自定义上游必须填写模型名");
        }
        if (id) {
          if (key) payload.api_key = key;
          await api("/api/upstreams/" + id, { method: "PUT", body: JSON.stringify(payload) });
          showMsg("已更新（pool=" + model + "）", true);
        } else {
          if (!key) throw new Error("新增时必须填写 API Key");
          payload.api_key = key;
          await api("/api/upstreams", { method: "POST", body: JSON.stringify(payload) });
          showMsg("已新增（pool=" + model + "）", true);
        }
        resetUpstreamForm();
        closeUpstreamModal();
        await refreshModels();
        await Promise.all([loadUpstreams(), loadOverview()]);
      } catch (e) { showMsg(e.message, false); }
    };

    $("btnAddUpstream").onclick = () => openUpstreamModal();
    $("btnUpstreamClose").onclick = closeUpstreamModal;
    $("btnUpstreamCancel").onclick = closeUpstreamModal;
    $("upstreamType").onchange = syncUpstreamType;
    $("base_url").addEventListener("blur", () => {
      $("base_url").value = stripUrlScheme($("base_url").value);
    });
