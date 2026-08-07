/* 页面切换 + 路径路由：/logs、/settings/pricing 等后缀，刷新后停留在当前页面。 */
const PAGE_TITLES = {
  overview: "总览",
  history: "历史可用性",
  upstreams: "上游管理",
  logs: "请求日志",
  errors: "错误日志",
  settings: "设置",
};
const PAGE_SUBS = {
  overview: "模型可用性 · 今日流量",
  history: "按模型查看历史可用性与平均倍率",
  upstreams: "管理各模型池的上游轨道",
  logs: "请求明细与费用耗时",
  errors: "失败尝试与自动切换记录（保留 24 小时）",
  settings: "模型切换 / 价格 / NewAPI 探测 / 公网调用",
};
const SUB_ROUTES = { model: "model", pricing: "pricing", newapi: "newapi", public: "public" };
let _applyingPath = false;

function pathFor(name) {
  return name === "overview" ? "/" : "/" + name;
}

function setPageTitle(name) {
  const title = $("pageTitle");
  const sub = $("pageSub");
  if (title) title.textContent = PAGE_TITLES[name] || name;
  if (sub) sub.textContent = PAGE_SUBS[name] || "";
}

function pushPath(path) {
  if (location.pathname !== path) history.pushState({}, "", path);
}

function switchPage(name) {
  if (!_applyingPath) pushPath(pathFor(name));
  document.querySelectorAll(".page").forEach((el) => { el.style.display = "none"; });
  document.querySelectorAll(".nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.page === name);
  });
  const page = $("page-" + name);
  setPageTitle(name);
  if (page) page.style.display = "block";
  if (name === "overview") loadOverview();
  else if (name === "history") loadAvailabilityHistory();
  else if (name === "upstreams") loadUpstreams();
  else if (name === "logs") loadLogs(true);
  else if (name === "errors") loadErrors(true);
  else if (name === "settings") {
    switchSub("model");
    loadPricing();
    loadNewapiProbes();
    loadPublicAccess();
  }
}

function switchSub(name) {
  document.querySelectorAll(".sub-page").forEach((el) => { el.style.display = "none"; });
  document.querySelectorAll(".sub-nav-item").forEach((el) => {
    el.classList.toggle("active", el.dataset.sub === name);
  });
  const sub = $("sub-" + name);
  if (sub) sub.style.display = "block";
  if (name === "newapi") loadNewapiProbes();
  if (name === "public") loadPublicAccess();
  if (!_applyingPath) pushPath("/settings/" + name);
}

function applyPath() {
  const path = location.pathname.replace(/\/+$/, "") || "/";
  const parts = path.split("/").filter(Boolean);
  _applyingPath = true;
  let page = "overview";
  if (parts[0] && PAGE_TITLES[parts[0]]) page = parts[0];
  switchPage(page);
  if (page === "settings" && parts[1] && SUB_ROUTES[parts[1]]) {
    switchSub(parts[1]);
  }
  _applyingPath = false;
}

document.querySelectorAll(".nav-item").forEach((btn) => {
  btn.onclick = () => switchPage(btn.dataset.page);
});
document.querySelectorAll(".sub-nav-item").forEach((btn) => {
  btn.onclick = () => switchSub(btn.dataset.sub);
});

async function checkHealth() {
  try {
    const h = await api("/health");
    $("healthDot").className = "dot ok";
    $("healthText").textContent = "运行中 · " + (h.active_model || "");
  } catch (e) {
    $("healthDot").className = "dot err";
    $("healthText").textContent = "无法连接";
  }
}
