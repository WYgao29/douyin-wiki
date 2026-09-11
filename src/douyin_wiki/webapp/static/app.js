"use strict";

const STORAGE = {
  view: "douyin-wiki.library-view",
  uiVersion: "douyin-wiki.ui-version",
  density: "douyin-wiki.library-density",
  sidebar: "douyin-wiki.sidebar-collapsed",
  chat: "douyin-wiki.chat-collapsed",
};

const UI_PREFERENCE_VERSION = "2";
const VALID_VIEWS = new Set(["list", "gallery"]);
const VALID_DENSITIES = new Set(["comfortable", "compact"]);
const VALID_SORTS = new Set([
  "captured_desc", "published_desc", "published_asc", "title_asc", "author_asc",
]);
const VALID_SECTIONS = new Set(["all", "recent", "favorite", "inspiration"]);

const state = {
  items: [],
  allItems: [],
  facets: {},
  section: "all",
  libraryView: "gallery",
  density: "comfortable",
  sort: "captured_desc",
  authors: [],
  types: [],
  tags: [],
  sources: [],
  inspirationOnly: false,
  query: "",
  currentEntry: document.body.dataset.entryId || "",
  currentArticleItem: null,
  currentTopic: document.body.dataset.topicId || "",
  topics: [],
  trashItems: [],
  topicSelectionMode: false,
  selectedEntryIds: new Set(),
  currentSession: "",
  sessions: [],
  sending: false,
  sidebarCollapsed: false,
  chatCollapsed: false,
  activeDrawer: "",
  commandIndex: 0,
  lastDialogTrigger: null,
  libraryLoaded: false,
  catalogHasItems: false,
  animatedEntryIds: new Set(),
  destructiveAction: null,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function svgIcon(name, className = "") {
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  if (className) svg.setAttribute("class", className);
  svg.setAttribute("aria-hidden", "true");
  const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
  use.setAttribute("href", `/static/icons.svg#${name}`);
  svg.append(use);
  return svg;
}

function readStored(key, fallback) {
  try {
    return window.localStorage.getItem(key) ?? fallback;
  } catch (_error) {
    return fallback;
  }
}

function writePreference(key, value) {
  try {
    window.localStorage.setItem(key, String(value));
  } catch (_error) {
    // The application remains usable when local storage is unavailable.
  }
}

function readPreferences() {
  const needsAlbumWallMigration = readStored(STORAGE.uiVersion, "") !== UI_PREFERENCE_VERSION;
  if (needsAlbumWallMigration) {
    state.libraryView = "gallery";
    writePreference(STORAGE.view, state.libraryView);
    writePreference(STORAGE.uiVersion, UI_PREFERENCE_VERSION);
  } else {
    const storedView = readStored(STORAGE.view, "gallery");
    state.libraryView = VALID_VIEWS.has(storedView) ? storedView : "gallery";
  }
  state.density = VALID_DENSITIES.has(readStored(STORAGE.density, "comfortable"))
    ? readStored(STORAGE.density, "comfortable")
    : "comfortable";
  state.sidebarCollapsed = readStored(STORAGE.sidebar, "false") === "true";
  state.chatCollapsed = readStored(STORAGE.chat, "false") === "true";
}

function readStateFromURL() {
  const params = new URLSearchParams(window.location.search);
  state.query = params.get("q") || "";
  const view = params.get("view");
  const preferredView = readStored(STORAGE.view, "gallery");
  state.libraryView = VALID_VIEWS.has(view)
    ? view
    : VALID_VIEWS.has(preferredView) ? preferredView : "gallery";
  const sort = params.get("sort");
  state.sort = VALID_SORTS.has(sort) ? sort : "captured_desc";
  const section = params.get("section");
  state.section = VALID_SECTIONS.has(section) ? section : "all";
  state.authors = params.getAll("author").filter(Boolean);
  state.types = params.getAll("type").filter(Boolean);
  state.tags = params.getAll("tag").filter(Boolean);
  state.sources = params.getAll("source").filter((value) => ["video", "image_note"].includes(value));
  state.inspirationOnly = params.get("has_inspiration") === "1";
}

function currentLibraryURL() {
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  if (state.libraryView !== "list") params.set("view", state.libraryView);
  if (state.sort !== "captured_desc") params.set("sort", state.sort);
  if (state.section !== "all") params.set("section", state.section);
  state.authors.forEach((value) => params.append("author", value));
  state.types.forEach((value) => params.append("type", value));
  state.tags.forEach((value) => params.append("tag", value));
  state.sources.forEach((value) => params.append("source", value));
  if (state.inspirationOnly) params.set("has_inspiration", "1");
  const query = params.toString();
  return query ? `/?${query}` : "/";
}

function writeStateToURL(mode = "replace") {
  if (state.currentEntry) return;
  const method = mode === "push" ? "pushState" : "replaceState";
  history[method]({}, "", currentLibraryURL());
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.classList.add("show");
  window.clearTimeout(toast.timer);
  toast.timer = window.setTimeout(() => node.classList.remove("show"), 2600);
}

function showRestoreRetry(jobId, message) {
  const node = $("#toast");
  const copy = document.createElement("span");
  copy.textContent = message;
  const retry = document.createElement("button");
  retry.type = "button";
  retry.className = "toast-action";
  retry.textContent = "重试下载";
  retry.addEventListener("click", async () => {
    retry.disabled = true;
    try {
      await api(`/api/jobs/${encodeURIComponent(jobId)}/retry`, {method: "POST"});
      toast("已重新加入视频恢复队列");
      await pollRestoreJob(jobId);
    } catch (error) {
      showRestoreRetry(jobId, error.message || "重试失败");
    }
  });
  node.replaceChildren(copy, retry);
  node.classList.add("show");
  window.clearTimeout(toast.timer);
}

async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: {"Content-Type": "application/json", ...(options.headers || {})},
    ...options,
  });
  if (!response.ok) {
    const body = await response.json().catch(() => ({}));
    const detail = Array.isArray(body.detail)
      ? body.detail.map((item) => item.msg || "输入内容无效").join("；")
      : body.detail;
    throw new Error(detail || "请求失败");
  }
  return response.json();
}

function openDestructiveDialog({title, description, confirmLabel, action, onSuccess, successMessage}) {
  state.destructiveAction = {action, onSuccess, successMessage};
  $("#destructive-title").textContent = title;
  $("#destructive-description").textContent = description;
  $("#confirm-destructive").textContent = confirmLabel;
  $("#confirm-destructive").disabled = false;
  $("#destructive-error").classList.add("hidden");
  $("#destructive-dialog").showModal();
  $("#confirm-destructive").focus();
}

async function runDestructiveAction() {
  if (!state.destructiveAction) return;
  const button = $("#confirm-destructive");
  const originalLabel = button.textContent;
  button.disabled = true;
  button.textContent = "处理中…";
  $("#destructive-error").classList.add("hidden");
  try {
    const pending = state.destructiveAction;
    const result = await pending.action();
    state.destructiveAction = null;
    $("#destructive-dialog").close();
    const warning = result?.warnings?.[0];
    toast(warning ? `${pending.successMessage || "操作已完成"}；${warning}` : (pending.successMessage || "操作已完成"));
    try {
      await pending.onSuccess?.(result);
    } catch (error) {
      toast(`操作已完成，但页面刷新失败：${error.message}`);
    }
  } catch (error) {
    $("#destructive-error").textContent = error.message;
    $("#destructive-error").classList.remove("hidden");
    button.disabled = false;
    button.textContent = originalLabel;
  }
}

function formatBytes(value) {
  const bytes = Number(value) || 0;
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 ** 3) return `${(bytes / 1024 ** 2).toFixed(1)} MB`;
  return `${(bytes / 1024 ** 3).toFixed(2)} GB`;
}

function toggleListValue(key, value) {
  const values = state[key];
  state[key] = values.includes(value)
    ? values.filter((item) => item !== value)
    : [...values, value];
}

function formatDate(value) {
  if (!value) return "时间未知";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value).slice(0, 10);
  return new Intl.DateTimeFormat("zh-CN", {
    timeZone: "Asia/Shanghai", year: "numeric", month: "2-digit", day: "2-digit",
  }).format(date);
}

function compareDate(a, b, field, direction) {
  const left = Date.parse(a[field] || "") || 0;
  const right = Date.parse(b[field] || "") || 0;
  return direction === "asc" ? left - right : right - left;
}

function filteredItems() {
  let items = state.items.filter((item) => {
    const hasInspiration = Boolean(item.inspirations?.length);
    return (!state.authors.length || state.authors.includes(item.author))
      && (!state.types.length || state.types.includes(item.content_type))
      && (!state.tags.length || state.tags.some((tag) => (item.tags || []).includes(tag)))
      && (!state.sources.length || state.sources.includes(item.source_kind))
      && (!state.inspirationOnly || hasInspiration)
      && (state.section !== "favorite" || item.favorite)
      && (state.section !== "inspiration" || hasInspiration);
  });
  items.sort((a, b) => {
    if (state.sort === "published_desc") return compareDate(a, b, "published_at", "desc");
    if (state.sort === "published_asc") return compareDate(a, b, "published_at", "asc");
    if (state.sort === "title_asc") return a.title.localeCompare(b.title, "zh-CN");
    if (state.sort === "author_asc") return a.author.localeCompare(b.author, "zh-CN") || a.title.localeCompare(b.title, "zh-CN");
    return compareDate(a, b, "captured_at", "desc");
  });
  if (state.section === "recent") items = items.slice(0, 20);
  return items;
}

function setLibraryLoading() {
  $("#library-status").classList.remove("hidden");
  $("#library-results").classList.add("hidden");
  $("#empty-library").classList.add("hidden");
  $("#empty-results").classList.add("hidden");
  $("#library-error").classList.add("hidden");
}

function setLibraryError(error) {
  $("#library-status").classList.add("hidden");
  $("#library-results").classList.add("hidden");
  $("#empty-library").classList.add("hidden");
  $("#empty-results").classList.add("hidden");
  $("#library-error-copy").textContent = error.message || "请稍后重试。";
  $("#library-error").classList.remove("hidden");
  $("#result-count").textContent = "载入失败";
}

async function loadLibrary({showLoading = !state.libraryLoaded} = {}) {
  if (showLoading) setLibraryLoading();
  const params = new URLSearchParams();
  if (state.query) params.set("q", state.query);
  try {
    const data = await api(`/api/library${params.size ? `?${params}` : ""}`);
    state.items = data.items;
    state.facets = data.facets;
    if (!state.query) state.allItems = data.items;
    state.catalogHasItems = Boolean(
      (!state.query && data.items.length)
      || data.facets.authors?.length
      || data.facets.content_types?.length
      || data.facets.tags?.length,
    );
    state.libraryLoaded = true;
    renderFilters();
    renderLibrary();
    fillInspirationTargets();
  } catch (error) {
    setLibraryError(error);
  }
}

async function ensureCommandItems() {
  if (state.allItems.length) return;
  try {
    const data = await api("/api/library");
    state.allItems = data.items;
  } catch (_error) {
    state.allItems = state.items;
  }
}

function makeChoice(label, value, key) {
  const node = document.createElement("button");
  node.type = "button";
  node.className = "choice-button";
  node.textContent = label;
  const active = state[key].includes(value);
  node.classList.toggle("active", active);
  node.setAttribute("aria-pressed", String(active));
  node.addEventListener("click", () => {
    toggleListValue(key, value);
    renderFilters();
    renderLibrary();
    writeStateToURL();
  });
  return node;
}

function renderFilters() {
  $("#author-filters").replaceChildren(...(state.facets.authors || []).map((value) => makeChoice(value, value, "authors")));
  $("#type-filters").replaceChildren(...(state.facets.content_types || []).map((value) => makeChoice(value.label, value.value, "types")));
  $("#source-filters").replaceChildren(
    makeChoice("视频", "video", "sources"),
    makeChoice("图文", "image_note", "sources"),
  );
  $("#tag-filters").replaceChildren(...(state.facets.tags || []).map((value) => makeChoice(`#${value}`, value, "tags")));
  $("#inspiration-only").checked = state.inspirationOnly;
  $$(".nav-item[data-section]").forEach((node) => node.classList.toggle("active", node.dataset.section === state.section));
  $("#topics-nav").classList.remove("active");
  $("#trash-nav").classList.remove("active");
  renderSidebarTags();
  renderAppliedFilters();
  renderToolbarState();
}

function renderSidebarTags() {
  const root = $("#sidebar-tags");
  const tags = (state.facets.tags || []).slice(0, 8);
  root.replaceChildren(...tags.map((tag) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "sidebar-tag";
    button.textContent = `#${tag}`;
    button.classList.toggle("active", state.tags.includes(tag));
    button.setAttribute("aria-pressed", String(state.tags.includes(tag)));
    button.addEventListener("click", () => {
      toggleListValue("tags", tag);
      renderFilters();
      renderLibrary();
      writeStateToURL();
    });
    return button;
  }));
}

function activeFilterCount() {
  return state.authors.length + state.types.length + state.tags.length + state.sources.length
    + Number(state.inspirationOnly);
}

function makeFilterChip(label, onRemove) {
  const chip = document.createElement("button");
  chip.type = "button";
  chip.className = "filter-chip";
  chip.append(document.createTextNode(label), svgIcon("x"));
  chip.setAttribute("aria-label", `移除筛选：${label}`);
  chip.addEventListener("click", () => {
    onRemove();
    renderFilters();
    renderLibrary();
    writeStateToURL();
  });
  return chip;
}

function renderAppliedFilters() {
  const root = $("#applied-filters");
  const chips = [];
  state.authors.forEach((value) => chips.push(makeFilterChip(`博主：${value}`, () => toggleListValue("authors", value))));
  state.types.forEach((value) => {
    const label = state.facets.content_types?.find((item) => item.value === value)?.label || value;
    chips.push(makeFilterChip(`类型：${label}`, () => toggleListValue("types", value)));
  });
  state.tags.forEach((value) => chips.push(makeFilterChip(`#${value}`, () => toggleListValue("tags", value))));
  state.sources.forEach((value) => chips.push(makeFilterChip(value === "image_note" ? "图文" : "视频", () => toggleListValue("sources", value))));
  if (state.inspirationOnly) chips.push(makeFilterChip("有灵感", () => { state.inspirationOnly = false; }));
  if (state.query) chips.push(makeFilterChip(`搜索：${state.query}`, () => {
    state.query = "";
    $("#search-input").value = "";
    loadLibrary();
  }));
  root.replaceChildren(...chips);
  root.classList.toggle("hidden", chips.length === 0);
}

function renderToolbarState() {
  const count = activeFilterCount();
  $("#filter-count").textContent = String(count);
  $("#filter-count").classList.toggle("hidden", count === 0);
  $("#sort-select").value = state.sort;
  $$('[data-library-view]').forEach((node) => node.setAttribute("aria-pressed", String(node.dataset.libraryView === state.libraryView)));
  $("#density-toggle").setAttribute("aria-label", state.density === "compact" ? "切换为舒适密度" : "切换为紧凑密度");
}

function makeCover(item, className) {
  const cover = document.createElement("div");
  cover.className = className;
  if (className === "gallery-cover") {
    cover.classList.add(item.source_kind === "image_note" ? "image-note-cover" : "video-cover");
  }
  if (item.cover_url) {
    const image = document.createElement("img");
    image.src = item.cover_url;
    image.alt = "";
    image.loading = "lazy";
    image.width = className === "item-thumbnail" ? 72 : 480;
    image.height = className === "item-thumbnail" ? 72 : 640;
    image.addEventListener("error", () => cover.replaceChildren(makePlaceholder(item)));
    cover.append(image);
  } else {
    cover.append(makePlaceholder(item));
  }
  return cover;
}

function makePlaceholder(item) {
  const node = document.createElement("div");
  node.className = "cover-placeholder";
  node.append(svgIcon(item.source_kind === "image_note" ? "image" : "video"));
  return node;
}

function sourceLabel(item) {
  return item.source_kind === "image_note" ? "图文" : "视频";
}

function syncFavoriteItem(updated) {
  const update = (item) => item.entry_id === updated.entry_id ? {...item, ...updated} : item;
  state.items = state.items.map(update);
  state.allItems = state.allItems.map(update);
  if (state.currentArticleItem?.entry_id === updated.entry_id) {
    state.currentArticleItem = {...state.currentArticleItem, ...updated};
  }
}

function findLibraryItem(entryId) {
  return state.items.find((item) => item.entry_id === entryId)
    || state.allItems.find((item) => item.entry_id === entryId)
    || (state.currentArticleItem?.entry_id === entryId ? state.currentArticleItem : null)
    || null;
}

function isDatabaseManaged(item) {
  return Boolean(item?.database_managed);
}

function currentEntryItem() {
  return state.currentEntry ? findLibraryItem(state.currentEntry) : null;
}

function updateFavoriteButton(button, favorite) {
  button.classList.toggle("active", favorite);
  button.setAttribute("aria-pressed", String(favorite));
  button.setAttribute("aria-label", favorite ? "取消收藏" : "收藏");
  button.title = favorite ? "取消收藏" : "收藏";
  const label = $("[data-favorite-label]", button);
  if (label) label.textContent = favorite ? "取消收藏" : "收藏";
}

async function pollRestoreJob(jobId) {
  const terminal = new Set(["completed", "completed_with_warnings", "failed", "needs_auth"]);
  for (let attempt = 0; attempt < 60; attempt += 1) {
    await new Promise((resolve) => window.setTimeout(resolve, 2000));
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (!terminal.has(job.status)) continue;
    if (job.status === "completed" || job.status === "completed_with_warnings") {
      toast(job.result?.skipped ? "已取消视频恢复" : "收藏视频已重新下载到本地");
      await loadLibrary({showLoading: false});
    } else if (job.status === "needs_auth") {
      showRestoreRetry(jobId, "已收藏；更新抖音登录后可重试下载");
    } else {
      showRestoreRetry(jobId, "已收藏；视频恢复失败");
    }
    return;
  }
  toast("已收藏；视频仍在后台恢复");
}

function makeFavoriteButton(item, className = "") {
  if (!isDatabaseManaged(item)) return null;
  const button = document.createElement("button");
  button.type = "button";
  button.className = `favorite-button ${className}`.trim();
  button.append(svgIcon("bookmark"));
  updateFavoriteButton(button, Boolean(item.favorite));
  button.addEventListener("click", async (event) => {
    event.preventDefault();
    event.stopPropagation();
    const favorite = !button.classList.contains("active");
    button.disabled = true;
    try {
      const response = await api(`/api/articles/${encodeURIComponent(item.entry_id)}/favorite`, {
        method: "PUT",
        body: JSON.stringify({favorite}),
      });
      Object.assign(item, response.item);
      syncFavoriteItem(response.item);
      updateFavoriteButton(button, response.item.favorite);
      toast(response.item.favorite ? "已收藏，视频会永久保留在本地" : "已取消收藏");
      if (!$("#library-view").classList.contains("hidden")) renderLibrary();
      if (response.restore_job) pollRestoreJob(response.restore_job.id).catch(() => {
        toast("已收藏；暂时无法读取视频恢复进度");
      });
    } catch (error) {
      toast(error.message || "收藏操作失败");
    } finally {
      button.disabled = false;
    }
  });
  return button;
}

function stableHash(value) {
  let hash = 2166136261;
  for (const character of String(value || "抖库")) {
    hash ^= character.codePointAt(0);
    hash = Math.imul(hash, 16777619);
  }
  return hash >>> 0;
}

function galleryAccessibleName(item) {
  return [
    item.title || "未命名文章",
    item.author || "未知作者",
    item.content_type_label || "其他",
    sourceLabel(item),
    formatDate(item.published_at),
  ].join("，");
}

function makeMetadata(item) {
  const metadata = document.createElement("div");
  metadata.className = "item-metadata";
  [item.author || "未知作者", item.content_type_label || "其他", formatDate(item.published_at), sourceLabel(item)].forEach((value) => {
    const span = document.createElement("span");
    span.textContent = value;
    metadata.append(span);
  });
  return metadata;
}

function makeListItem(item) {
  const article = document.createElement("article");
  article.className = "library-item";
  article.classList.toggle("active", state.currentEntry === item.entry_id);
  const link = document.createElement("a");
  link.className = "library-item-link";
  link.href = `/articles/${encodeURIComponent(item.entry_id)}`;
  link.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (state.topicSelectionMode && isDatabaseManaged(item)) {
      toggleTopicEntry(item.entry_id);
      return;
    }
    openArticle(item.entry_id);
  });
  const copy = document.createElement("div");
  copy.className = "item-copy";
  const title = document.createElement("h2");
  title.textContent = item.title;
  const summary = document.createElement("p");
  summary.className = "item-summary";
  summary.textContent = item.summary || "这篇资料暂时没有一句话摘要。";
  copy.append(title, summary, makeMetadata(item));
  const tags = document.createElement("div");
  tags.className = "item-tags";
  (item.tags || []).slice(0, 3).forEach((tag) => {
    const node = document.createElement("span");
    node.className = "item-tag";
    node.textContent = `#${tag}`;
    tags.append(node);
  });
  link.append(makeCover(item, "item-thumbnail"), copy, tags);
  article.append(link);
  if (isDatabaseManaged(item)) {
    article.classList.toggle("topic-selected", state.selectedEntryIds.has(item.entry_id));
    if (state.topicSelectionMode) article.append(makeSelectionIndicator(item.entry_id));
  }
  return article;
}

function makeSelectionIndicator(entryId) {
  const indicator = document.createElement("span");
  indicator.className = "topic-selection-indicator";
  indicator.append(svgIcon(state.selectedEntryIds.has(entryId) ? "check" : "plus"));
  indicator.setAttribute("aria-hidden", "true");
  return indicator;
}

function toggleTopicEntry(entryId) {
  if (!isDatabaseManaged(findLibraryItem(entryId))) return;
  if (state.selectedEntryIds.has(entryId)) state.selectedEntryIds.delete(entryId);
  else state.selectedEntryIds.add(entryId);
  renderLibrary();
  updateTopicSelectionButton();
}

function makeGalleryCard(item, index, animateNew) {
  const article = document.createElement("article");
  article.className = "gallery-card";
  const animationId = item.entry_id || item.work_id;
  if (animateNew && !state.animatedEntryIds.has(animationId)) {
    article.classList.add("is-entering");
    article.style.setProperty("--enter-delay", `${Math.min(index, 8) * 35}ms`);
    state.animatedEntryIds.add(animationId);
  }
  article.style.setProperty("--cover-tilt", stableHash(item.work_id || item.entry_id) % 2 ? ".35deg" : "-.35deg");
  const link = document.createElement("a");
  link.className = "gallery-card-link";
  link.href = `/articles/${encodeURIComponent(item.entry_id)}`;
  const accessibleName = galleryAccessibleName(item);
  link.setAttribute("aria-label", `打开文章：${accessibleName}`);
  link.title = accessibleName;
  const cover = makeCover(item, "gallery-cover");
  const copy = document.createElement("div");
  copy.className = "gallery-copy";
  const title = document.createElement("h2");
  title.textContent = item.title || "未命名文章";
  const author = document.createElement("p");
  author.textContent = item.author || "未知作者";
  const details = document.createElement("p");
  details.className = "gallery-details";
  details.textContent = `${item.content_type_label || "其他"} · ${sourceLabel(item)} · ${formatDate(item.published_at)}`;
  copy.append(title, author, details);
  const openFromAlbumWall = (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    if (state.topicSelectionMode && isDatabaseManaged(item)) {
      toggleTopicEntry(item.entry_id);
      return;
    }
    openArticle(item.entry_id, true, cover);
  };
  link.addEventListener("click", openFromAlbumWall);
  link.addEventListener("keydown", (event) => {
    if (event.key === "Enter") openFromAlbumWall(event);
  });
  link.append(cover, copy);
  article.append(link);
  if (isDatabaseManaged(item)) {
    article.classList.toggle("topic-selected", state.selectedEntryIds.has(item.entry_id));
    if (state.topicSelectionMode) article.append(makeSelectionIndicator(item.entry_id));
  }
  return article;
}

function renderLibraryList(items) {
  return items.map(makeListItem);
}

function renderLibraryGallery(items) {
  const animateNew = !$("#library-view").classList.contains("hidden");
  return items.map((item, index) => makeGalleryCard(item, index, animateNew));
}

function renderLibraryEmptyState(items) {
  const hasCatalogItems = state.catalogHasItems || state.allItems.length;
  $("#empty-library").classList.toggle("hidden", items.length > 0 || hasCatalogItems);
  $("#empty-results").classList.toggle("hidden", items.length > 0 || !hasCatalogItems);
  if (!items.length && hasCatalogItems) {
    $("#empty-results-copy").textContent = state.query
      ? `没有包含“${state.query}”的文章。请调整关键词或清除筛选。`
      : "当前筛选条件没有匹配文章。请调整或清除筛选。";
  }
}

function renderLibrary() {
  const items = filteredItems();
  const titles = {all: "资料库", recent: "最近加入", favorite: "收藏", inspiration: "灵感"};
  const summaries = {
    all: "整理并检索已经入库的抖音知识",
    recent: "最近采集和更新的知识资料",
    favorite: "永久保留在本地的收藏资料",
    inspiration: "带有你原始灵感的文章",
  };
  $("#view-title").textContent = titles[state.section];
  $("#view-summary").textContent = summaries[state.section];
  $("#result-count").textContent = `${items.length} 篇`;
  $("#library-status").classList.add("hidden");
  $("#library-error").classList.add("hidden");
  const root = $("#library-results");
  const effectiveView = state.libraryView;
  root.className = `library-results ${effectiveView}${effectiveView === "list" && state.density === "compact" ? " compact" : ""}`;
  root.replaceChildren(...(effectiveView === "gallery" ? renderLibraryGallery(items) : renderLibraryList(items)));
  root.classList.toggle("hidden", items.length === 0);
  renderLibraryEmptyState(items);
  renderAppliedFilters();
  updateTopicSelectionButton();
}

function updateTopicSelectionButton() {
  const button = $("#topic-select-toggle");
  const hasManagedItems = [...state.items, ...state.allItems, state.currentArticleItem]
    .some(isDatabaseManaged);
  if (!hasManagedItems) {
    state.topicSelectionMode = false;
    state.selectedEntryIds.clear();
  }
  button.disabled = !hasManagedItems;
  button.classList.toggle("hidden", !hasManagedItems);
  const topicsCreateButton = $("#topics-create-button");
  topicsCreateButton.disabled = !hasManagedItems;
  topicsCreateButton.classList.toggle("hidden", !hasManagedItems);
  button.setAttribute("aria-pressed", String(state.topicSelectionMode));
  const label = $("span", button);
  label.textContent = state.topicSelectionMode
    ? (state.selectedEntryIds.size ? `已选 ${state.selectedEntryIds.size} 篇 · 下一步` : "选择专题来源")
    : "创建专题";
  button.classList.toggle("active", state.topicSelectionMode);
}

function openTopicDialog() {
  if (!state.selectedEntryIds.size) {
    toast("请先选择至少一篇文章");
    return;
  }
  $("#topic-title-input").value = "";
  $("#topic-goal-input").value = "";
  $("#topic-instructions-input").value = "";
  $("#topic-error").classList.add("hidden");
  $("#topic-selection-summary").textContent = `已选择 ${state.selectedEntryIds.size} 篇文章，顺序按当前资料库显示。`;
  $("#topic-dialog").showModal();
  $("#topic-title-input").focus();
}

async function createTopicFromSelection() {
  if (!$("#topic-form").reportValidity()) return;
  const orderedIds = filteredItems()
    .filter(isDatabaseManaged)
    .map((item) => item.entry_id)
    .filter((entryId) => state.selectedEntryIds.has(entryId));
  const payload = {
    title: $("#topic-title-input").value,
    goal: $("#topic-goal-input").value,
    instructions: $("#topic-instructions-input").value,
    entry_ids: orderedIds,
  };
  try {
    const result = await api("/api/topics", {method: "POST", body: JSON.stringify(payload)});
    $("#topic-dialog").close();
    state.topicSelectionMode = false;
    state.selectedEntryIds.clear();
    await loadTopics();
    await openTopic(result.topic.id);
    toast("专题已创建");
  } catch (error) {
    $("#topic-error").textContent = error.message;
    $("#topic-error").classList.remove("hidden");
  }
}

function clearSearchAndFilters() {
  state.query = "";
  state.authors = [];
  state.types = [];
  state.tags = [];
  state.sources = [];
  state.inspirationOnly = false;
  $("#search-input").value = "";
  renderFilters();
  writeStateToURL();
  loadLibrary();
}

function closeImportsPopover() {
  $("#imports-popover")?.classList.add("hidden");
  $("#imports-toggle")?.setAttribute("aria-expanded", "false");
}

function positionImportsPopover() {
  const toggle = $("#imports-toggle");
  const popover = $("#imports-popover");
  if (!toggle || !popover) return;
  const rect = toggle.getBoundingClientRect();
  const viewportPadding = 16;
  const gap = 8;
  const width = Math.min(360, Math.max(0, window.innerWidth - viewportPadding * 2));
  const left = Math.max(
    viewportPadding,
    Math.min(rect.right - width, window.innerWidth - width - viewportPadding),
  );
  popover.style.setProperty("--filter-popover-top", `${rect.bottom + gap}px`);
  popover.style.setProperty("--filter-popover-left", `${left}px`);
}

function openImportsPopover() {
  const popover = $("#imports-popover");
  if (!popover) return;
  popover.classList.remove("hidden");
  $("#imports-toggle").setAttribute("aria-expanded", "true");
  positionImportsPopover();
  popover.focus();
}

function hideLegacyViews() {
  window.Douku?.hideAllViews();
  $("#article-view").classList.add("hidden");
  $("#topics-view").classList.add("hidden");
  $("#topic-view").classList.add("hidden");
  $("#trash-view").classList.add("hidden");
  $("#library-view").classList.add("hidden");
}

function showLibrary(push = true) {
  state.currentEntry = "";
  state.currentArticleItem = null;
  state.currentTopic = "";
  hideLegacyViews();
  $("#library-view").classList.remove("hidden");
  window.Douku?.setPage("library");
  $("#topics-nav").classList.remove("active");
  $("#trash-nav").classList.remove("active");
  $$(".nav-item[data-section]").forEach((node) => node.classList.toggle("active", node.dataset.section === state.section));
  if (push) history.pushState({}, "", currentLibraryURL());
  document.title = "资料库 · 抖库";
  renderLibrary();
  updateChatContext();
  closeDrawers();
}

async function loadTopics() {
  const data = await api("/api/topics");
  state.topics = data.topics || [];
  renderTopicsList();
  return state.topics;
}

function renderTopicsList() {
  const root = $("#topics-list");
  if (!state.topics.length) {
    const empty = document.createElement("section");
    empty.className = "empty-state";
    empty.append(svgIcon("bookmark"));
    const title = document.createElement("h2");
    title.textContent = "还没有专题";
    const copy = document.createElement("p");
    copy.textContent = "回到资料库选择文章，创建一个严格限定来源的研究专题。";
    empty.append(title, copy);
    root.replaceChildren(empty);
    return;
  }
  root.replaceChildren(...state.topics.map((value) => {
    const topic = value.topic;
    const button = document.createElement("button");
    button.type = "button";
    button.className = "topic-list-card";
    const title = document.createElement("h2");
    title.textContent = topic.title;
    const goal = document.createElement("p");
    goal.textContent = topic.goal || "未填写研究目标";
    const meta = document.createElement("span");
    const active = topic.sources.filter((source) => source.enabled).length;
    meta.textContent = `${active} 个启用来源 · ${topic.updated_display}`;
    button.append(title, goal, meta);
    button.addEventListener("click", () => openTopic(topic.id));
    return button;
  }));
}

async function showTopics(push = true) {
  state.currentEntry = "";
  state.currentArticleItem = null;
  state.currentTopic = "";
  state.topicSelectionMode = false;
  hideLegacyViews();
  $("#topics-view").classList.remove("hidden");
  window.Douku?.setPage("topics");
  $$(".nav-item[data-section]").forEach((node) => node.classList.remove("active"));
  $("#topics-nav").classList.add("active");
  $("#trash-nav").classList.remove("active");
  if (push) history.pushState({}, "", "/topics");
  document.title = "专题 · 抖库";
  updateTopicSelectionButton();
  await loadTopics();
  updateChatContext();
  closeDrawers();
}

async function loadTrash() {
  const data = await api("/api/trash");
  state.trashItems = data.items || [];
  renderTrash();
  return state.trashItems;
}

function renderTrash() {
  const root = $("#trash-list");
  $("#trash-count").textContent = `${state.trashItems.length} 条资料`;
  if (!state.trashItems.length) {
    const empty = document.createElement("section");
    empty.className = "empty-state";
    empty.append(svgIcon("trash-2"));
    const title = document.createElement("h2");
    title.textContent = "废纸篓是空的";
    const copy = document.createElement("p");
    copy.textContent = "从文章页面删除的资料会暂存在这里。";
    empty.append(title, copy);
    root.replaceChildren(empty);
    return;
  }
  root.replaceChildren(...state.trashItems.map((item) => {
    const row = document.createElement("article");
    row.className = "trash-item";
    const icon = document.createElement("span");
    icon.className = `trash-item-icon ${item.source_kind === "image_note" ? "is-image" : "is-video"}`;
    icon.append(svgIcon(item.source_kind === "image_note" ? "image" : "video"));
    const copy = document.createElement("div");
    copy.className = "trash-item-copy";
    const title = document.createElement("h2");
    title.textContent = item.title;
    const meta = document.createElement("p");
    meta.textContent = `${item.author || "未知作者"} · 删除于 ${item.deleted_display || "时间未知"} · ${formatBytes(item.size_bytes)}`;
    copy.append(title, meta);
    const actions = document.createElement("div");
    actions.className = "trash-item-actions";
    const restore = document.createElement("button");
    restore.type = "button";
    restore.className = "secondary-button";
    restore.append(svgIcon("rotate-ccw"), document.createTextNode("恢复"));
    restore.addEventListener("click", () => openDestructiveDialog({
      title: "恢复这条资料？",
      description: `将恢复《${item.title}》的文章、原始记录、视频或图片，并重新加入检索和相关专题。`,
      confirmLabel: "确认恢复",
      action: async () => {
        return api(`/api/trash/${encodeURIComponent(item.trash_id)}/restore`, {
          method: "POST", body: JSON.stringify({confirmed: true}),
        });
      },
      successMessage: "资料已恢复",
      onSuccess: async (result) => {
        await Promise.all([loadTrash(), loadLibrary({showLoading: false}), loadTopics()]);
        await openArticle(result.entry_id);
      },
    }));
    const purge = document.createElement("button");
    purge.type = "button";
    purge.className = "danger-text-button";
    purge.append(svgIcon("trash-2"), document.createTextNode("彻底删除"));
    purge.addEventListener("click", () => openDestructiveDialog({
      title: "彻底删除这条资料？",
      description: `《${item.title}》及其 ${formatBytes(item.size_bytes)} 本地文件将被永久删除，无法通过抖库恢复；Markdown 可能仍存在于 Git 历史中。`,
      confirmLabel: "彻底删除",
      action: async () => {
        return api(`/api/trash/${encodeURIComponent(item.trash_id)}`, {
          method: "DELETE", body: JSON.stringify({confirmed: true}),
        });
      },
      successMessage: "资料已彻底删除",
      onSuccess: async () => {
        await loadTrash();
      },
    }));
    actions.append(restore, purge);
    row.append(icon, copy, actions);
    return row;
  }));
}

async function showTrash(push = true) {
  state.currentEntry = "";
  state.currentArticleItem = null;
  state.currentTopic = "";
  state.topicSelectionMode = false;
  hideLegacyViews();
  $("#trash-view").classList.remove("hidden");
  window.Douku?.setPage("trash");
  $$(".nav-item").forEach((node) => node.classList.remove("active"));
  $("#trash-nav").classList.add("active");
  if (push) history.pushState({}, "", "/trash");
  document.title = "废纸篓 · 抖库";
  updateTopicSelectionButton();
  await loadTrash();
  updateChatContext();
  closeDrawers();
}

function artifactButtons(topicId) {
  const group = document.createElement("div");
  group.className = "topic-artifact-actions";
  [
    ["overview", "专题总览"], ["comparison", "对比表"],
    ["evidence_map", "证据地图"], ["consensus", "共识与分歧"],
    ["decision_brief", "决策简报"], ["faq", "FAQ"],
  ].forEach(([kind, label]) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "secondary-button";
    button.textContent = label;
    button.addEventListener("click", async () => {
      button.disabled = true;
      button.textContent = "生成中…";
      try {
        await api(`/api/topics/${encodeURIComponent(topicId)}/artifacts`, {
          method: "POST", body: JSON.stringify({kind}),
        });
        await openTopic(topicId, false);
        toast(`${label}已生成`);
      } catch (error) {
        toast(error.message);
      } finally {
        button.disabled = false;
        button.textContent = label;
      }
    });
    group.append(button);
  });
  return group;
}

function renderTopicData(value) {
  const topic = value.topic;
  const root = $("#topic-content");
  const header = document.createElement("header");
  header.className = "topic-header";
  const kicker = document.createElement("span");
  kicker.className = "dialog-kicker";
  kicker.textContent = "选定来源研究";
  const title = document.createElement("h1");
  title.textContent = topic.title;
  const goal = document.createElement("p");
  goal.className = "topic-goal";
  goal.textContent = topic.goal || "未填写研究目标";
  header.append(kicker, title, goal);
  if (topic.instructions) {
    const instructions = document.createElement("p");
    instructions.className = "topic-instructions";
    instructions.textContent = `自定义指令：${topic.instructions}`;
    header.append(instructions);
  }

  const sourceSection = document.createElement("section");
  sourceSection.className = "topic-section";
  const sourceTitle = document.createElement("h2");
  sourceTitle.textContent = "专题来源";
  const sourceHint = document.createElement("p");
  sourceHint.textContent = "停用后，该文章会立即退出专题问答范围。";
  const sourceList = document.createElement("div");
  sourceList.className = "topic-source-list";
  topic.sources.forEach((source) => {
    const label = document.createElement("label");
    label.className = "topic-source-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.checked = source.enabled;
    checkbox.dataset.entryId = source.entry_id;
    const copy = document.createElement("span");
    copy.textContent = source.title || source.entry_id;
    const open = document.createElement("button");
    open.type = "button";
    open.className = "text-button";
    open.textContent = "打开文章";
    open.addEventListener("click", (event) => { event.preventDefault(); openArticle(source.entry_id); });
    label.append(checkbox, copy, open);
    sourceList.append(label);
  });
  sourceList.addEventListener("change", async () => {
    const sources = $$("input[type='checkbox']", sourceList).map((node) => ({
      entry_id: node.dataset.entryId, enabled: node.checked,
    }));
    try {
      await api(`/api/topics/${encodeURIComponent(topic.id)}/sources`, {
        method: "PUT", body: JSON.stringify({sources}),
      });
      await openTopic(topic.id, false);
      toast("专题来源已更新");
    } catch (error) {
      toast(error.message);
      await openTopic(topic.id, false);
    }
  });
  sourceSection.append(sourceTitle, sourceHint, sourceList);

  const artifactSection = document.createElement("section");
  artifactSection.className = "topic-section";
  const artifactTitle = document.createElement("h2");
  artifactTitle.textContent = "研究成果";
  artifactSection.append(artifactTitle, artifactButtons(topic.id));
  const artifactList = document.createElement("div");
  artifactList.className = "topic-artifacts";
  value.artifacts.forEach((artifact) => {
    const card = document.createElement("article");
    card.className = "topic-artifact-card";
    const heading = document.createElement("header");
    const h3 = document.createElement("h3");
    h3.textContent = artifact.title;
    const status = document.createElement("span");
    status.className = artifact.status === "needs_update" ? "artifact-stale" : "status-badge";
    status.textContent = artifact.status_label;
    const meta = document.createElement("small");
    meta.textContent = `${artifact.created_display}${artifact.model ? ` · ${artifact.model}` : ""}${artifact.total_tokens != null ? ` · ${artifact.total_tokens} token` : ""}`;
    heading.append(h3, status, meta);
    const body = document.createElement("div");
    body.className = "article-body";
    body.innerHTML = artifact.html;
    card.append(heading, body);
    artifactList.append(card);
  });
  if (!value.artifacts.length) {
    const empty = document.createElement("p");
    empty.textContent = "尚未生成成果。只有点击上方按钮时才会调用模型并消耗 token。";
    artifactList.append(empty);
  }
  artifactSection.append(artifactList);
  root.replaceChildren(header, sourceSection, artifactSection);
}

async function openTopic(topicId, push = true) {
  try {
    const value = await api(`/api/topics/${encodeURIComponent(topicId)}`);
    state.currentEntry = "";
    state.currentTopic = topicId;
    renderTopicData(value);
    hideLegacyViews();
    $("#topic-view").classList.remove("hidden");
    window.Douku?.setPage("topic");
    $$(".nav-item[data-section]").forEach((node) => node.classList.remove("active"));
    $("#topics-nav").classList.add("active");
    $("#trash-nav").classList.remove("active");
    if (push) history.pushState({topicId}, "", `/topics/${encodeURIComponent(topicId)}`);
    document.title = `${value.topic.title} · 抖库`;
    await loadTopics();
    updateChatContext();
    closeDrawers();
    $(".library-main").scrollTo({top: 0, behavior: "instant"});
  } catch (error) {
    toast(error.message);
  }
}

function makeArticleHeader(item) {
  const hero = document.createElement("header");
  hero.className = "article-hero";
  const copy = document.createElement("div");
  copy.className = "article-hero-copy";
  const badges = document.createElement("div");
  badges.className = "article-kicker-row";
  const type = document.createElement("span");
  type.className = "content-badge";
  type.textContent = `${item.content_type_label || "其他"} · ${sourceLabel(item)}`;
  const status = document.createElement("span");
  status.className = "status-badge";
  status.textContent = item.status || "已入库";
  badges.append(type, status);
  const title = document.createElement("h1");
  title.textContent = item.title;
  const byline = document.createElement("div");
  byline.className = "article-byline";
  [item.author || "未知作者", `发布于 ${item.published_display || "时间未知"}`, `采集于 ${item.captured_display || "时间未知"}`].forEach((value) => {
    const span = document.createElement("span");
    span.textContent = value;
    byline.append(span);
  });
  copy.append(badges, title, byline);
  if (item.tags?.length) {
    const tags = document.createElement("div");
    tags.className = "article-tags";
    item.tags.forEach((tag) => {
      const node = document.createElement("span");
      node.className = "item-tag";
      node.textContent = `#${tag}`;
      tags.append(node);
    });
    copy.append(tags);
  }
  const actions = document.createElement("div");
  actions.className = "article-actions";
  if (isDatabaseManaged(item)) {
    const favorite = makeFavoriteButton(item, "article-favorite-button");
    const favoriteLabel = document.createElement("span");
    favoriteLabel.dataset.favoriteLabel = "";
    favoriteLabel.textContent = item.favorite ? "取消收藏" : "收藏";
    favorite.append(favoriteLabel);
    actions.append(favorite);
  }
  if (item.original_url) {
    const source = document.createElement("a");
    source.className = "article-source-link";
    source.href = item.original_url;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    source.append(document.createTextNode("打开原作品"), svgIcon("external-link"));
    actions.append(source);
  }
  if (isDatabaseManaged(item)) {
    const remove = document.createElement("button");
    remove.type = "button";
    remove.className = "danger-text-button article-delete-button";
    remove.append(svgIcon("trash-2"), document.createTextNode("删除文章"));
    remove.addEventListener("click", () => openDestructiveDialog({
      title: "删除整条资料？",
      description: `《${item.title}》的文章、原始记录、机器数据、视频、图片和封面将移入抖库废纸篓，并从检索与专题中移除。`,
      confirmLabel: "移到废纸篓",
      action: async () => {
        return api(`/api/articles/${encodeURIComponent(item.entry_id)}`, {
          method: "DELETE", body: JSON.stringify({confirmed: true}),
        });
      },
      successMessage: "资料已移到废纸篓",
      onSuccess: async () => {
        state.currentEntry = "";
        await Promise.all([loadLibrary({showLoading: false}), loadTopics(), loadTrash()]);
        showLibrary();
      },
    }));
    actions.append(remove);
  }
  if (actions.childElementCount) copy.append(actions);
  hero.append(copy);
  return hero;
}

function prefersReducedMotion() {
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function renderArticleData(data, entryId, push) {
  const root = $("#article-content");
  state.currentEntry = entryId;
  state.currentArticleItem = data.item;
  state.currentTopic = "";
  const body = document.createElement("div");
  body.className = "article-body";
  body.innerHTML = data.html;
  const hero = makeArticleHeader(data.item);
  root.replaceChildren(hero, body);
  hideLegacyViews();
  $("#article-view").classList.remove("hidden");
  window.Douku?.setPage("article");
  if (push) history.pushState({entryId}, "", `/articles/${encodeURIComponent(entryId)}`);
  document.title = `${data.item.title} · 抖库`;
  renderLibrary();
  updateChatContext();
  closeDrawers();
  $(".library-main").scrollTo({top: 0, behavior: "instant"});
  return hero;
}

function focusCitation(citation) {
  const root = $("#article-content .article-body");
  if (!root) return;
  const candidates = [];
  if (citation.image_index) candidates.push(`第 ${citation.image_index} 张`);
  if (citation.timestamp_ms != null) candidates.push(formatLocation(citation));
  if (citation.snippet) candidates.push(citation.snippet.trim().slice(0, 32));
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
  while (walker.nextNode()) {
    const textNode = walker.currentNode;
    const match = candidates.find((candidate) => candidate && textNode.nodeValue.includes(candidate));
    if (!match) continue;
    const mark = document.createElement("mark");
    mark.className = "citation-highlight";
    const start = textNode.nodeValue.indexOf(match);
    const range = document.createRange();
    range.setStart(textNode, start);
    range.setEnd(textNode, start + match.length);
    range.surroundContents(mark);
    mark.scrollIntoView({behavior: prefersReducedMotion() ? "auto" : "smooth", block: "center"});
    window.setTimeout(() => mark.classList.add("settled"), 1800);
    return;
  }
  $("#article-content").scrollIntoView({behavior: "smooth", block: "start"});
}

function showArticleFallbackEntry() {
  const view = $("#article-view");
  view.classList.remove("article-fallback-enter");
  window.requestAnimationFrame(() => view.classList.add("article-fallback-enter"));
  window.setTimeout(() => view.classList.remove("article-fallback-enter"), 360);
}

function renderArticleError(error) {
  const root = $("#article-content");
  state.currentEntry = "";
  state.currentArticleItem = null;
  $("#library-view").classList.add("hidden");
  $("#article-view").classList.remove("hidden");
  const empty = document.createElement("section");
  empty.className = "empty-state error-state";
  empty.append(svgIcon("alert-circle"));
  const title = document.createElement("h2");
  title.textContent = "文章无法打开";
  const copy = document.createElement("p");
  copy.textContent = error.message || "文章不存在或已经移动。";
  const back = document.createElement("button");
  back.type = "button";
  back.className = "secondary-button";
  back.textContent = "返回资料库";
  back.addEventListener("click", () => showLibrary());
  empty.append(title, copy, back);
  root.replaceChildren(empty);
  showArticleFallbackEntry();
}

function hideCardOverlays(source) {
  const card = source.closest(".gallery-card");
  if (!card) return () => {};
  const hidden = [];
  card.querySelectorAll(".topic-selection-indicator").forEach((node) => {
    hidden.push(node);
    node.style.visibility = "hidden";
  });
  return () => {
    hidden.forEach((node) => { node.style.visibility = ""; });
  };
}

async function openArticle(entryId, push = true, transitionSource = null, citation = null) {
  const root = $("#article-content");
  const sourceIsUsable = transitionSource?.isConnected && !$("#library-view").classList.contains("hidden");
  const sourceLink = transitionSource?.closest("a");
  if (sourceLink) sourceLink.setAttribute("aria-busy", "true");
  if (!sourceIsUsable) {
    root.replaceChildren();
    const loading = document.createElement("div");
    loading.className = "library-status";
    loading.append(Object.assign(document.createElement("div"), {className: "skeleton-row"}));
    root.append(loading);
    $("#library-view").classList.add("hidden");
    $("#article-view").classList.remove("hidden");
  }
  try {
    const data = await api(`/api/articles/${encodeURIComponent(entryId)}`);
    const canTransition = sourceIsUsable
      && typeof document.startViewTransition === "function"
      && !prefersReducedMotion();
    if (canTransition) {
      transitionSource.style.viewTransitionName = "active-album-cover";
      const restoreOverlays = hideCardOverlays(transitionSource);
      let transitionTarget = null;
      let rendered = false;
      const cleanTransitionNames = () => {
        transitionSource.style.viewTransitionName = "";
        if (transitionTarget) transitionTarget.style.viewTransitionName = "";
        restoreOverlays();
      };
      try {
        const transition = document.startViewTransition(() => {
          const hero = renderArticleData(data, entryId, push);
          rendered = true;
          transitionTarget = hero;
          transitionTarget.style.viewTransitionName = "active-album-cover";
        });
        transition.finished.then(cleanTransitionNames, cleanTransitionNames);
      } catch (_error) {
        cleanTransitionNames();
        if (!rendered) renderArticleData(data, entryId, push);
        showArticleFallbackEntry();
      }
    } else {
      renderArticleData(data, entryId, push);
      if (sourceIsUsable) showArticleFallbackEntry();
    }
    if (citation) window.setTimeout(() => focusCitation(citation), 50);
  } catch (error) {
    renderArticleError(error);
  } finally {
    sourceLink?.removeAttribute("aria-busy");
  }
}

function positionFilterPopover() {
  const toggle = $("#filter-toggle");
  const popover = $("#filter-popover");
  if (!toggle || !popover) return;
  const rect = toggle.getBoundingClientRect();
  const viewportPadding = 16;
  const gap = 8;
  const width = Math.min(380, Math.max(0, window.innerWidth - viewportPadding * 2));
  const left = Math.max(
    viewportPadding,
    Math.min(rect.right - width, window.innerWidth - width - viewportPadding),
  );
  popover.style.setProperty("--filter-popover-top", `${rect.bottom + gap}px`);
  popover.style.setProperty("--filter-popover-left", `${left}px`);
}

function openFilterPopover(focusTags = false) {
  positionFilterPopover();
  $("#filter-popover").classList.remove("hidden");
  $("#filter-toggle").setAttribute("aria-expanded", "true");
  window.setTimeout(() => {
    const target = focusTags ? $("#tag-filters .choice-button") : $("#filter-popover");
    target?.focus();
  }, 0);
}

function closeFilterPopover() {
  $("#filter-popover").classList.add("hidden");
  $("#filter-toggle").setAttribute("aria-expanded", "false");
}

function renderPanelState() {
  const shell = $("#app-shell");
  const desktopSidebar = window.matchMedia("(min-width: 960px)").matches;
  const desktopChat = window.matchMedia("(min-width: 1280px)").matches;
  shell.classList.toggle("is-sidebar-collapsed", desktopSidebar && state.sidebarCollapsed);
  shell.classList.toggle("is-chat-collapsed", desktopChat && state.chatCollapsed);
  $("#chat-restore").classList.toggle("hidden", !desktopChat || !state.chatCollapsed);
  $("#chat-toggle").setAttribute("aria-expanded", String(desktopChat ? !state.chatCollapsed : state.activeDrawer === "chat"));
  if (!$("#filter-popover").classList.contains("hidden")) positionFilterPopover();
}

function setSidebarOpen(open) {
  if (window.matchMedia("(max-width: 959px)").matches) {
    if (open) openDrawer("sidebar"); else closeDrawers();
    return;
  }
  state.sidebarCollapsed = !open;
  writePreference(STORAGE.sidebar, state.sidebarCollapsed);
  renderPanelState();
}

function setChatPanelOpen(open) {
  if (window.matchMedia("(max-width: 1279px)").matches) {
    if (open) openDrawer("chat"); else closeDrawers();
    return;
  }
  state.chatCollapsed = !open;
  writePreference(STORAGE.chat, state.chatCollapsed);
  renderPanelState();
}

function focusableElements(root) {
  return $$("a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex='-1'])", root)
    .filter((node) => !node.closest(".hidden"));
}

function openDrawer(kind) {
  closeDrawers(false);
  state.activeDrawer = kind;
  const shell = $("#app-shell");
  shell.classList.toggle("sidebar-drawer-open", kind === "sidebar");
  shell.classList.toggle("chat-drawer-open", kind === "chat");
  $("#drawer-backdrop").classList.remove("hidden");
  document.body.classList.add("drawer-open");
  const trigger = kind === "sidebar" ? $("#nav-toggle") : $("#chat-toggle");
  trigger.setAttribute("aria-expanded", "true");
  focusableElements(kind === "sidebar" ? $("#library-sidebar") : $("#chat-panel"))[0]
    ?.focus({preventScroll: true});
}

function closeDrawers(restoreFocus = true) {
  const previous = state.activeDrawer;
  state.activeDrawer = "";
  $("#app-shell").classList.remove("sidebar-drawer-open", "chat-drawer-open");
  $("#drawer-backdrop").classList.add("hidden");
  document.body.classList.remove("drawer-open");
  $("#nav-toggle").setAttribute("aria-expanded", "false");
  if (!window.matchMedia("(min-width: 1280px)").matches) $("#chat-toggle").setAttribute("aria-expanded", "false");
  if (restoreFocus && previous) (previous === "sidebar" ? $("#nav-toggle") : $("#chat-toggle")).focus();
}

function trapDrawerFocus(event) {
  if (!state.activeDrawer || event.key !== "Tab") return;
  const panel = state.activeDrawer === "sidebar" ? $("#library-sidebar") : $("#chat-panel");
  const focusable = focusableElements(panel);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable.at(-1);
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
}

function commandHaystack(item) {
  return [item.title, item.author, item.summary, ...(item.tags || []), ...(item.inspirations || []).map((value) => value.text)].join(" ").toLocaleLowerCase("zh-CN");
}

function commandMatches() {
  const query = $("#command-input").value.trim().toLocaleLowerCase("zh-CN");
  return (state.allItems.length ? state.allItems : state.items)
    .filter((item) => !query || commandHaystack(item).includes(query))
    .slice(0, 12);
}

function renderCommandResults() {
  const root = $("#command-results");
  const items = commandMatches();
  state.commandIndex = Math.max(0, Math.min(state.commandIndex, items.length - 1));
  root.replaceChildren(...items.map((item, index) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = `command-result${index === state.commandIndex ? " active" : ""}`;
    button.setAttribute("role", "option");
    button.setAttribute("aria-selected", String(index === state.commandIndex));
    const copy = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = item.title;
    const meta = document.createElement("span");
    meta.textContent = `${item.author} · ${item.content_type_label}`;
    copy.append(title, meta);
    button.append(makeCover(item, "item-thumbnail"), copy);
    button.addEventListener("mouseenter", () => { state.commandIndex = index; renderCommandResults(); });
    button.addEventListener("click", () => {
      $("#command-dialog").close();
      openArticle(item.entry_id);
    });
    return button;
  }));
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "command-help";
    empty.textContent = "没有找到匹配文章";
    root.append(empty);
  }
}

async function openCommandMenu() {
  await ensureCommandItems();
  state.commandIndex = 0;
  $("#command-input").value = state.query;
  renderCommandResults();
  $("#command-dialog").showModal();
  $("#command-input").focus();
}

function closeCommandMenu() {
  if ($("#command-dialog").open) $("#command-dialog").close();
}

async function loadSessions(preferNew = false) {
  try {
    const data = await api("/api/chat/sessions");
    state.sessions = data.sessions;
    $("#model-label").textContent = `模型：${data.configured ? data.model : "未配置"}`;
    $("#chat-persistent-error").classList.toggle("hidden", data.configured);
    $("#chat-persistent-error").textContent = data.configured ? "" : "尚未配置对话模型。资料浏览不受影响，请前往模型设置完成配置。";
    if (preferNew) {
      await createSession();
    } else {
      if (!state.sessions.some((item) => item.id === state.currentSession)) state.currentSession = state.sessions[0]?.id || "";
      renderSessionSelect();
      if (state.currentSession) await loadMessages(); else $("#chat-messages").replaceChildren(makeWelcome());
    }
  } catch (error) {
    $("#chat-persistent-error").textContent = error.message;
    $("#chat-persistent-error").classList.remove("hidden");
  }
}

async function createSession() {
  const item = currentEntryItem();
  const payload = state.currentTopic
    ? {scope: "topic", context_topic_id: state.currentTopic}
    : isDatabaseManaged(item)
      ? {scope: "entry", context_entry_id: state.currentEntry}
      : {scope: "library"};
  const session = await api("/api/chat/sessions", {method: "POST", body: JSON.stringify(payload)});
  state.currentSession = session.id;
  await loadSessions(false);
}

function renderSessionSelect() {
  const options = state.sessions.map((session) => {
    const option = document.createElement("option");
    option.value = session.id;
    option.textContent = session.title;
    option.selected = session.id === state.currentSession;
    return option;
  });
  if (!options.length) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "暂无对话";
    options.push(option);
  }
  $("#session-select").replaceChildren(...options);
  updateChatContext();
}

function setChatContext(iconName, text) {
  const root = $("#chat-context");
  const copy = document.createElement("span");
  copy.textContent = text;
  root.replaceChildren(svgIcon(iconName), copy);
  root.title = text;
}

function updateChatContext() {
  const item = currentEntryItem();
  const session = state.sessions.find((value) => value.id === state.currentSession);
  const topicValue = state.topics.find((value) => value.topic.id === state.currentTopic);
  if (topicValue && session?.scope === "topic" && session.context_topic_id === state.currentTopic) {
    setChatContext("bookmark", `当前专题：${topicValue.topic.title}`);
  } else if (topicValue) {
    setChatContext("bookmark", `当前专题：${topicValue.topic.title} · 发送时会新建专题对话`);
  } else if (item && !isDatabaseManaged(item)) {
    setChatContext("file-text", `当前文章：${item.title} · 只读 Markdown，发送时使用全库对话`);
  } else if (item && session?.scope === "entry" && session.context_entry_id === item.entry_id) {
    setChatContext("file-text", `当前文章：${item.title}`);
  } else if (item) {
    setChatContext("file-text", `当前文章：${item.title} · 发送时会新建本文对话`);
  } else if (session?.scope === "entry" || session?.scope === "topic") {
    setChatContext("library", "当前会话属于其他范围；发送时会新建全库对话");
  } else {
    setChatContext("library", "整个资料库");
  }
}

async function loadMessages() {
  try {
    const data = await api(`/api/chat/sessions/${state.currentSession}`);
    const box = $("#chat-messages");
    box.replaceChildren(...(data.messages.length
      ? data.messages.map((message) => makeMessage(message.role, message.content, message.html, message.citations))
      : [makeWelcome()]));
    const latestAssistant = [...data.messages]
      .reverse()
      .find((message) => message.role === "assistant");
    $("#token-label").textContent = latestAssistant?.total_tokens != null
      ? `最近一次用量：${latestAssistant.total_tokens} token`
      : "用量：—";
    box.scrollTop = box.scrollHeight;
  } catch (error) {
    $("#chat-persistent-error").textContent = error.message;
    $("#chat-persistent-error").classList.remove("hidden");
  }
}

function makeWelcome() {
  const root = document.createElement("div");
  root.className = "chat-welcome";
  const mark = document.createElement("span");
  mark.className = "ai-mark";
  mark.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><g fill="currentColor" stroke="none"><path d="M12 2c.6 4.8 3.2 7.4 8 8-4.8.6-7.4 3.2-8 8-.6-4.8-3.2-7.4-8-8 4.8-.6 7.4-3.2 8-8Z"/><path d="M18.7 3c.25 2.05 1.35 3.2 3.3 3.45-1.95.25-3.05 1.4-3.3 3.45-.25-2.05-1.35-3.2-3.3-3.45 1.95-.25 3.05-1.4 3.3-3.45Z" opacity=".45"/></g></svg>';
  const title = document.createElement("h3");
  title.textContent = "问问你的知识库";
  const copy = document.createElement("p");
  copy.textContent = "回答会区分作品原话与 AI 推断，并附上可验证的文章引用。";
  root.append(mark, title, copy);
  return root;
}

function formatLocation(citation) {
  if (citation.image_index) return `第 ${citation.image_index} 张图片`;
  if (citation.timestamp_ms != null) {
    const seconds = Math.floor(citation.timestamp_ms / 1000);
    return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
  }
  return "文章片段";
}

function makeCitation(citation, index) {
  const row = document.createElement("section");
  row.className = "citation";
  const number = document.createElement("span");
  number.className = "citation-number";
  number.textContent = String(index + 1);
  const copy = document.createElement("div");
  copy.className = "citation-copy";
  const title = document.createElement("a");
  title.className = "citation-title";
  title.href = `/articles/${encodeURIComponent(citation.entry_id)}`;
  title.textContent = citation.article_title;
  title.addEventListener("click", (event) => {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
    event.preventDefault();
    openArticle(citation.entry_id, true, null, citation);
  });
  const meta = document.createElement("div");
  meta.className = "citation-meta";
  const location = document.createElement("span");
  location.textContent = formatLocation(citation);
  meta.append(location);
  if (citation.original_url) {
    let safeSourceUrl = null;
    try {
      const parsed = new URL(citation.original_url);
      if (["http:", "https:"].includes(parsed.protocol)) safeSourceUrl = parsed.href;
    } catch (_error) {
      safeSourceUrl = null;
    }
    if (safeSourceUrl) {
    const source = document.createElement("a");
    source.href = safeSourceUrl;
    source.target = "_blank";
    source.rel = "noopener noreferrer";
    source.textContent = "原作品";
    source.append(svgIcon("external-link"));
    meta.append(source);
    }
  }
  copy.append(title, meta);
  if (citation.snippet) {
    const preview = document.createElement("p");
    preview.className = "citation-preview";
    preview.textContent = citation.snippet;
    copy.append(preview);
    row.title = citation.snippet;
  }
  row.append(number, copy);
  return row;
}

function makeMessage(role, content, html = "", citations = []) {
  const node = document.createElement("div");
  node.className = `message ${role}`;
  if (role === "assistant" && html) node.innerHTML = html;
  else node.textContent = content;
  if (role !== "system") {
    const actions = document.createElement("div");
    actions.className = "message-actions";
    const currentItem = currentEntryItem();
    if (!state.currentEntry || isDatabaseManaged(currentItem)) {
      const save = document.createElement("button");
      save.type = "button";
      save.className = "save-inspiration";
      save.append(svgIcon("sparkles"), document.createTextNode("保存为灵感"));
      save.addEventListener("click", () => openInspiration(content, save));
      actions.append(save);
    }
    if (state.currentTopic) {
      const note = document.createElement("button");
      note.type = "button";
      note.className = "save-inspiration";
      note.append(svgIcon("bookmark"), document.createTextNode("保存为专题笔记"));
      note.addEventListener("click", async () => {
        if (!window.confirm("将这段内容逐字保存为当前专题笔记？")) return;
        try {
          await api(`/api/topics/${encodeURIComponent(state.currentTopic)}/notes`, {
            method: "POST",
            body: JSON.stringify({content, title: "对话笔记", confirmed: true}),
          });
          await openTopic(state.currentTopic, false);
          toast("专题笔记已逐字保存");
        } catch (error) {
          toast(error.message);
        }
      });
      actions.append(note);
    }
    node.append(actions);
  }
  if (citations?.length) {
    const list = document.createElement("div");
    list.className = "citations";
    citations.forEach((citation, index) => list.append(makeCitation(citation, index)));
    node.append(list);
  }
  return node;
}

function parseSSEChunk(buffer, onEvent) {
  const blocks = buffer.replaceAll("\r\n", "\n").split("\n\n");
  const rest = blocks.pop();
  blocks.forEach((block) => {
    let name = "message";
    const dataLines = [];
    block.split("\n").forEach((line) => {
      if (line.startsWith("event:")) name = line.slice(6).trim();
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trimStart());
    });
    if (!dataLines.length) return;
    try {
      onEvent(name, JSON.parse(dataLines.join("\n")));
    } catch (_error) {
      onEvent("error", {message: "收到的流式响应格式不完整，请重试。"});
    }
  });
  return rest;
}

function setSending(sending) {
  state.sending = sending;
  const button = $("#send-button");
  button.disabled = sending;
  button.setAttribute("aria-label", sending ? "正在生成回答" : "发送消息");
  button.replaceChildren(svgIcon(sending ? "square" : "send"));
  $("#screen-reader-status").textContent = sending ? "正在生成回答" : "回答完成";
}

async function sendChat(text) {
  if (state.sending) return;
  const current = state.sessions.find((item) => item.id === state.currentSession);
  const articleItem = currentEntryItem();
  const articleChatEnabled = isDatabaseManaged(articleItem);
  const needsArticle = articleChatEnabled
    && (current?.scope !== "entry" || current?.context_entry_id !== state.currentEntry);
  const needsTopic = state.currentTopic && (current?.scope !== "topic" || current?.context_topic_id !== state.currentTopic);
  const needsLibrary = !state.currentTopic && !articleChatEnabled && current?.scope !== "library";
  try {
    if (!state.currentSession || needsArticle || needsTopic || needsLibrary) await createSession();
  } catch (error) {
    $("#chat-persistent-error").textContent = error.message;
    $("#chat-persistent-error").classList.remove("hidden");
    return;
  }
  setSending(true);
  $("#chat-persistent-error").classList.add("hidden");
  const box = $("#chat-messages");
  $(".chat-welcome", box)?.remove();
  box.append(makeMessage("user", text));
  const answer = makeMessage("assistant", "");
  answer.textContent = "";
  box.append(answer);
  box.scrollTop = box.scrollHeight;
  try {
    const response = await fetch(`/api/chat/sessions/${state.currentSession}/messages`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({content: text}),
    });
    if (!response.ok || !response.body) {
      const error = await response.json().catch(() => ({}));
      throw new Error(error.detail || "发送失败");
    }
    const reader = response.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let raw = "";
    let citations = [];
    let completed = false;
    const onEvent = (name, data) => {
      if (name === "meta") citations = data.citations || [];
      if (name === "delta") {
        raw += data.text;
        answer.textContent = raw;
      }
      if (name === "error") {
        const message = data.message || "AI 对话暂时不可用";
        answer.textContent = message;
        answer.classList.add("error");
        $("#chat-persistent-error").textContent = message;
        $("#chat-persistent-error").classList.remove("hidden");
      }
      if (name === "done") {
        completed = true;
        answer.replaceWith(makeMessage("assistant", raw, data.message.html, citations));
        const usage = data.usage;
        $("#token-label").textContent = usage?.total_tokens != null
          ? `本次用量：${usage.total_tokens} token`
          : "本次用量：未知";
      }
      box.scrollTop = box.scrollHeight;
    };
    while (true) {
      const {value, done} = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), {stream: !done});
      buffer = parseSSEChunk(buffer, onEvent);
      if (done) break;
    }
    if (!completed && raw) answer.replaceWith(makeMessage("assistant", raw, "", citations));
    await loadSessions(false);
  } catch (error) {
    const message = error.message || "AI 对话暂时不可用";
    answer.textContent = message;
    answer.classList.add("error");
    $("#chat-persistent-error").textContent = message;
    $("#chat-persistent-error").classList.remove("hidden");
  } finally {
    setSending(false);
  }
}

function resizeChatInput() {
  const input = $("#chat-input");
  input.style.height = "auto";
  input.style.height = `${Math.min(input.scrollHeight, 160)}px`;
}

function fillInspirationTargets() {
  const items = (state.allItems.length ? state.allItems : state.items).filter(isDatabaseManaged);
  $("#inspiration-entry").replaceChildren(...items.map((item) => {
    const option = document.createElement("option");
    option.value = item.entry_id;
    option.textContent = item.title;
    return option;
  }));
}

function openInspiration(text, trigger) {
  if (state.currentEntry && !isDatabaseManaged(currentEntryItem())) {
    toast("只读 Markdown 文章不能保存灵感");
    return;
  }
  const items = (state.allItems.length ? state.allItems : state.items).filter(isDatabaseManaged);
  if (!items.length) {
    toast("资料库中还没有可保存的文章");
    return;
  }
  state.lastDialogTrigger = trigger || document.activeElement;
  $("#inspiration-text").value = text;
  $("#inspiration-quote").value = "";
  $("#inspiration-start").value = "";
  $("#inspiration-end").value = "";
  $("#inspiration-error").classList.add("hidden");
  $("#inspiration-entry").value = state.currentEntry || items[0].entry_id;
  $("#inspiration-dialog").showModal();
  window.setTimeout(() => $("#inspiration-entry").focus(), 0);
}

async function saveInspiration() {
  const start = $("#inspiration-start").value;
  const end = $("#inspiration-end").value;
  const payload = {
    entry_id: $("#inspiration-entry").value,
    text: $("#inspiration-text").value,
    quote: $("#inspiration-quote").value || null,
    start_ms: start ? Number(start) : null,
    end_ms: end ? Number(end) : null,
    confirmed: true,
  };
  const errorNode = $("#inspiration-error");
  errorNode.classList.add("hidden");
  try {
    await api("/api/inspirations/confirm", {method: "POST", body: JSON.stringify(payload)});
    $("#inspiration-dialog").close();
    toast("灵感已逐字保存");
    await loadLibrary({showLoading: false});
    if (state.currentEntry === payload.entry_id) await openArticle(payload.entry_id, false);
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.classList.remove("hidden");
  }
}

function containsDouyinURL(value) {
  const matches = value.match(/https?:\/\/[^\s<>\]\[)(]+/giu) || [];
  return matches.some((raw) => {
    const candidate = raw.replace(/[.,;:!?'"，。；：！？）]+$/u, "");
    try {
      const host = new URL(candidate).hostname.toLowerCase();
      return ["douyin.com", "iesdouyin.com"].some(
        (suffix) => host === suffix || host.endsWith(`.${suffix}`),
      );
    } catch (_error) {
      return false;
    }
  });
}

function openCaptureDialog() {
  $("#capture-share-text").value = "";
  $("#capture-error").classList.add("hidden");
  $("#capture-dialog").showModal();
  window.setTimeout(() => $("#capture-share-text").focus(), 0);
}

function setCaptureSubmitting(submitting) {
  const button = $("#confirm-capture");
  button.disabled = submitting;
  button.textContent = submitting ? "正在加入…" : "加入写入队列";
}

async function queueCapture() {
  const shareText = $("#capture-share-text").value.trim();
  const errorNode = $("#capture-error");
  errorNode.classList.add("hidden");
  if (!containsDouyinURL(shareText)) {
    errorNode.textContent = "请粘贴有效的抖音链接或分享文案";
    errorNode.classList.remove("hidden");
    return;
  }
  setCaptureSubmitting(true);
  try {
    await api("/api/captures", {
      method: "POST",
      body: JSON.stringify({share_text: shareText}),
    });
    $("#capture-dialog").close();
    toast("已加入写入队列");
  } catch (error) {
    errorNode.textContent = error.message;
    errorNode.classList.remove("hidden");
  } finally {
    setCaptureSubmitting(false);
  }
}

function closeChatMenu() {
  $("#chat-menu").classList.add("hidden");
  $("#chat-menu-toggle").setAttribute("aria-expanded", "false");
}

function handleGlobalKeydown(event) {
  trapDrawerFocus(event);
  const target = event.target;
  const editing = target.matches("input, textarea, select, [contenteditable='true']");
  if ((event.metaKey || event.ctrlKey) && event.key.toLowerCase() === "k") {
    event.preventDefault();
    openCommandMenu();
    return;
  }
  if (!editing && event.key === "/") {
    event.preventDefault();
    $("#search-input").focus();
    return;
  }
  if (!editing && event.key === "[") setSidebarOpen(state.sidebarCollapsed);
  if (!editing && event.key === "]") setChatPanelOpen(state.chatCollapsed);
  if (event.key === "Escape") {
    if (state.activeDrawer) closeDrawers();
    closeFilterPopover();
    closeImportsPopover();
    closeChatMenu();
  }
}

function bindEvents() {
  $$(".nav-item[data-section]").forEach((node) => node.addEventListener("click", () => {
    state.section = node.dataset.section;
    state.authors = [];
    state.types = [];
    state.tags = [];
    state.sources = [];
    state.inspirationOnly = false;
    renderFilters();
    showLibrary(false);
    writeStateToURL("push");
  }));
  $("#topics-nav").addEventListener("click", () => showTopics());
  $("#trash-nav").addEventListener("click", () => showTrash());
  $$("[data-route]").forEach((node) => node.addEventListener("click", (event) => {
    const path = node.getAttribute("data-route") || node.getAttribute("href");
    if (!path || !path.startsWith("/")) return;
    event.preventDefault();
    closeImportsPopover();
    window.Douku?.navigate(path);
  }));
  $$("[data-back='library']").forEach((node) => node.addEventListener("click", () => showLibrary()));
  $("#back-jobs")?.addEventListener("click", () => window.Douku?.navigate("/jobs"));
  $("#imports-toggle")?.addEventListener("click", () => {
    if ($("#imports-popover").classList.contains("hidden")) openImportsPopover();
    else closeImportsPopover();
  });
  $("#imports-close")?.addEventListener("click", closeImportsPopover);
  $("#confirm-capture")?.addEventListener("click", (event) => {
    event.preventDefault();
    if (!$("#capture-form").reportValidity()) return;
    queueCapture();
  });
  $("#capture-share-text")?.addEventListener("input", () => $("#capture-error").classList.add("hidden"));
  $("#capture-dialog")?.addEventListener("close", () => $("#imports-toggle")?.focus());
  $("#topic-select-toggle").addEventListener("click", () => {
    if (!state.topicSelectionMode) {
      state.topicSelectionMode = true;
      state.selectedEntryIds.clear();
      if (state.currentEntry || state.currentTopic || !$("#topics-view").classList.contains("hidden")) showLibrary();
      renderLibrary();
      toast("请选择要加入专题的文章");
    } else if (state.selectedEntryIds.size) {
      openTopicDialog();
    } else {
      state.topicSelectionMode = false;
      renderLibrary();
    }
  });
  $("#topics-create-button").addEventListener("click", () => {
    const hasManagedItems = [...state.items, ...state.allItems, state.currentArticleItem]
      .some(isDatabaseManaged);
    if (!hasManagedItems) return;
    state.topicSelectionMode = true;
    state.selectedEntryIds.clear();
    showLibrary();
    toast("请选择要加入专题的文章");
  });
  $("#confirm-topic").addEventListener("click", (event) => {
    event.preventDefault();
    createTopicFromSelection();
  });
  let searchTimer = 0;
  $("#search-input").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    window.clearTimeout(searchTimer);
    searchTimer = window.setTimeout(() => {
      writeStateToURL();
      loadLibrary({showLoading: false});
    }, 200);
  });
  $("#filter-toggle").addEventListener("click", () => {
    if ($("#filter-popover").classList.contains("hidden")) openFilterPopover();
    else closeFilterPopover();
  });
  $("#filter-close").addEventListener("click", closeFilterPopover);
  $("#apply-filters").addEventListener("click", closeFilterPopover);
  $("#all-tags-button").addEventListener("click", () => openFilterPopover(true));
  $("#clear-filters").addEventListener("click", () => {
    state.authors = []; state.types = []; state.tags = []; state.sources = []; state.inspirationOnly = false;
    renderFilters(); renderLibrary(); writeStateToURL();
  });
  $("#inspiration-only").addEventListener("change", (event) => {
    state.inspirationOnly = event.target.checked;
    renderFilters(); renderLibrary(); writeStateToURL();
  });
  $("#sort-select").addEventListener("change", (event) => {
    state.sort = VALID_SORTS.has(event.target.value) ? event.target.value : "captured_desc";
    renderLibrary(); writeStateToURL();
  });
  $$('[data-library-view]').forEach((node) => node.addEventListener("click", () => {
    state.libraryView = node.dataset.libraryView;
    writePreference(STORAGE.view, state.libraryView);
    renderToolbarState(); renderLibrary(); writeStateToURL();
  }));
  $("#density-toggle").addEventListener("click", () => {
    state.density = state.density === "compact" ? "comfortable" : "compact";
    writePreference(STORAGE.density, state.density);
    renderToolbarState(); renderLibrary();
  });
  $("#empty-clear").addEventListener("click", clearSearchAndFilters);
  $("#library-retry").addEventListener("click", () => loadLibrary({showLoading: true}));
  $("#back-library").addEventListener("click", () => showLibrary());
  $("#back-topics").addEventListener("click", () => showTopics());
  $("#sidebar-collapse").addEventListener("click", () => setSidebarOpen(false));
  $("#sidebar-restore").addEventListener("click", () => setSidebarOpen(true));
  $("#nav-toggle").addEventListener("click", () => setSidebarOpen(true));
  $("#chat-toggle").addEventListener("click", () => setChatPanelOpen(true));
  $("#chat-close").addEventListener("click", () => setChatPanelOpen(false));
  $("#chat-restore").addEventListener("click", () => setChatPanelOpen(true));
  $("#drawer-backdrop").addEventListener("click", closeDrawers);
  $("#chat-menu-toggle").addEventListener("click", () => {
    const open = $("#chat-menu").classList.contains("hidden");
    $("#chat-menu").classList.toggle("hidden", !open);
    $("#chat-menu-toggle").setAttribute("aria-expanded", String(open));
  });
  $("#new-session").addEventListener("click", () => createSession());
  $("#delete-session").addEventListener("click", async () => {
    closeChatMenu();
    if (!state.currentSession || !window.confirm("删除当前对话及全部历史消息？此操作无法撤销。")) return;
    try {
      await api(`/api/chat/sessions/${state.currentSession}`, {method: "DELETE"});
      state.currentSession = "";
      await loadSessions();
      toast("对话已删除");
    } catch (error) {
      $("#chat-persistent-error").textContent = error.message;
      $("#chat-persistent-error").classList.remove("hidden");
    }
  });
  $("#session-select").addEventListener("change", async (event) => {
    state.currentSession = event.target.value;
    if (state.currentSession) await loadMessages();
    updateChatContext();
  });
  $("#chat-form").addEventListener("submit", (event) => {
    event.preventDefault();
    const input = $("#chat-input");
    const text = input.value.trim();
    if (!text) return;
    input.value = "";
    resizeChatInput();
    sendChat(text);
  });
  $("#chat-input").addEventListener("input", resizeChatInput);
  $("#chat-input").addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      $("#chat-form").requestSubmit();
    }
  });
  $("#confirm-inspiration").addEventListener("click", (event) => {
    event.preventDefault();
    if (!$("#inspiration-form").reportValidity()) return;
    saveInspiration();
  });
  $("#confirm-destructive").addEventListener("click", (event) => {
    event.preventDefault();
    runDestructiveAction();
  });
  $("#destructive-dialog").addEventListener("close", () => {
    if ($("#destructive-dialog").returnValue === "cancel") state.destructiveAction = null;
  });
  $("#inspiration-dialog").addEventListener("close", () => state.lastDialogTrigger?.focus());
  $("#command-input").addEventListener("input", () => { state.commandIndex = 0; renderCommandResults(); });
  $("#command-input").addEventListener("keydown", (event) => {
    const length = commandMatches().length;
    if (event.key === "ArrowDown" && length) { event.preventDefault(); state.commandIndex = (state.commandIndex + 1) % length; renderCommandResults(); }
    if (event.key === "ArrowUp" && length) { event.preventDefault(); state.commandIndex = (state.commandIndex - 1 + length) % length; renderCommandResults(); }
    if (event.key === "Enter" && length) { event.preventDefault(); $("#command-results .command-result.active")?.click(); }
  });
  window.addEventListener("douku:route", (event) => {
    if (event.detail.path !== "/imports") return;
    hideLegacyViews();
    window.Douku?.setPage("imports");
    $("#imports-view").classList.remove("hidden");
    document.title = "导入内容 · 抖库";
  });
  $$('[data-theme-select]').forEach((select) => select.addEventListener("change", (event) => window.DoukuTheme?.set(event.target.value)));
  document.addEventListener("keydown", handleGlobalKeydown);
  document.addEventListener("click", (event) => {
    if (!event.target.closest("#filter-popover, #filter-toggle, #all-tags-button")) closeFilterPopover();
    if (!event.target.closest("#imports-popover, #imports-toggle")) closeImportsPopover();
    if (!event.target.closest("#chat-menu, #chat-menu-toggle")) closeChatMenu();
  }, {capture: true});
  window.addEventListener("popstate", () => {
    const path = window.location.pathname;
    const match = path.match(/^\/articles\/([^/]+)$/);
    const topicMatch = path.match(/^\/topics\/([^/]+)$/);
    if (match) openArticle(decodeURIComponent(match[1]), false);
    else if (topicMatch) openTopic(decodeURIComponent(topicMatch[1]), false);
    else if (path === "/topics") showTopics(false);
    else if (path === "/trash") showTrash(false);
    else if (window.Douku?.isOperationPath(path)) window.Douku.navigate(path + window.location.search, false);
    else {
      readStateFromURL();
      $("#search-input").value = state.query;
      showLibrary(false);
      loadLibrary({showLoading: false});
    }
  });
  window.addEventListener("resize", () => {
    if (window.matchMedia("(min-width: 1280px)").matches) closeDrawers(false);
    renderPanelState();
    if (state.libraryLoaded) renderLibrary();
    if (!$("#filter-popover")?.classList.contains("hidden")) positionFilterPopover();
    if (!$("#imports-popover")?.classList.contains("hidden")) positionImportsPopover();
  });
}

document.addEventListener("DOMContentLoaded", async () => {
  readPreferences();
  readStateFromURL();
  $("#search-input").value = state.query;
  bindEvents();
  renderPanelState();
  renderToolbarState();
  await loadLibrary();
  await loadTopics();
  if (state.currentTopic) await openTopic(state.currentTopic, false);
  else if (state.currentEntry) await openArticle(state.currentEntry, false);
  else if (window.location.pathname === "/topics") await showTopics(false);
  else if (window.location.pathname === "/trash") await showTrash(false);
  else if (window.Douku?.isOperationPath(window.location.pathname)) {
    window.Douku.navigate(window.location.pathname + window.location.search, false);
  }
  await loadSessions();
  const events = new EventSource("/api/library/events");
  events.addEventListener("library", async () => {
    state.allItems = [];
    await loadLibrary({showLoading: false});
    await loadTopics();
    if (!$("#trash-view").classList.contains("hidden")) await loadTrash();
    if (state.currentEntry) await openArticle(state.currentEntry, false);
    if (state.currentTopic) await openTopic(state.currentTopic, false);
    toast("资料库已更新");
  });
  events.addEventListener("error", () => {
    // Native EventSource reconnects automatically; browsing remains available.
  });
});
