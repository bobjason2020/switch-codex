    function fillModelSelect(selectEl, selected, includeLocal) {
      const cur = selected || DEFAULT_MODEL;
      const custom = knownModels.filter((m) => m !== DEFAULT_MODEL && m !== DEEPSEEK_POOL).sort();
      const opt = (m, tag) => {
        const n = modelCounts[m] || 0;
        const label = `${m} · ${tag}` + (n ? ` · ${n} 启用` : "");
        return `<option value="${escapeHtml(m)}"${m === cur ? " selected" : ""}>${escapeHtml(label)}</option>`;
      };
      const parts = [];
      if (includeLocal) parts.push(opt(LOCAL_DIRECT, "本机直连"));
      parts.push(opt(DEFAULT_MODEL, "4100 代理"));
      if (knownModels.includes(DEEPSEEK_POOL)) parts.push(opt(DEEPSEEK_POOL, "官方同步"));
      custom.forEach((m) => parts.push(opt(m, modelSync[m] === "official-deepseek" ? "官方同步" : "仅路由")));
      if (cur && cur !== DEFAULT_MODEL && cur !== LOCAL_DIRECT && cur !== DEEPSEEK_POOL && !knownModels.includes(cur)) {
        parts.push(opt(cur, "仅路由"));
      }
      selectEl.innerHTML = parts.join("");
    }

    function fillPoolSelect(selectEl, selected) {
      const cur = selected || DEFAULT_MODEL;
      const opts = new Set(knownPools || [DEFAULT_MODEL, DEEPSEEK_POOL]);
      opts.add(DEFAULT_MODEL);
      opts.add(DEEPSEEK_POOL);
      if (cur) opts.add(cur);
      const rest = [...opts].filter((m) => m !== DEFAULT_MODEL && m !== DEEPSEEK_POOL).sort();
      const ordered = [DEFAULT_MODEL, DEEPSEEK_POOL, ...rest];
      selectEl.innerHTML = ordered.map((m) =>
        `<option value="${escapeHtml(m)}"${m === cur ? " selected" : ""}>${escapeHtml(m)} · 池</option>`
      ).join("");
    }

    function fillOptionSelect(id, emptyLabel, list, nameKey, countKey) {
      const sel = $(id);
      if (!sel) return;
      const cur = sel.value;
      sel.innerHTML = `<option value="">${escapeHtml(emptyLabel)}</option>` + list
        .map((x) => {
          const name = String(x[nameKey] || "").trim();
          if (!name) return "";
          const count = Number(x[countKey] || 0);
          const tag = count ? ` · ${fmtNum(count)} 次` : "";
          return `<option value="${escapeHtml(name)}">${escapeHtml(name)}${tag}</option>`;
        })
        .join("");
      if (cur && list.some((x) => String(x[nameKey]) === cur)) sel.value = cur;
    }

    function applyFilterOptions(kind, opts) {
      const o = opts || { pools: [], models: [], upstreams: [] };
      const empty = kind === "stats" ? "全部" : "全部池";
      fillOptionSelect(kind + "Pool", empty, o.pools || [], "pool", "count");
      fillOptionSelect(kind + "Model", "全部实际模型", o.models || [], "model", "count");
      fillOptionSelect(kind + "Upstream", "全部上游", o.upstreams || [], "upstream", "count");
    }

    async function loadPageFilterOptions(kind) {
      const params = new URLSearchParams();
      if (kind === "stats") {
        params.set("range", $("statsRange").value || "today");
      } else {
        appendLogRangeParams(params, kind);
      }
      const data = await api("/api/logs/filter-options?" + params.toString());
      const apiKey = kind === "stats" ? "stats" : kind === "log" ? "logs" : "errors";
      applyFilterOptions(kind, data[apiKey]);
    }

    function resolveFormModel() {
      const custom = $("modelCustom").value.trim();
      if (custom) return custom;
      return ($("modelSelect").value || DEFAULT_MODEL).trim() || DEFAULT_MODEL;
    }

    function stripUrlScheme(raw) {
      return String(raw || "").trim().replace(/^(?:https?:\/\/)+/i, "").replace(/\/+$/, "");
    }

    function storedUrl(raw) {
      const v = stripUrlScheme(raw);
      return v ? "https://" + v : v;
    }

    function displayUrl(u) {
      return stripUrlScheme(u && u.base_url ? u.base_url : "");
    }

    function isDeepseekPool(m) {
      const name = String(m || "").trim().toLowerCase();
      return name === "deepseek" || name.startsWith("deepseek-");
    }

    function isGrokPool(m) {
      const name = String(m || "").trim().toLowerCase();
      return name === "grok" || name.startsWith("grok-");
    }

    function deepseekPool() {
      return DEEPSEEK_POOL;
    }

    function resolveTypeModel() {
      const t = $("upstreamType").value;
      if (t === "deepseek") {
        // 编辑 DeepSeek 上游时保留原池，新增时默认用当前 DeepSeek 池
        if (upstreamEditModel && isDeepseekPool(upstreamEditModel)) return upstreamEditModel;
        return deepseekPool();
      }
      if (t === "grok") return GROK_POOL;
      if (t === "custom") return resolveFormModel();
      return DEFAULT_MODEL;
    }

    function syncUpstreamType() {
      const t = $("upstreamType").value;
      const isPreset = t !== "custom";
      $("modelFields").style.display = isPreset ? "none" : "";
      if (!$("editId").value && collectModelMap().length === 0) {
        renderModelMap(defaultModelMapForType(t));
      }
      if (t === "deepseek" && !$("editId").value) {
        const url = stripUrlScheme($("base_url").value);
        if (!url || url === "api.deepseek.com") {
          $("base_url").value = "api.deepseek.com";
        }
      }
    }

    function defaultModelMapForType(type) {
      if (type === DEFAULT_MODEL) {
        return [
          { model: "gpt-5.6-luna", actual: "" },
          { model: "gpt-5.6-terra", actual: "" },
          { model: "gpt-5.6-sol", actual: "" },
        ];
      }
      if (type === "deepseek") {
        return DEEPSEEK_CLIENT_MODELS.map((m) => ({ model: m, actual: "" }));
      }
      if (type === "grok") {
        return GROK_CLIENT_MODELS.map((m) => ({ model: m, actual: "" }));
      }
      return [];
    }

    function addModelMapRow(entry) {
      const row = document.createElement("div");
      row.className = "model-map-row";
      row.style.cssText = "display:flex;gap:8px;margin-bottom:6px;align-items:center";
      row.innerHTML =
        '<input class="mm-model" placeholder="模型名" style="flex:1;min-width:0" value="' +
        escapeHtml(entry && entry.model ? entry.model : "") +
        '" />' +
        '<input class="mm-actual" placeholder="实际模型名（留空同模型名）" style="flex:1;min-width:0" value="' +
        escapeHtml(entry && entry.actual ? entry.actual : "") +
        '" />' +
        '<button type="button" class="btn-ghost btn-sm mm-del">删除</button>';
      row.querySelector(".mm-del").onclick = () => {
        row.remove();
        if (!$("modelMapRows").children.length) addModelMapRow(null);
      };
      $("modelMapRows").appendChild(row);
    }

    function renderModelMap(entries) {
      $("modelMapRows").innerHTML = "";
      const list = (entries && entries.length) ? entries : [];
      if (!list.length) addModelMapRow(null);
      list.forEach(addModelMapRow);
    }

    function collectModelMap() {
      const out = [];
      document.querySelectorAll("#modelMapRows .model-map-row").forEach((row) => {
        const model = row.querySelector(".mm-model").value.trim();
        const actual = row.querySelector(".mm-actual").value.trim();
        if (model) out.push({ model, actual });
      });
      return out;
    }

    $("btnAddModelMap").onclick = () => addModelMapRow(null);

    function resetUpstreamForm() {
      $("editId").value = "";
      $("name").value = "";
      $("base_url").value = "";
      $("api_key").value = "";
      $("api_key").placeholder = "sk-...";
      $("priority").value = "100";
      $("multiplier").value = "";
      renderModelMap([]);
      $("enabled").checked = true;
      $("chatCompletions").checked = false;
      $("anthropicMessages").checked = false;
      $("modelCustom").value = "";
      fillPoolSelect($("modelSelect"), DEFAULT_MODEL);
      $("upstreamType").value = DEFAULT_MODEL;
      syncUpstreamType();
    }

    let upstreamEditModel = "";

    function openUpstreamModal(u) {
      upstreamEditModel = u ? (u.model || "") : "";
      $("editId").value = u ? u.id : "";
      $("name").value = u ? u.name : "";
      $("base_url").value = u ? displayUrl(u) : "";
      $("api_key").value = "";
      $("api_key").placeholder = u ? "留空则不修改密钥" : "sk-...";
      $("priority").value = u ? u.priority : "100";
      $("multiplier").value = u ? (u.multiplier ?? 1) : "";
      $("enabled").checked = u ? !!u.enabled : true;
      $("chatCompletions").checked = u ? !!u.chat_completions : false;
      $("anthropicMessages").checked = u ? !!u.anthropic_messages : false;
      $("modelCustom").value = "";
      fillPoolSelect($("modelSelect"), u ? (u.model || DEFAULT_MODEL) : DEFAULT_MODEL);
      let type = "custom";
      if (!u) type = DEFAULT_MODEL;
      else if (u.model === DEFAULT_MODEL) type = DEFAULT_MODEL;
      else if (isDeepseekPool(u.model)) type = "deepseek";
      else if (isGrokPool(u.model)) type = "grok";
      $("upstreamType").value = type;
      renderModelMap(u ? u.model_map : defaultModelMapForType(type));
      $("upstreamModalTitle").textContent = u ? "编辑上游：" + u.name : "新增上游";
      syncUpstreamType();
      $("upstreamModal").style.display = "flex";
      $("name").focus();
    }

    function closeUpstreamModal() {
      $("upstreamModal").style.display = "none";
    }

    let authMustChange = false;
    let appBooted = false;

    function showLogin() {
      const alreadyOpen = $("loginModal").style.display === "flex";
      setKey("");
      $("loginModal").style.display = "flex";
      if (!alreadyOpen) {
        $("loginError").style.display = "none";
        $("loginPassword").value = "";
        setTimeout(() => $("loginPassword").focus(), 50);
      }
      api("/api/auth/status").then((st) => {
        $("loginHint").textContent = st.default_password
          ? "默认密码：admin123（首次登录后必须修改）"
          : "请输入管理密码";
      }).catch(() => {});
    }

    function hideLogin() {
      $("loginModal").style.display = "none";
    }

    function showLoginError(text) {
      const el = $("loginError");
      el.textContent = text;
      el.style.display = "block";
    }

    function showChangePw() {
      // 已在改密界面输入中时不要清空表单（403 自动刷新会重复触发）
      if ($("changePwModal").style.display === "flex") return;
      $("changePwModal").style.display = "flex";
      $("changePwError").style.display = "none";
      $("oldPw").value = "";
      $("newPw").value = "";
      $("confirmPw").value = "";
      setTimeout(() => $("oldPw").focus(), 50);
    }

    function hideChangePw() {
      $("changePwModal").style.display = "none";
    }

    function showChangePwError(text) {
      const el = $("changePwError");
      el.textContent = text;
      el.style.display = "block";
    }

    async function submitLogin() {
      const pw = $("loginPassword").value;
      if (!pw) return;
      const btn = $("btnLogin");
      btn.disabled = true;
      try {
        const r = await api("/api/login", {
          method: "POST",
          body: JSON.stringify({ password: pw }),
        });
        setKey(r.token);
        authMustChange = !!r.must_change;
        hideLogin();
        if (authMustChange) {
          showChangePw();
        } else {
          bootApp();
        }
      } catch (e) {
        showLoginError(e.message);
      } finally {
        btn.disabled = false;
      }
    }

    async function submitChangePw() {
      const oldPw = $("oldPw").value;
      const newPw = $("newPw").value;
      const confirmPw = $("confirmPw").value;
      if (!oldPw) { showChangePwError("请填写旧密码"); return; }
      if (newPw.length < 8) { showChangePwError("新密码至少 8 位"); return; }
      if (newPw !== confirmPw) { showChangePwError("两次输入的新密码不一致"); return; }
      const btn = $("btnChangePwSave");
      btn.disabled = true;
      try {
        await api("/api/change-password", {
          method: "POST",
          body: JSON.stringify({ old_password: oldPw, new_password: newPw }),
        });
        authMustChange = false;
        hideChangePw();
        showMsg("密码已修改", true);
        bootApp();
      } catch (e) {
        showChangePwError(e.message);
      } finally {
        btn.disabled = false;
      }
    }

    async function bootApp() {
      appBooted = false;
      const st = await api("/api/auth/status");
      if (!st.logged_in) { showLogin(); return; }
      authMustChange = !!st.must_change;
      if (authMustChange) { showChangePw(); return; }
      appBooted = true;
      fillModelSelect($("activeModel"), DEFAULT_MODEL, true);
      fillPoolSelect($("modelSelect"), DEFAULT_MODEL);
      checkHealth();
      refreshModels()
        .then(() => Promise.all([loadUpstreams(), loadOverview(), loadPricing()]))
        .catch((e) => showMsg(e.message, false));
    }

    async function refreshModels() {
      const data = await api("/api/models");
      knownModels = (data.data || []).map((x) => x.model);
      knownPools = (data.pools || []).map((x) => x.model);
      if (!knownModels.includes(DEFAULT_MODEL)) knownModels.unshift(DEFAULT_MODEL);
      if (!knownPools.includes(DEFAULT_MODEL)) knownPools.unshift(DEFAULT_MODEL);
      if (!knownPools.includes(DEEPSEEK_POOL)) knownPools.unshift(DEEPSEEK_POOL);
      modelCounts = {};
      modelSync = {};
      for (const x of data.data || []) {
        modelCounts[x.model] = x.enabled_upstreams || 0;
        modelSync[x.model] = x.codex_sync || "";
      }
      activeModel = data.active_model || DEFAULT_MODEL;
      codexStatus = data.codex || {};
      claudeStatus = data.claude || {};
      grokStatus = data.grok || {};
      fillModelSelect($("activeModel"), activeModel, true);
      fillPoolSelect($("modelSelect"), $("modelSelect").value || DEFAULT_MODEL);
      fillClaudeModelSelect();
      fillGrokModelSelect();
      renderCodexStatus();
      renderClaudeStatus();
      renderGrokStatus();
    }

    function fillClaudeModelSelect() {
      const sel = $("claudeModel");
      if (!sel) return;
      const mode = claudeStatus.mode || "";
      let cur = "local-direct";
      if (mode === "openai-all") cur = DEFAULT_MODEL;
      else if (mode === "deepseek") cur = DEEPSEEK_POOL;
      const parts = [
        `<option value="local-direct"${cur === "local-direct" ? " selected" : ""}>本机原配置</option>`,
        `<option value="${escapeHtml(DEFAULT_MODEL)}"${cur === DEFAULT_MODEL ? " selected" : ""}>${escapeHtml(DEFAULT_MODEL)}</option>`,
      ];
      parts.push(
        `<option value="${escapeHtml(DEEPSEEK_POOL)}"${cur === DEEPSEEK_POOL ? " selected" : ""}>${escapeHtml(DEEPSEEK_POOL)}</option>`
      );
      sel.innerHTML = parts.join("");
    }

    function fillGrokModelSelect() {
      const sel = $("grokModel");
      if (!sel) return;
      const mode = grokStatus.mode || "";
      let cur = "local-direct";
      if (mode === "grok") cur = grokStatus.pool || GROK_POOL;
      const pools = (knownPools || [])
        .filter((p) => isGrokPool(p))
        .filter((p, i, arr) => arr.indexOf(p) === i)
        .sort();
      if (cur !== "local-direct" && cur && !pools.includes(cur)) pools.push(cur);
      const parts = [
        `<option value="local-direct"${cur === "local-direct" ? " selected" : ""}>本机原配置</option>`,
      ];
      for (const p of pools) {
        parts.push(
          `<option value="${escapeHtml(p)}"${p === cur ? " selected" : ""}>${escapeHtml(p)}</option>`
        );
      }
      sel.innerHTML = parts.join("");
    }
