    const DEFAULT_MODEL = "openai";
    const LOCAL_DIRECT = "local-direct";
    const DEEPSEEK_POOL = "deepseek";
    const DEEPSEEK_CLIENT_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro"];
    const GROK_POOL = "grok";
    const GROK_CLIENT_MODELS = ["grok-4.6"];
    const PAGE_SIZE = 100;
    const $ = (id) => document.getElementById(id);
    const msg = $("msg");

    let knownModels = [DEFAULT_MODEL];
    let knownPools = [DEFAULT_MODEL, DEEPSEEK_POOL];
    let activeModel = DEFAULT_MODEL;
    let modelCounts = {};
    let modelSync = {};
    let codexStatus = {};
    let claudeStatus = {};
    let grokStatus = {};
    let logOffset = 0;
    let logTotal = 0;
    let errOffset = 0;
    let errTotal = 0;
    let pricing = {};
    let pricingModels = [];

    function showMsg(text, ok) {
      msg.className = "banner " + (ok ? "ok" : "err");
      msg.replaceChildren();
      const raw = String(text == null ? "" : text);
      const looksLikeHtml = /<!doctype\s+html|<html[\s>]/i.test(raw);
      const needsCollapse = !ok && (
        looksLikeHtml || raw.length > 600 || raw.split("\n").length > 10
      );
      if (!needsCollapse) {
        msg.textContent = raw;
      } else {
        const summary = document.createElement("span");
        summary.textContent = looksLikeHtml
          ? "上游返回了 HTML 网关错误（可能是 Cloudflare 502）"
          : raw.slice(0, 600) + (raw.length > 600 ? "…" : "");
        msg.appendChild(summary);

        const details = document.createElement("details");
        details.className = "banner-details";
        const label = document.createElement("summary");
        label.textContent = "展开详情";
        const body = document.createElement("pre");
        body.textContent = raw.slice(0, 65536) + (
          raw.length > 65536 ? "\n...[已截断]" : ""
        );
        details.append(label, body);
        msg.appendChild(details);
      }
      if (ok) setTimeout(() => { msg.className = "banner"; }, 4000);
    }

    function getKey() {
      return localStorage.getItem("sy_master_key") || localStorage.getItem("sr_master_key") || "";
    }

    function setKey(token) {
      if (token) {
        localStorage.setItem("sy_master_key", token);
        localStorage.removeItem("sr_master_key");
      } else {
        localStorage.removeItem("sy_master_key");
        localStorage.removeItem("sr_master_key");
      }
    }

    async function api(path, opts = {}) {
      const key = getKey();
      const headers = Object.assign(
        { "Content-Type": "application/json" },
        opts.headers || {}
      );
      if (key) headers["Authorization"] = "Bearer " + key;
      const res = await fetch(path, Object.assign({}, opts, { headers }));
      const text = await res.text();
      let data;
      try { data = JSON.parse(text); } catch { data = { raw: text }; }
      if (!res.ok) {
        const detail = data.detail || data.error || data.msg || text;
        if (res.status === 401 && path !== "/api/login" && path !== "/api/auth/status") {
          setKey("");
          showLogin();
        } else if (res.status === 403 && String(detail).indexOf("密码") !== -1) {
          authMustChange = true;
          showChangePw();
        }
        throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
      }
      return data;
    }

    function escapeHtml(s) {
      return String(s).replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
      })[c]);
    }

    function fmtNum(n) {
      return n == null ? "" : Number(n).toLocaleString("en-US");
    }
    function hasValue(v) {
      return v != null && Number(v) > 0;
    }
    function fmtTok(n) {
      if (n == null) return "";
      n = Number(n);
      if (n >= 1000000000) return (n / 1000000000).toFixed(1) + "b";
      if (n >= 1000000) return (n / 1000000).toFixed(1) + "m";
      return n >= 10000 ? (n / 1000).toFixed(1) + "k" : String(n);
    }
    function fmtDur(d) {
      if (d == null) return "";
      d = Number(d);
      return d >= 1000 ? (d / 1000).toFixed(1) + "s" : Math.round(d) + "ms";
    }
    function fmtBytes(n) {
      if (n == null) return "";
      n = Number(n);
      if (n >= 1048576) return (n / 1048576).toFixed(1) + " MB";
      if (n >= 1024) return (n / 1024).toFixed(1) + " KB";
      return n + " B";
    }
    function fmtTps(v) {
      return v == null ? "" : Number(v).toFixed(1);
    }
    function tpsClass(v) {
      if (v == null) return "ok";
      const n = Number(v);
      if (n >= 80) return "bolt";
      if (n >= 40) return "ok";
      if (n > 20) return "warn";
      return "err";
    }
    function tpsCell(e) {
      if (!hasValue(e.tps)) return "<td></td>";
      const cls = tpsClass(e.tps);
      const color = cls;
      const bolt = cls === "bolt" ? " ⚡" : "";
      return `<td><span class="tps-box ${color}">${Math.round(Number(e.tps))} t/s${bolt}</span></td>`;
    }
    function fmtSec(v) {
      return v == null ? "" : (Number(v) / 1000).toFixed(1) + "s";
    }
    function fmtCost(v) {
      if (!hasValue(v)) return "";
      const s = Number(v);
      return "$" + s.toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
    }
    function fmtUnitPrice(v) {
      if (!hasValue(v)) return "";
      const s = Number(v).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
      return "$" + s + "/M";
    }
    function fmtMultiplier(v) {
      if (v == null) return "×1";
      const s = Number(v).toFixed(6).replace(/0+$/, "").replace(/\.$/, "");
      return "×" + s;
    }
    function fmtRealCost(v) {
      if (!hasValue(v)) return "";
      const s = Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
      return "￥" + s;
    }
    function fmtTotalCny(v) {
      if (v == null) return "";
      const s = Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
      return "￥" + s;
    }
    function feeCell(e) {
      const ok = e.status != null && Number(e.status) < 400;
      const breakdown = e.cost_breakdown && e.cost_breakdown.rows && e.cost_breakdown.rows.length;
      const flag = cacheMissFlag(e);
      const costHtml = !hasValue(e.cost_usd)
        ? ""
        : (ok && breakdown
            ? `<div class="fee-click">${fmtCost(e.cost_usd)}${flag}</div>`
            : `<div>${fmtCost(e.cost_usd)}${flag}</div>`);
      const multHtml = `<div><span class="box green">${fmtMultiplier(e.multiplier)}</span></div>`;
      if (ok && breakdown) {
        return '<td class="cell-2l" data-fee="1" title="点击查看费用明细">' + costHtml + multHtml + "</td>";
      }
      return '<td class="cell-2l">' + costHtml + multHtml + "</td>";
    }
    function openFeeModal(e) {
      const b = e.cost_breakdown || {};
      const rows = (b.rows || []).map((r) => `
        <tr>
          <td>${escapeHtml(r.label)} <span class="mono">${fmtTok(r.tokens)}</span></td>
          <td class="mono">${fmtUnitPrice(r.unit_price)}</td>
          <td class="mono">${fmtCost(r.cost)}</td>
        </tr>`).join("");
      const mult = b.multiplier != null ? b.multiplier : e.multiplier;
      const real = b.real_cost_cny != null ? b.real_cost_cny : e.real_cost_cny;
      const feeMeta = [
        e.client_model ? escapeHtml(e.client_model) : "",
        e.upstream ? escapeHtml(e.upstream) : "",
        e.ts ? fmtTs(e.ts) : "",
      ].filter(Boolean).join(" · ");
      $("feeDetail").innerHTML = `
        <div class="fee-meta">${feeMeta}</div>
        ${b.tier === "long_context" && b.long_context_threshold != null
          ? `<div class="fee-tier">长文本档（输入上下文 > ${fmtTok(b.long_context_threshold)}）</div>`
          : ""}
        <table class="fee-table">
          <thead>
            <tr><th>项目</th><th>单价</th><th>单项总价</th></tr>
          </thead>
          <tbody>${rows}</tbody>
          <tfoot>
            <tr class="fee-total"><td>三项总和</td><td></td><td class="mono">${fmtCost(b.total)}</td></tr>
          </tfoot>
        </table>
        <div class="fee-mult">倍率 <span class="fee-mult-val">${fmtMultiplier(mult)}</span></div>
        <div class="fee-real">真实费用 <b>${fmtRealCost(real)}</b></div>`;
      $("feeModal").style.display = "flex";
    }
    function closeFeeModal() {
      $("feeModal").style.display = "none";
    }
    function fmtMoney(v) {
      if (v == null) return "";
      const s = Number(v).toFixed(4).replace(/0+$/, "").replace(/\.$/, "");
      return "$" + s;
    }
    function modelCell(e) {
      const model = escapeHtml(e.client_model || "");
      const effort = e.reasoning_effort ? escapeHtml(e.reasoning_effort) : "";
      const cls = e.is_classifier ? '<span class="box box-cls">分类器</span>' : "";
      const line2 = [effort, cls].filter(Boolean).join(" ");
      if (!model && !line2) return "<td></td>";
      if (!line2) return `<td class="mono col-model">${model}</td>`;
      return `<td class="mono col-model">
        <div>${model}</div>
        <div class="muted">${line2}</div>
      </td>`;
    }
    function endpointBadge(ep) {
      if (ep === "anthropic") return '<span class="box blue">Anthropic</span>';
      if (ep === "chat") return '<span class="box green">chat</span>';
      return '<span class="box">response</span>';
    }
    function upstreamCell(e) {
      const up = escapeHtml(e.upstream || "");
      const badge = endpointBadge(e.endpoint || "response");
      if (!up) return `<td>${badge}</td>`;
      return `<td class="mono">
        <div>${up}</div>
        <div class="muted">${badge}</div>
      </td>`;
    }
    const sidColorMap = new Map();
    function sessionIdCell(e) {
      const rawPath = Array.isArray(e.session_path) ? e.session_path : [];
      const path = rawPath.filter((sid, i) => sid && rawPath.indexOf(sid) === i);
      if (!path.length && e.session_id) path.push(e.session_id);
      if (!path.length) return "<td></td>";
      const lines = path.map((sid) => {
        let idx = sidColorMap.get(sid);
        if (idx === undefined) {
          idx = sidColorMap.size % 10;
          sidColorMap.set(sid, idx);
        }
        const short = sid.length > 20 ? sid.slice(0, 10) + "…" + sid.slice(-6) : sid;
        return `<div class="sid-line"><span class="box sid-${idx}">${escapeHtml(short)}</span></div>`;
      }).join("");
      return `<td class="mono sid-cell" style="font-size:0.75rem" title="${escapeHtml(path.join(" → "))}">${lines}</td>`;
    }
    function timeCell(e) {
      const hasTtft = hasValue(e.ttft_ms);
      const hasDur = hasValue(e.duration_ms);
      if (!hasTtft && !hasDur) return "<td></td>";
      const ttftOk = e.ttft_ms < 5000;
      const durCls = tpsClass(e.tps);
      const durColor = durCls === "bolt" ? "ok" : durCls;
      const barClass = (cls) => `<span class="vh ${cls}"></span>`;
      const lines = [];
      if (hasTtft) {
        lines.push(`<div><span class="tl">首字</span><span class="tv ${ttftOk ? "ok" : "warn"}">${fmtSec(e.ttft_ms)}</span></div>`);
      }
      if (hasDur) {
        lines.push(`<div><span class="tl">用时</span><span class="tv ${durColor}">${fmtSec(e.duration_ms)}</span></div>`);
      }
      return '<td class="cell-2l time-cell">' +
        `<span class="vbar">${barClass(ttftOk ? "ok" : "warn")}${barClass(durColor)}</span>` +
        '<span class="vbody">' + lines.join("") + "</span></td>";
    }
    function cacheMissFlag(e) {
      if (!e || !e.is_cache_miss) return "";
      const extra = hasValue(e.cache_miss_extra_usd) ? fmtMoney(e.cache_miss_extra_usd) : "";
      const kind = e.cache_miss_type === "prefix_reset" ? "缓存前缀失配" : "掉缓存";
      return `<span class="cm-flag" title="${kind} ${fmtTok(e.cache_miss_tokens)} tokens · 多花 ${extra}">!</span>`;
    }
    function tokensCell(e) {
      const parts = [];
      const inParts = [];
      if (hasValue(e.input_tokens)) {
        const uncached = Math.max(0, Number(e.input_tokens) - (Number(e.cached_tokens) || 0));
        if (hasValue(uncached)) {
          inParts.push(`<span class="arrow in">↓</span>输入<span class="tv">${fmtTok(uncached)}</span>`);
        }
      }
      if (hasValue(e.cached_tokens)) {
        inParts.push(`<span class="box green">缓存 ${fmtTok(e.cached_tokens)}</span>`);
      }
      if (e.is_cache_miss) inParts.push(cacheMissFlag(e));
      if (inParts.length) parts.push(`<div>${inParts.join("")}</div>`);
      const outParts = [];
      if (hasValue(e.output_tokens)) {
        outParts.push(`<span class="arrow out">↑</span>输出<span class="tv">${fmtTok(e.output_tokens)}</span>`);
      }
      if (hasValue(e.reasoning_tokens)) {
        outParts.push(`<span class="box blue">思考 ${fmtTok(e.reasoning_tokens)}</span>`);
      }
      if (outParts.length) parts.push(`<div>${outParts.join("")}</div>`);
      return parts.length ? `<td class="cell-2l">${parts.join("")}</td>` : "<td></td>";
    }
