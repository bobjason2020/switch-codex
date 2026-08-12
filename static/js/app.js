    function bindMaskClose(id, closeFn) {
      const mask = $(id);
      let pressOnMask = false;
      mask.addEventListener("mousedown", (ev) => {
        pressOnMask = ev.target === mask;
      });
      mask.addEventListener("click", (ev) => {
        if (ev.target === mask && pressOnMask) closeFn();
      });
    }
    bindMaskClose("upstreamModal", closeUpstreamModal);
    $("btnLogin").onclick = submitLogin;
    $("loginPassword").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") submitLogin();
    });
    $("btnChangePwSave").onclick = submitChangePw;
    ["oldPw", "newPw", "confirmPw"].forEach((id) => {
      $(id).addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") submitChangePw();
      });
    });

    $("btnLogApply").onclick = () => loadLogs(true);
    $("btnLogRefresh").onclick = () => loadLogs(false);
    $("logRange").onchange = () => {
      syncRangeUI("log");
      if ($("logRange").value !== "custom") loadLogs(true);
    };
    ["logStart", "logEnd"].forEach((id) => {
      $(id).addEventListener("change", () => {
        if ($("logRange").value === "custom") loadLogs(true);
      });
      $(id).addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") loadLogs(true);
      });
    });
    $("btnLogPrev").onclick = () => {
      if (logOffset > 0) { logOffset -= PAGE_SIZE; loadLogs(false); }
    };
    $("btnLogNext").onclick = () => {
      if (logOffset + PAGE_SIZE < logTotal) { logOffset += PAGE_SIZE; loadLogs(false); }
    };
    $("btnLogGo").onclick = () => goToPage("log", $("logPageInput").value);
    $("logPageInput").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") goToPage("log", ev.target.value);
    });
    $("btnErrApply").onclick = () => loadErrors(true);
    $("btnErrRefresh").onclick = () => loadErrors(false);
    $("errRange").onchange = () => {
      syncRangeUI("err");
      if ($("errRange").value !== "custom") loadErrors(true);
    };
    ["errStart", "errEnd"].forEach((id) => {
      $(id).addEventListener("change", () => {
        if ($("errRange").value === "custom") loadErrors(true);
      });
      $(id).addEventListener("keydown", (ev) => {
        if (ev.key === "Enter") loadErrors(true);
      });
    });
    $("btnErrPrev").onclick = () => {
      if (errOffset > 0) { errOffset -= PAGE_SIZE; loadErrors(false); }
    };
    $("btnErrNext").onclick = () => {
      if (errOffset + PAGE_SIZE < errTotal) { errOffset += PAGE_SIZE; loadErrors(false); }
    };
    $("btnErrGo").onclick = () => goToPage("err", $("errPageInput").value);
    $("errPageInput").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") goToPage("err", ev.target.value);
    });
    syncRangeUI("log");
    syncRangeUI("err");
    $("btnClearErrors").onclick = async () => {
      if (!confirm("清空全部错误日志？此操作不可恢复。")) return;
      try {
        await api("/api/errors", { method: "DELETE" });
        showMsg("错误日志已清空", true);
        await loadErrors(true);
      } catch (e) { showMsg(e.message, false); }
    };
    $("btnErrClose").onclick = closeErrModal;
    bindMaskClose("errModal", closeErrModal);
    $("btnFeeClose").onclick = closeFeeModal;
    bindMaskClose("feeModal", closeFeeModal);
    $("histRange").onchange = () => loadAvailabilityHistory();
    $("btnHistRefresh").onclick = () => loadAvailabilityHistory();
    $("btnModelProbeSettings").onclick = openProbeSettingsModal;
    $("btnModelAvailRefresh").onclick = async () => {
      const btn = $("btnModelAvailRefresh");
      const prev = btn.textContent;
      btn.disabled = true;
      btn.textContent = "探测中…";
      try {
        showMsg("正在重新探测所有启用模型…", true);
        const data = await api("/api/model-availability/run", { method: "POST" });
        renderModelBoard(data);
        const n = (data.data || []).filter((x) => x.probe_enabled !== false).length;
        const ok = (data.data || []).filter((x) => x.ok).length;
        showMsg(`探测完成：${ok}/${n} 可用`, true);
      } catch (e) {
        showMsg(e.message, false);
      } finally {
        btn.disabled = false;
        btn.textContent = prev || "刷新";
      }
    };
    $("modelBoard").addEventListener("click", async (e) => {
      const btn = e.target.closest(".btn-probe");
      if (!btn) return;
      const model = btn.dataset.probeModel;
      if (!model) return;
      const prev = btn.textContent;
      btn.disabled = true;
      btn.textContent = "探测中…";
      try {
        const data = await api("/api/model-availability/run", {
          method: "POST",
          body: JSON.stringify({ model }),
        });
        renderModelBoard(data);
        const item = (data.data || []).find((x) => x.model === model);
        if (item && item.ok) showMsg(`单独探测完成：${model} 可用`, true);
        else if (item && item.ok === false) {
          showMsg(`单独探测完成：${model} 不可用（${item.error || "全部失败"}）`, false);
        } else {
          showMsg(`已触发 ${model} 单独探测`, true);
        }
      } catch (err) {
        showMsg(err.message, false);
        btn.disabled = false;
        btn.textContent = prev || "单独探测";
      }
    });
    $("btnProbeSettingsClose").onclick = closeProbeSettingsModal;
    $("btnProbeSettingsCancel").onclick = closeProbeSettingsModal;
    $("btnProbeSettingsSave").onclick = saveProbeSettingsForm;
    bindMaskClose("probeSettingsModal", closeProbeSettingsModal);
    document.addEventListener("keydown", (ev) => {
      if (ev.key !== "Escape") return;
      if ($("newapiProbeModal").style.display !== "none") closeNewapiProbeModal();
      if ($("upstreamModal").style.display !== "none") closeUpstreamModal();
      if ($("errModal").style.display !== "none") closeErrModal();
      if ($("feeModal").style.display !== "none") closeFeeModal();
      if ($("probeSettingsModal").style.display !== "none") closeProbeSettingsModal();
    });
    $("btnAllLogs").onclick = () => switchPage("logs");
    $("btnClearLogs").onclick = async () => {
      if (!confirm("清空全部请求日志？此操作不可恢复。")) return;
      try {
        await api("/api/logs", { method: "DELETE" });
        showMsg("日志已清空", true);
        await loadLogs(true);
      } catch (e) { showMsg(e.message, false); }
    };
    $("btnSavePricing").onclick = async () => {
      const out = {};
      document.querySelectorAll("#pricingRows [data-model]").forEach((row) => {
        const m = row.dataset.model;
        const p = {};
        row.querySelectorAll("input[data-k]").forEach((inp) => {
          const v = inp.value.trim();
          if (v !== "") p[inp.dataset.k] = Number(v);
        });
        if (Object.keys(p).length) out[m] = p;
      });
      try {
        const r = await api("/api/pricing", {
          method: "PUT",
          body: JSON.stringify({ pricing: out }),
        });
        pricing = r.pricing || {};
        showMsg("费用单价已保存", true);
        if ($("page-logs").style.display !== "none") loadLogs(false);
      } catch (e) { showMsg(e.message, false); }
    };
    $("btnAddNewapiProbe").onclick = () => openNewapiProbeModal();
    $("btnNewapiRunAll").onclick = async () => {
      try {
        showMsg("正在探测所有启用任务…", true);
        const r = await api("/api/newapi-probes/run", { method: "POST" });
        const ok = (r.data || []).filter((x) => x.ok).length;
        showMsg(`探测完成：${ok}/${r.count} 成功`, true);
        await Promise.all([loadNewapiProbes(), loadUpstreams(), loadOverview()]);
      } catch (e) { showMsg(e.message, false); }
    };
    $("btnNewapiProbeClose").onclick = closeNewapiProbeModal;
    $("btnNewapiProbeCancel").onclick = closeNewapiProbeModal;
    $("btnNewapiProbeSave").onclick = saveNewapiProbeForm;
    bindMaskClose("newapiProbeModal", closeNewapiProbeModal);
    $("btnPublicRefresh").onclick = () => loadPublicAccess();
    $("btnPublicIpRefresh").onclick = () => loadPublicAccess();
    $("publicRange").onchange = () => loadPublicAccess();
    $("publicQ").addEventListener("keydown", (ev) => {
      if (ev.key === "Enter") loadPublicAccess();
    });
    $("btnSavePublic").onclick = savePublicSettings;
    $("btnSavePublicRules").onclick = savePublicSettings;
    const btnLogout = $("btnLogout");
    if (btnLogout) btnLogout.onclick = logout;

    setInterval(checkHealth, 15000);
    bootApp().catch((e) => showMsg(e.message, false));

    function formDirty() {
      const el = document.activeElement;
      if (!el) return false;
      const tag = (el.tagName || "").toLowerCase();
      return tag === "input" || tag === "textarea" || tag === "select";
    }

    setInterval(() => {
      if (!appBooted || !getKey()) return;
      if (formDirty()) return;
      if ($("page-logs").style.display !== "none" && logOffset === 0) {
        loadLogs(false);
      }
      if ($("page-errors").style.display !== "none" && errOffset === 0) {
        loadErrors(false);
      }
      if ($("page-upstreams").style.display !== "none") {
        loadUpstreams();
      }
      if ($("page-overview").style.display !== "none") {
        loadModelAvailability();
      }
      if ($("page-history").style.display !== "none") {
        loadAvailabilityHistory();
      }
      if ($("sub-public") && $("sub-public").style.display !== "none") {
        loadPublicAccess();
      }
    }, 10000);

function initRoute() {
  applyPath();
}
window.addEventListener("popstate", applyPath);
if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", initRoute);
} else {
  initRoute();
}

function updateClock() {
  const el = $("clock");
  if (!el) return;
  const now = new Date();
  el.textContent = now.toLocaleTimeString("zh-CN", {
    hour12: false,
    timeZone: "Asia/Shanghai",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}
setInterval(updateClock, 1000);
updateClock();
