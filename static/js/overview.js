    function attemptsTag(attempts) {
      if (!attempts || !attempts.length) return "";
      const tip = attempts
        .map((a) => `${a.upstream || "?"}:${a.status == null ? "ERR" : a.status}`)
        .join(" → ");
      return `<span class="pill warn" title="失败尝试：${escapeHtml(tip)}">已切换</span>`;
    }

    function statusPill(st, errorLogId, attempts) {
      if (st == null) return '<span class="pill off">—</span>';
      const ok = Number(st) < 400;
      const cls = ok ? "on" : "off";
      const tag = attemptsTag(attempts);
      if (errorLogId) {
        return `<a class="pill ${cls}" href="#" data-error-id="${escapeHtml(String(errorLogId))}" title="查看错误日志详情">${escapeHtml(st)}</a>${tag}`;
      }
      return `<span class="pill ${cls}">${escapeHtml(st)}</span>${tag}`;
    }

    function healthStatusPill(u) {
      const status = u.status || (u.enabled ? "ok" : "disabled");
      if (status === "disabled" || !u.enabled) {
        return '<span class="pill disabled">停用</span>';
      }
      if (status === "error") {
        const tip = u.probe_error
          ? String(u.probe_error).slice(0, 120)
          : (u.probe_status_code != null ? "HTTP " + u.probe_status_code : "探测失败");
        return `<span class="pill error" title="${escapeHtml(tip)}">错误</span>`;
      }
      const tip = u.probe_checked_at
        ? "最近手动测试 " + fmtTs(u.probe_checked_at)
        : "尚未手动测试";
      return `<span class="pill ok" title="${escapeHtml(tip)}">正常</span>`;
    }

    function fmtMultShort(n) {
      if (n == null || n === "") return "—";
      const s = Number(n).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
      return s || "0";
    }

    function lightLabel(light) {
      if (light === "green") return "绿灯 · 低倍率可用";
      if (light === "yellow") return "黄灯 · 仅高倍率可用";
      if (light === "red") return "红灯 · 全部失败";
      if (light === "disabled") return "已关闭自动探测";
      return "尚未探测";
    }

    function renderModelBoard(payload) {
      const box = $("modelBoard");
      const meta = $("modelAvailMeta");
      if (!box) return;
      const items = (payload && payload.data) || [];
      const thr = Number((payload && payload.threshold) || 0.1);
      const nextSec = payload && payload.next_boundary_sec;
      if (meta) {
        const enabled = items.filter((it) => it.probe_enabled !== false);
        const nextMin = nextSec == null ? "—" : Math.max(0, Math.ceil(Number(nextSec) / 60));
        meta.textContent =
          `启用 ${enabled.length}/${items.length} · 低优先级优先 · <${thr} 绿 / ≥${thr} 黄` +
          (nextSec == null ? "" : ` · 下次约 ${nextMin} 分钟后`);
      }
      if (!items.length) {
        box.innerHTML = '<div class="muted">暂无模型</div>';
        return;
      }
      const headRow = `
        <div class="model-board-head-row">
          <span>模型</span>
          <span>倍率 / 上游</span>
          <span>间隔</span>
          <span>最近更新</span>
          <span>状态</span>
          <span>操作</span>
        </div>`;
      box.innerHTML = headRow + items.map((it) => {
        const enabled = it.probe_enabled !== false;
        const light = enabled ? (it.light || "unknown") : "disabled";
        let multText;
        let statusTip = "";
        if (!enabled) {
          if (it.ok) {
            multText = `<strong>×${escapeHtml(fmtMultShort(it.multiplier))}</strong>` +
              (it.upstream ? ` · ${escapeHtml(it.upstream)}` : "");
            statusTip = "手动探测 · 倍率 ×" + fmtMultShort(it.multiplier) +
              (it.upstream ? " · 上游 " + it.upstream : "");
          } else if (it.ok === false) {
            multText = it.error || "全部失败";
            statusTip = "手动探测 · " + (it.error || "全部失败");
          } else {
            multText = "探测已关闭";
            statusTip = "已关闭自动探测";
          }
        } else if (it.ok) {
          multText = `<strong>×${escapeHtml(fmtMultShort(it.multiplier))}</strong>` +
            (it.upstream ? ` · ${escapeHtml(it.upstream)}` : "");
          statusTip = "倍率 ×" + fmtMultShort(it.multiplier) +
            (it.upstream ? " · 上游 " + it.upstream : "");
        } else if (it.ok === false) {
          multText = it.error || "全部失败";
          statusTip = it.error || "全部失败";
        } else {
          multText = "探测中…";
          statusTip = "探测中…";
        }
        const intervalMin = Math.max(1, Math.round(Number(it.interval_sec || 300) / 60));
        const checked = it.checked_at ? fmtTs(it.checked_at) : "";
        const tip = lightLabel(light) +
          ` · 间隔 ${intervalMin} 分钟` +
          (it.attempts ? ` · 尝试 ${it.attempts} 次` : "") +
          (it.duration_ms != null ? ` · ${Math.round(it.duration_ms)}ms` : "");
        const dotStyle = light !== "disabled" && it.color
          ? ` style="background:${escapeHtml(it.color)};box-shadow:0 0 0 3px ${escapeHtml(it.color)}33,0 0 14px ${escapeHtml(it.color)}aa"`
          : "";
        return `
          <div class="model-card" title="${escapeHtml(tip)}">
            <div class="mc-name" title="${escapeHtml(it.model || "")}">${escapeHtml(it.model || "")}</div>
            <div class="mc-status" title="${escapeHtml(statusTip)}">${multText}</div>
            <div class="mc-interval">每 ${intervalMin} 分</div>
            <div class="mc-updated">${escapeHtml(checked)}</div>
            <span class="traffic-light ${escapeHtml(light)}"${dotStyle} aria-label="${escapeHtml(lightLabel(light))}"></span>
            <button class="btn btn-sm btn-probe" type="button" data-probe-model="${escapeHtml(it.model || "")}">单独探测</button>
          </div>`;
      }).join("");
    }

    async function loadModelAvailability() {
      try {
        const data = await api("/api/model-availability");
        renderModelBoard(data);
      } catch (e) {
        const box = $("modelBoard");
        if (box) box.innerHTML = `<div class="muted">可用性加载失败：${escapeHtml(e.message)}</div>`;
      }
    }

    function fmtAvailRate(rate) {
      if (rate == null) return { text: "", cls: "na" };
      const pct = Math.round(Number(rate) * 1000) / 10;
      let cls = "good";
      if (pct < 90) cls = "mid";
      if (pct < 70) cls = "bad";
      const text = (Number.isInteger(pct) ? String(pct) : pct.toFixed(1)) + "%";
      return { text, cls };
    }

    function fmtHistRange(startIso, endIso) {
      const s = String(startIso || "");
      const e = String(endIso || "");
      // Expect ISO-like: 2026-08-02T17:00:00+08:00
      const sd = s.slice(0, 10);
      const st = s.slice(11, 16);
      const et = e.slice(11, 16);
      if (!sd || !st) return s || "";
      return `${sd} ${st}-${et || ""}`;
    }

    function fmtHistMult(v) {
      if (v == null || v === "") return "";
      const n = Number(v);
      if (!Number.isFinite(n)) return "";
      return "×" + n.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    }

    function renderAvailabilityHistory(payload) {
      const box = $("histRows");
      const meta = $("histMeta");
      if (!box) return;
      const items = (payload && payload.data) || [];
      const bucketSec = Number((payload && payload.bucket_sec) || 0);
      const bucketCount = Number((payload && payload.bucket_count) || 0);
      if (meta) {
        const label =
          bucketSec >= 3600 ? (bucketSec / 3600) + " 小时/格" :
          bucketSec >= 60 ? (bucketSec / 60) + " 分钟/格" :
          bucketSec + " 秒/格";
        meta.textContent =
          `${bucketCount} 格 · ${label} · ≥95%按倍率 gre→橙 · 80–95%深橙 · <80%红`;
      }
      if (!items.length) {
        box.innerHTML = '<div class="muted">暂无历史数据（探测后自动累积）</div>';
        return;
      }
      box.innerHTML = items.map((it) => {
        const rate = fmtAvailRate(it.availability_rate);
        const cells = (it.cells || []).map((c) => {
          const st = c.state || "empty";
          const range = fmtHistRange(c.start, c.end);
          const mult = fmtHistMult(c.avg_multiplier);
          const reqs = c.request_count != null ? Number(c.request_count) : 0;
          const sr = c.success_rate;
          const srText = sr == null ? "" : (Math.round(Number(sr) * 1000) / 10) + "%";
          const status =
            st === "up" ? "健康(≥95%)" :
            st === "mid" ? "中等错误(80–95%)" :
            st === "down" ? "高错误(<80%)" : "无记录";
          const tip =
            `${range}\n成功率 ${srText}\n平均倍率 ${mult}\n请求 ${reqs} 次\n${status}`;
          let style = "";
          if (c.color && (st === "up" || st === "mid" || st === "down")) {
            style = ` style="background:${escapeHtml(c.color)};box-shadow:0 0 8px ${escapeHtml(c.color)}55"`;
          }
          return `<span class="hist-cell ${escapeHtml(st)}"${style} title="${escapeHtml(tip)}"></span>`;
        }).join("");
        const avgRow = it.avg_multiplier != null
          ? ` · 均价 ${fmtHistMult(it.avg_multiplier)}`
          : "";
        const sub = (it.samples || it.request_count)
          ? `采样 ${it.samples || 0} · 请求 ${it.request_count || 0}${avgRow}`
          : "尚无采样";
        return `
          <div class="hist-row">
            <div>
              <div class="hist-name">${escapeHtml(it.model || "")}</div>
              <div class="hist-sub">${escapeHtml(sub)}</div>
            </div>
            <div class="hist-bar" role="img" aria-label="${escapeHtml(it.model || "")} 可用性">${cells}</div>
            <div class="hist-rate ${rate.cls}">${rate.text}</div>
          </div>`;
      }).join("");
    }

    async function loadAvailabilityHistory() {
      const range = ($("histRange") && $("histRange").value) || "24h";
      const box = $("histRows");
      if (box && !box.dataset.loaded) {
        box.innerHTML = '<div class="muted">加载中…</div>';
      }
      try {
        const data = await api("/api/availability-history?range=" + encodeURIComponent(range));
        renderAvailabilityHistory(data);
        if (box) box.dataset.loaded = "1";
      } catch (e) {
        if (box) box.innerHTML = `<div class="muted">加载失败：${escapeHtml(e.message)}</div>`;
      }
    }

    function closeProbeSettingsModal() {
      $("probeSettingsModal").style.display = "none";
    }

    function openProbeSettingsModal() {
      $("probeSettingsModal").style.display = "flex";
      loadProbeSettingsForm();
    }

    async function loadProbeSettingsForm() {
      const box = $("probeSettingsRows");
      box.innerHTML = '<div class="muted">加载中…</div>';
      try {
        const data = await api("/api/model-availability/settings");
        const items = data.data || [];
        if (!items.length) {
          box.innerHTML = '<div class="muted">暂无模型</div>';
          return;
        }
        box.innerHTML = items.map((it) => {
          const enabled = !!it.probe_enabled;
          const interval = Number(it.interval_sec || 300);
          return `
            <div class="probe-settings-row" data-model="${escapeHtml(it.model)}">
              <div class="ps-name-wrap">
                <div class="ps-name">${escapeHtml(it.model)}</div>
                <div class="ps-pool">池 ${escapeHtml(it.pool || "")}</div>
              </div>
              <input type="number" min="60" max="86400" step="60" data-k="interval"
                value="${escapeHtml(interval)}" title="秒，建议 60 的倍数" />
              <label class="ps-toggle">
                <input type="checkbox" data-k="enabled" ${enabled ? "checked" : ""} />
                启用
              </label>
            </div>`;
        }).join("");
      } catch (e) {
        box.innerHTML = `<div class="muted">加载失败：${escapeHtml(e.message)}</div>`;
      }
    }

    async function saveProbeSettingsForm() {
      const rows = document.querySelectorAll("#probeSettingsRows .probe-settings-row");
      const models = {};
      rows.forEach((row) => {
        const model = row.dataset.model;
        if (!model) return;
        const enabled = !!row.querySelector('input[data-k="enabled"]')?.checked;
        let interval = Number(row.querySelector('input[data-k="interval"]')?.value || 300);
        if (!Number.isFinite(interval) || interval < 60) interval = 60;
        if (interval > 86400) interval = 86400;
        models[model] = { enabled, interval_sec: Math.round(interval) };
      });
      try {
        await api("/api/model-availability/settings", {
          method: "PUT",
          body: JSON.stringify({ models }),
        });
        showMsg("探测设置已保存", true);
        closeProbeSettingsModal();
        await loadModelAvailability();
      } catch (e) {
        showMsg(e.message, false);
      }
    }

    const BEIJING_DATE_FORMATTER = new Intl.DateTimeFormat("en-CA", {
      timeZone: "Asia/Shanghai",
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      second: "2-digit",
      hourCycle: "h23",
    });

    function fmtTs(ts) {
      const raw = String(ts || "").trim();
      if (!raw) return "—";
      const value = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(raw) ? raw : raw + "+08:00";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return raw.replace("T", " ").slice(0, 19);
      const parts = BEIJING_DATE_FORMATTER.formatToParts(date);
      const part = (type) => parts.find((item) => item.type === type)?.value || "";
      return `${part("year")}-${part("month")}-${part("day")} ${part("hour")}:${part("minute")}:${part("second")}`;
    }

    const PIE_COLORS = [
      "#4f8cff", "#34d399", "#fbbf24", "#f87171", "#22d3ee",
      "#a78bfa", "#fb7185", "#a3e635", "#f59e0b", "#60a5fa",
    ];

    function positiveChartItems(items) {
      return items
        .map((item) => Object.assign({}, item, {
          label: String(item.label),
          value: Number(item.value),
        }))
        .filter((item) => Number.isFinite(item.value) && item.value > 0)
        .sort((a, b) => b.value - a.value);
    }

    function chartPercent(value, total) {
      const percent = value / total * 100;
      return percent >= 10 ? percent.toFixed(0) : percent.toFixed(1);
    }

    function renderModelLegend(values, total) {
      const rows = values.map((item, index) => {
        const color = PIE_COLORS[index % PIE_COLORS.length];
        return `<tr>
          <td><span class="chart-detail-name"><span class="chart-legend-swatch" style="background:${color}"></span>${escapeHtml(item.label)}</span></td>
          <td class="chart-detail-amount">${escapeHtml(fmtTotalCny(item.amount || 0))}</td>
          <td>${chartPercent(item.value, total)}%</td>
          <td>${fmtNum(item.calls || 0)}次</td>
          <td>${fmtTok(item.tokens || 0)}</td>
        </tr>`;
      }).join("");
      return `<table class="chart-detail-table" aria-label="实际模型用量明细">
        <colgroup><col /><col /><col /><col /><col /></colgroup>
        <thead><tr><th scope="col">实际模型</th><th scope="col">金额</th><th scope="col">占比</th><th scope="col">次数</th><th scope="col">Tokens</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    }

    function renderTokenTable(values, total) {
      const rows = values.map((item, index) => {
        const color = PIE_COLORS[index % PIE_COLORS.length];
        return `<tr>
          <td><span class="chart-detail-name"><span class="chart-legend-swatch" style="background:${color}"></span>${escapeHtml(item.label)}</span></td>
          <td class="chart-detail-amount">${escapeHtml(fmtTok(item.value))}</td>
          <td>${chartPercent(item.value, total)}%</td>
        </tr>`;
      }).join("");
      return `<table class="chart-detail-table" aria-label="输入输出缓存明细">
        <colgroup><col /><col /><col /></colgroup>
        <thead><tr><th scope="col">类型</th><th scope="col">Tokens</th><th scope="col">占比</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    }

    function renderUpstreamTable(values, total) {
      const rows = values.map((item, index) => {
        const color = PIE_COLORS[index % PIE_COLORS.length];
        return `<tr>
          <td><span class="chart-detail-name"><span class="chart-legend-swatch" style="background:${color}"></span>${escapeHtml(item.label)}</span></td>
          <td class="chart-detail-amount">${escapeHtml(fmtTotalCny(item.value))}</td>
          <td>${chartPercent(item.value, total)}%</td>
          <td>${fmtNum(item.calls || 0)}次</td>
          <td>${fmtTok(item.tokens || 0)}</td>
        </tr>`;
      }).join("");
      return `<table class="chart-detail-table" aria-label="上游成本占比明细">
        <colgroup><col /><col /><col /><col /><col /></colgroup>
        <thead><tr><th scope="col">上游</th><th scope="col">成本</th><th scope="col">占比</th><th scope="col">次数</th><th scope="col">Tokens</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
    }

    function renderPieCard(title, items, centerValue, centerLabel, valueFormatter, legendRenderer) {
      const values = positiveChartItems(items);
      if (!values.length) {
        return `<div class="card chart-card"><h2>${escapeHtml(title)}</h2><div class="chart-empty">暂无可用数据</div></div>`;
      }

      const total = values.reduce((sum, item) => sum + item.value, 0);
      let cursor = 0;
      const segments = values.map((item, index) => {
        const start = cursor;
        cursor += item.value / total * 100;
        return `${PIE_COLORS[index % PIE_COLORS.length]} ${start.toFixed(3)}% ${cursor.toFixed(3)}%`;
      });
      const legend = values.map((item, index) => {
        const color = PIE_COLORS[index % PIE_COLORS.length];
        const percent = chartPercent(item.value, total);
        return `<div class="chart-legend-item">
          <span class="chart-legend-swatch" style="background:${color}"></span>
          <span class="chart-legend-label">${escapeHtml(item.label)}</span>
          <span class="chart-legend-value"><span>${escapeHtml(valueFormatter(item.value))}</span><span class="chart-legend-percent">${percent}%</span></span>
        </div>`;
      }).join("");
      const aria = `${title}：${values.map((item) => `${item.label} ${chartPercent(item.value, total)}%`).join("，")}`;
      return `<div class="card chart-card">
        <h2>${escapeHtml(title)}</h2>
        <div class="chart-body">
          <div class="pie-shell">
            <div class="pie-chart" style="--pie:conic-gradient(${segments.join(",")})" role="img" aria-label="${escapeHtml(aria)}">
              <div class="pie-center"><strong>${escapeHtml(centerValue)}</strong><span>${escapeHtml(centerLabel)}</span></div>
            </div>
          </div>
          <div class="chart-legend">${legendRenderer ? legendRenderer(values, total, valueFormatter) : legend}</div>
        </div>
      </div>`;
    }

    function renderOverviewCharts(stats) {
      const modelBreakdown = stats.model_breakdown || {};
      const modelItems = Object.entries(modelBreakdown).map(([label, detail]) => ({
        label,
        value: detail.total_tokens || 0,
        amount: detail.cost_cny || 0,
        calls: detail.calls || 0,
        tokens: detail.total_tokens || 0,
      }));
      const uncachedInput = Math.max(
        0,
        Number(stats.total_input_tokens || 0) - Number(stats.total_cached_tokens || 0)
      );
      const cachedInput = Number(stats.total_cached_tokens || 0);
      // output_tokens 已包含 reasoning/thinking，避免双计。
      const output = Number(stats.total_output_tokens || 0);
      const tokenItems = [
        { label: "输入（未缓存）", value: uncachedInput },
        { label: "缓存读", value: cachedInput },
        { label: "输出", value: output },
      ];
      const upstreamCostItems = Object.entries(stats.upstream_breakdown || {}).map(([label, detail]) => ({
        label,
        value: detail.cost_cny || 0,
        amount: detail.cost_cny || 0,
        calls: detail.calls || 0,
        tokens: detail.total_tokens || 0,
      }));
      const modelTotal = modelItems.reduce((sum, item) => sum + Number(item.value || 0), 0);
      const tokenTotal = tokenItems.reduce((sum, item) => sum + item.value, 0);
      const upstreamCostTotal = upstreamCostItems.reduce((sum, item) => sum + Number(item.value || 0), 0);
      $("overviewCharts").innerHTML = [
        renderPieCard("模型用量占比", modelItems, fmtTok(modelTotal), "Token 总量", fmtTok, renderModelLegend),
        renderPieCard("输入输出缓存", tokenItems, fmtTok(tokenTotal), "Token 总量", fmtTok, renderTokenTable),
        renderPieCard("上游成本占比", upstreamCostItems, fmtTotalCny(upstreamCostTotal), "总成本", fmtTotalCny, renderUpstreamTable),
      ].join("");
    }

    function renderStats(health, stats) {
      const rate = stats.success_rate == null ? "" : Math.round(stats.success_rate * 100) + "%";
      const rateCls =
        stats.success_rate == null ? "na"
        : stats.success_rate >= 0.95 ? "ok"
        : stats.success_rate >= 0.80 ? "warn"
        : "err";
      const upstreamCards = [
        { l: "上游总数", v: fmtNum(health.upstreams_total), c: "" },
        { l: "启用上游", v: fmtNum(health.upstreams_enabled), c: "" },
        { l: "池内上游", v: fmtNum(health.upstreams_in_scope), c: "" },
      ];
      const requestCards = [
        { l: "请求总数", v: fmtNum(stats.total), c: "" },
        { l: "成功率", v: rate, c: rateCls },
        { l: "输入(非缓存)", v: fmtTok(Math.max(0, (stats.total_input_tokens || 0) - (stats.total_cached_tokens || 0))), c: "" },
        { l: "缓存", v: fmtTok(stats.total_cached_tokens), c: "" },
        { l: "输出(含推理)", v: fmtTok((stats.total_output_tokens || 0) + (stats.total_reasoning_tokens || 0)), c: "" },
        { l: "总 Tokens", v: fmtTok(stats.total_tokens), c: "" },
        { l: "缓存命中率", v: stats.cache_hit_rate == null ? "" : (stats.cache_hit_rate * 100).toFixed(1) + "%", c: "" },
        { l: "平均首字", v: fmtDur(stats.avg_ttft_ms), c: "" },
        { l: "平均用时", v: fmtDur(stats.avg_duration_ms), c: "" },
        { l: "平均TPS", v: fmtTps(stats.avg_tps), c: "" },
        { l: "总费用", v: fmtMoney(stats.total_cost), c: "" },
        { l: "总成本", v: fmtTotalCny(stats.total_real_cost_cny), c: "" },
      ];
      const render = (cards) => cards.map((c) =>
        `<div class="stat"><div class="lbl">${c.l}</div><div class="num ${c.c}">${c.v}</div></div>`
      ).join("");
      $("statUpstreams").innerHTML = render(upstreamCards);
      $("statRequests").innerHTML = render(requestCards);
      renderOverviewCharts(stats);
    }
