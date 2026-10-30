"use strict";
(() => {
  // src/dom.ts
  function $(id) {
    const node = document.getElementById(id);
    if (!node) throw Error(`Missing interface element #${id}`);
    return node;
  }
  var $input = (id) => $(id);
  var $select = (id) => $(id);
  var $area = (id) => $(id);
  var $button = (id) => $(id);
  var sessionToken = document.querySelector('meta[name="typelogic-token"]').content;
  var ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  function esc(value) {
    return String(value).replace(/[&<>"']/g, (c) => ESCAPES[c]);
  }
  function errorMessage(error) {
    return error instanceof Error ? error.message : String(error);
  }
  async function api(path, payload) {
    const response = await fetch(path, {
      method: payload === void 0 ? "GET" : "POST",
      headers: { "X-TypeLogic-Token": sessionToken, "Content-Type": "application/json" },
      ...payload === void 0 ? {} : { body: JSON.stringify(payload) }
    });
    const data = await response.json();
    if (!response.ok) throw Error(data && data.error || `Failure ${response.status}`);
    return data;
  }
  var toastTimer;
  function toast(message) {
    const node = $("toast");
    node.textContent = message;
    node.style.display = "block";
    clearTimeout(toastTimer);
    toastTimer = window.setTimeout(() => {
      node.style.display = "none";
    }, 7e3);
  }
  var reportError = (error) => toast(errorMessage(error));
  function download(name, value) {
    const url = URL.createObjectURL(new Blob([JSON.stringify(value, null, 2)], { type: "application/json" }));
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 2e3);
  }
  function downloadText(name, text, type = "application/x-ndjson") {
    const url = URL.createObjectURL(new Blob([text], { type }));
    const link = document.createElement("a");
    link.href = url;
    link.download = name;
    link.click();
    setTimeout(() => URL.revokeObjectURL(url), 2e3);
  }
  var space = () => $input("space").value.trim() || "default";
  async function shortExportName(id, format) {
    const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", new TextEncoder().encode(`${id}:${format}`)));
    const alphabet = "abcdefghijklmnopqrstuvwxyz234567";
    let buffer = 0, bits = 0, text = "";
    for (const byte of digest.slice(0, 5)) {
      buffer = buffer << 8 | byte;
      bits += 8;
      while (bits >= 5) {
        bits -= 5;
        text += alphabet[buffer >>> bits & 31];
      }
    }
    return text + ".json";
  }
  async function exportDecision(id, format) {
    try {
      download(await shortExportName(id, format), await api(`/api/decision?id=${encodeURIComponent(id)}&format=${format}`));
    } catch (e) {
      reportError(e);
    }
  }

  // src/help.ts
  var helpTexts = {
    json: 'Select JSON scenario for a valid object or array, such as {"order":42,"paid":true}. Leave it unchecked for ordinary text. Object keys are sorted before evaluation. JSON organizes facts; it does not compute values or guarantee accuracy.',
    case: "The case is what happened: the original message, e-mail, tool call or scenario, as one text. The title is optional: when you leave it empty, a short title is written after the evaluation (by the local Ollama model when it is already running, otherwise from the first sentence of the case). Tags group decisions in the Library: pick suggested tags or type your own, up to 8. Click the header to collapse or expand this block.",
    decisions: "Each decision is one question about the case: Yes / no, Choice or Score. Decisions are answered independently, in order, and the answers keep the question text. Add up to 32. Save as template keeps the case layout, evidence fields, tags and decisions for later. Click the header to collapse or expand this block.",
    results: "The answers of the last evaluation: for each decision the most likely answer, the probability of every alternative, confidence (the top probability, not a measured accuracy) and concentration. The decision is saved in the Library with its title, tags, a unique ID and SHA-512 / MD5 digests. Export Full or Lite JSON here, or delete the decision.",
    types: "Click Yes / no, Choice or Score to choose the output type of this decision. Yes / no returns the probability that the question is true. Choice picks one of your numbered options (0, 1, 2, ...) and returns a probability for each. Score rates the case on your numbered levels, from lowest to highest, and returns the expected level. You can switch the type at any time; the question text is kept.",
    order: "The primary engine repeats each Choice question with reversed options and one rotation, remaps probabilities to the original IDs and compares them. A winner change or any probability shift greater than 0.15 is flagged. This costs additional time; two stable permutations do not prove robustness. The fixed-profile specialist does not perform this test.",
    evidence: "Optional. Evidence holds the facts that matter as typed fields: true/false, numbers, integers, dates, text and enums. Template values are marked template; values you type are confirmed; values from Extract evidence are suggestions marked extracted until you keep (✓) or correct them. Extraction uses the local LLM in Ollama (qwen3.5:4b by default) or, for short texts, Needle. The engine reads the case and the evidence together. On the held-out tests the evidence did not change model accuracy: its value is an auditable record of the facts. Unconfirmed or missing required evidence is flagged on every answer.",
    needle: "Optional. Needle 3, a 121M-parameter model by Cactus Compute, runs on this computer and fills the fields you declare from the scenario text. Each value is then checked in code: written in the scenario, not found, or inferred (yes/no fields). The model can omit or misread values, so they are unverified; when you add them to the decision they enter the scenario as model-extracted data and every answer is flagged. No network: only the native engine is used.",
    integrity: "SHA-512 and MD5 are recomputed from the immutable record: ID, workspace, title, creation time, request and response. JSON uses sorted keys, compact separators, UTF-8 and finite numbers. Digests are compared with the stored baseline. MD5 is a compatibility checksum with known collision weaknesses. These checks are not signatures or proof of factual correctness; someone able to change both record and baseline can bypass them."
  };
  var helpAnchor = null;
  var helpPinned = false;
  var helpCloseTimer;
  function closeHelp() {
    clearTimeout(helpCloseTimer);
    if (helpAnchor) {
      helpAnchor.setAttribute("aria-expanded", "false");
      helpAnchor.removeAttribute("aria-describedby");
    }
    helpAnchor = null;
    helpPinned = false;
    $("help-popup").hidden = true;
  }
  function positionHelp() {
    if (!helpAnchor) return;
    const popup = $("help-popup"), r = helpAnchor.getBoundingClientRect();
    popup.style.left = `${Math.max(8, Math.min(r.left, innerWidth - popup.offsetWidth - 8))}px`;
    popup.style.top = `${Math.max(8, r.bottom + 8 + popup.offsetHeight < innerHeight ? r.bottom + 8 : r.top - popup.offsetHeight - 8)}px`;
  }
  function openHelp(button, pin = false) {
    clearTimeout(helpCloseTimer);
    if (helpAnchor !== button) closeHelp();
    helpAnchor = button;
    helpPinned = helpPinned || pin;
    const popup = $("help-popup");
    popup.textContent = button.dataset.helpText || helpTexts[button.dataset.help || ""] || "";
    popup.hidden = false;
    button.setAttribute("aria-expanded", "true");
    button.setAttribute("aria-describedby", "help-popup");
    positionHelp();
  }
  function laterCloseHelp() {
    if (!helpPinned) helpCloseTimer = window.setTimeout(closeHelp, 150);
  }
  var helpTarget = (event) => event.target instanceof Element ? event.target.closest(".help-icon") : null;
  function installHelp() {
    document.addEventListener("mouseover", (e) => {
      const button = helpTarget(e);
      if (button && (!helpPinned || button === helpAnchor)) openHelp(button);
    });
    document.addEventListener("mouseout", (e) => {
      if (helpTarget(e)) laterCloseHelp();
    });
    document.addEventListener("focusin", (e) => {
      if (e.target instanceof HTMLElement && e.target.matches(".help-icon")) openHelp(e.target);
    });
    document.addEventListener("focusout", (e) => {
      if (e.target instanceof HTMLElement && e.target.matches(".help-icon")) laterCloseHelp();
    });
    document.addEventListener("click", (e) => {
      const button = helpTarget(e);
      if (button) {
        e.preventDefault();
        e.stopPropagation();
        if (helpAnchor === button && helpPinned) closeHelp();
        else openHelp(button, true);
      } else if (!(e.target instanceof Element && e.target.closest("#help-popup"))) closeHelp();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") closeHelp();
    });
    $("help-popup").addEventListener("mouseenter", () => clearTimeout(helpCloseTimer));
    $("help-popup").addEventListener("mouseleave", laterCloseHelp);
    window.addEventListener("resize", positionHelp);
    window.addEventListener("scroll", positionHelp, true);
  }
  function helpGlyph() {
    return '<svg aria-hidden="true" viewBox="0 0 20 20"><circle cx="10" cy="10" r="8.5" fill="none" stroke="currentColor" stroke-width="1.3"/><path d="M10 5.5v5.7" stroke="currentColor" stroke-width="1.7" stroke-linecap="round"/><circle cx="10" cy="14.4" r="1" fill="currentColor"/></svg>';
  }
  function helpIcon(text, label = "Explain this indicator") {
    return `<button type="button" class="help-icon" aria-label="${esc(label)}" aria-expanded="false" data-help-text="${esc(text)}">${helpGlyph()}</button>`;
  }

  // src/questions.ts
  var KINDS = [
    ["noul", "Yes / no", "A Yes / no question returns the probability that a condition holds."],
    ["choice", "Choice", "A Choice question picks one option from a set you define and returns a probability for each."],
    ["score", "Score", "A Score question rates the case against ordered levels and returns a score, a distribution and confidence."]
  ];
  var OUTPUT = { noul: "boolean", choice: "choice", score: "score" };
  var KIND = { boolean: "noul", noul: "noul", choice: "choice", score: "score" };
  var PLACEHOLDER = {
    noul: "Is this action safe to run without a human approving it first?",
    choice: "Which team should handle this message?",
    score: "How ready is this lead to buy?"
  };
  function addOption(card, description = "") {
    const list = card.querySelector(".options-list");
    if (list.children.length >= 255) {
      toast("Limit: 255 options");
      return;
    }
    const row = document.createElement("div");
    row.className = "option-row";
    row.innerHTML = `<span class="level-index opt-index"></span><input class="opt-desc" maxlength="3000" placeholder="Choose when…" value="${esc(description)}" aria-label="Choose when"><button type="button" class="remove opt-remove" aria-label="Remove option">×</button>`;
    row.querySelector(".opt-remove").addEventListener("click", () => {
      row.remove();
      refreshOptions(card);
    });
    row.querySelector(".opt-desc").addEventListener("input", () => refreshOptions(card));
    list.append(row);
    refreshOptions(card);
  }
  function refreshOptions(card, selected2) {
    card.querySelectorAll(".opt-index").forEach((n, i) => {
      n.textContent = String(i);
    });
    const select = card.querySelector(".abstain");
    const current2 = selected2 ?? select.value;
    const rows2 = [...card.querySelectorAll(".opt-desc")].map((input, i) => [String(i), input.value.trim()]);
    select.innerHTML = '<option value="">None</option>' + rows2.map(([k, d]) => `<option value="${k}"${k === current2 ? " selected" : ""}>${k}${d ? ` · ${esc(d.slice(0, 40))}` : ""}</option>`).join("");
  }
  function addLevel(card, text = "") {
    const list = card.querySelector(".levels-list");
    if (list.children.length >= 32) {
      toast("Limit: 32 levels");
      return;
    }
    const row = document.createElement("div");
    row.className = "level-row";
    row.innerHTML = `<span class="level-index"></span><input class="level-text" maxlength="3000" placeholder="Describe this level" value="${esc(text)}" aria-label="Level"><button type="button" class="remove level-remove" aria-label="Remove level">×</button>`;
    row.querySelector(".level-remove").addEventListener("click", () => {
      row.remove();
      refreshLevels(card);
    });
    row.querySelector(".level-text").addEventListener("input", () => refreshLevels(card));
    list.append(row);
    refreshLevels(card);
  }
  function levelTexts(card) {
    return [...card.querySelectorAll(".level-text")].map((i) => i.value.trim());
  }
  function refreshLevels(card) {
    card.querySelectorAll(".levels-list .level-index").forEach((n, i) => {
      n.textContent = String(i);
    });
  }
  function setKind(card, kind) {
    card.kind = kind;
    card.querySelectorAll(".type-tab").forEach((b) => {
      const on = b.dataset.kind === kind;
      b.classList.toggle("active", on);
      b.setAttribute("aria-selected", String(on));
    });
    card.querySelector(".type-hint").textContent = KINDS.find((k) => k[0] === kind)[2];
    card.querySelector(".instructions").placeholder = PLACEHOLDER[kind];
    card.querySelectorAll(".for-noul, .for-choice, .for-score").forEach((n) => n.classList.toggle("hidden", !n.classList.contains(`for-${kind}`)));
    if (kind === "choice" && !card.querySelector(".option-row")) {
      addOption(card);
      addOption(card);
    }
    if (kind === "score" && !card.querySelector(".level-row")) {
      addLevel(card);
      addLevel(card);
      addLevel(card);
    }
  }
  function addQuestion(type = "noul", d) {
    if (document.querySelectorAll(".question").length >= 32) {
      toast("Limit: 32 decisions");
      return void 0;
    }
    const card = document.createElement("div");
    card.className = "question decision-card";
    if (d?.name) card.name = d.name;
    card.innerHTML = `<div class="decision-head"><span class="decision-number"></span><div class="type-tabs" role="tablist" aria-label="Output type">` + KINDS.map(([k, label]) => `<button type="button" class="type-tab" role="tab" data-kind="${k}">${label}</button>`).join("") + `</div>${helpIcon(helpTexts.types, "Explain the output types")}<button type="button" class="remove decision-remove" aria-label="Remove decision">×</button></div><p class="type-hint micro"></p><label class="field-caption">Question<textarea class="instructions" rows="2" maxlength="6000">${esc(d?.instructions || "")}</textarea></label><div class="for-choice"><span class="field-caption">Options</span><div class="options-list"></div><div class="row-actions"><button type="button" class="text-button add-option">+ Add option</button><label class="abstain-line">"Not enough information" option <select class="abstain" aria-label="Abstention option"><option value="">None</option></select></label></div></div><div class="for-score"><span class="field-caption">Levels, lowest to highest</span><div class="levels-list"></div><div class="row-actions"><button type="button" class="text-button add-level">+ Add level</button></div></div>`;
    card.querySelectorAll(".type-tab").forEach((b) => b.addEventListener("click", () => setKind(card, b.dataset.kind)));
    card.querySelector(".decision-remove").addEventListener("click", () => {
      card.remove();
      renumber();
    });
    card.querySelector(".add-option").addEventListener("click", () => addOption(card));
    card.querySelector(".add-level").addEventListener("click", () => addLevel(card));
    $("questions").append(card);
    const kind = KIND[d?.output || type] || "noul";
    if (kind === "choice" && d) {
      const options = d.alternatives || (d.criteria && !Array.isArray(d.criteria) ? d.criteria : Object.fromEntries((d.criteria || []).map((k) => [k, ""])));
      const keys = Object.keys(options);
      keys.forEach((key) => {
        const text = options[key] || "";
        const named = !/^\d+$/.test(key) && key.length > 1 && !text.toLowerCase().includes(key.toLowerCase());
        addOption(card, text ? named ? `${key[0].toUpperCase()}${key.slice(1)} — ${text}` : text : key);
      });
      const abstain = d.abstain_option ? keys.indexOf(d.abstain_option) : -1;
      refreshOptions(card, abstain >= 0 ? String(abstain) : "");
    }
    if (kind === "score" && d) {
      const raw = d.levels || d.criteria || [];
      const levels = Array.isArray(raw) ? raw : Object.keys(raw).sort((a, b) => Number(a) - Number(b)).map((k) => raw[k]);
      levels.forEach((text) => addLevel(card, String(text)));
    }
    setKind(card, kind);
    renumber();
    return card;
  }
  function renumber() {
    const numbers = document.querySelectorAll(".decision-card .decision-number");
    numbers.forEach((n, i) => {
      n.textContent = String(i + 1);
    });
    const hint = document.getElementById("decisions-hint");
    if (hint) hint.textContent = `${numbers.length} decision${numbers.length === 1 ? "" : "s"} · Yes / no, Choice or Score`;
  }
  function loadDecisions(decisions) {
    $("questions").replaceChildren();
    for (const d of decisions) addQuestion(KIND[d.output], d);
    if (!decisions.length) addQuestion();
  }
  function loadQuestions(questions) {
    loadDecisions(Object.values(questions).map((q) => ({
      output: OUTPUT[q.type],
      instructions: q.instructions,
      criteria: q.criteria,
      abstain_option: q.abstain_option
    })));
  }
  function getDecisions() {
    const out = [];
    document.querySelectorAll(".decision-card").forEach((card, i) => {
      const label = `Decision ${i + 1}`;
      const instructions = card.querySelector(".instructions").value.trim();
      if (!instructions) throw Error(`${label}: write the question`);
      const kind = card.kind;
      const d = { output: OUTPUT[kind], instructions };
      if (card.name) d.name = card.name;
      if (kind === "choice") {
        const criteria = /* @__PURE__ */ Object.create(null);
        card.querySelectorAll(".option-row").forEach((row, n) => {
          const key = String(n);
          const description = row.querySelector(".opt-desc").value.trim();
          if (!description) throw Error(`${label}: describe option ${n} or remove it`);
          criteria[key] = description;
        });
        if (Object.keys(criteria).length < 2) throw Error(`${label}: add at least two options`);
        d.criteria = criteria;
        const abstain = card.querySelector(".abstain").value;
        if (abstain && Object.hasOwn(criteria, abstain)) d.abstain_option = abstain;
      } else if (kind === "score") {
        const levels = levelTexts(card).filter(Boolean);
        if (levels.length < 2) throw Error(`${label}: add at least two levels`);
        if (new Set(levels).size !== levels.length) throw Error(`${label}: levels must be different`);
        d.criteria = levels;
      }
      out.push(d);
    });
    if (!out.length) throw Error("Add a decision");
    return out;
  }

  // src/tags.ts
  var suggested = [];
  var used = [];
  var chosen = [];
  var highlight = -1;
  var maxTags = 8;
  var maxChars = 32;
  var normalizeTag = (value) => value.split(/\s+/).filter(Boolean).join(" ").toLowerCase();
  var suggestedTags = () => suggested;
  var getTags = () => [...chosen];
  function setUsedTags(tags) {
    used = tags;
  }
  function setTags(tags = []) {
    chosen = [];
    for (const t of tags) {
      const tag = normalizeTag(t);
      if (tag && !chosen.includes(tag)) chosen.push(tag);
    }
    renderChips();
  }
  function renderChips() {
    $("tag-chips").innerHTML = chosen.map((t, i) => `<span class="tag-chip">${esc(t)}<button type="button" class="tag-remove" data-index="${i}" aria-label="Remove tag ${esc(t)}">×</button></span>`).join("");
    $("tag-chips").querySelectorAll(".tag-remove").forEach((b) => b.addEventListener("click", () => {
      chosen.splice(Number(b.dataset.index), 1);
      renderChips();
      $input("tag-entry").focus();
    }));
    $input("tag-entry").placeholder = chosen.length >= maxTags ? "Tag limit reached" : chosen.length ? "Add another tag…" : "Type a tag…";
  }
  function matches(text) {
    const q = normalizeTag(text);
    const pool = [.../* @__PURE__ */ new Set([...suggested, ...used])].filter((t) => !chosen.includes(t));
    if (!q) return pool.slice(0, 12);
    const starts = pool.filter((t) => t.startsWith(q)), contains = pool.filter((t) => !t.startsWith(q) && t.includes(q));
    return [...starts, ...contains].slice(0, 12);
  }
  function closeList() {
    highlight = -1;
    $("tag-suggestions").classList.add("hidden");
    $input("tag-entry").setAttribute("aria-expanded", "false");
    $input("tag-entry").removeAttribute("aria-activedescendant");
  }
  function renderList() {
    const entry = $input("tag-entry"), list = $("tag-suggestions");
    const typed = normalizeTag(entry.value);
    const items = matches(entry.value);
    const own = typed && !items.includes(typed) && !chosen.includes(typed);
    if (!items.length && !own) {
      closeList();
      return;
    }
    if (highlight >= items.length + (own ? 1 : 0)) highlight = -1;
    list.innerHTML = items.map((t, i) => `<li id="tag-option-${i}" role="option" class="tag-option${i === highlight ? " active" : ""}" data-tag="${esc(t)}" aria-selected="${i === highlight}">${esc(t)}${used.includes(t) && !suggested.includes(t) ? "<small>used</small>" : ""}</li>`).join("") + (own ? `<li id="tag-option-${items.length}" role="option" class="tag-option own${highlight === items.length ? " active" : ""}" data-tag="${esc(typed)}" aria-selected="${highlight === items.length}">Add “${esc(typed)}”<small>your tag</small></li>` : "");
    list.classList.remove("hidden");
    entry.setAttribute("aria-expanded", "true");
    if (highlight >= 0) entry.setAttribute("aria-activedescendant", `tag-option-${highlight}`);
    else entry.removeAttribute("aria-activedescendant");
    list.querySelectorAll(".tag-option").forEach((li) => li.addEventListener("mousedown", (e) => {
      e.preventDefault();
      addTag(li.dataset.tag || "");
    }));
  }
  function addTag(value) {
    const tag = normalizeTag(value);
    const entry = $input("tag-entry");
    if (!tag) return false;
    if (chosen.includes(tag)) {
      entry.value = "";
      renderList();
      return false;
    }
    if (chosen.length >= maxTags) {
      toast(`Up to ${maxTags} tags`);
      return false;
    }
    if (tag.length > maxChars || /[<>"\\]/.test(tag)) {
      toast(`A tag has 1-${maxChars} characters, without < > " or \\`);
      return false;
    }
    chosen.push(tag);
    entry.value = "";
    highlight = -1;
    renderChips();
    renderList();
    return true;
  }
  async function installTags() {
    const entry = $input("tag-entry");
    entry.addEventListener("focus", renderList);
    entry.addEventListener("input", () => {
      if (entry.value.includes(",")) {
        entry.value.split(",").slice(0, -1).forEach(addTag);
        entry.value = entry.value.split(",").pop() || "";
      }
      highlight = -1;
      renderList();
    });
    entry.addEventListener("blur", () => window.setTimeout(closeList, 100));
    entry.addEventListener("keydown", (e) => {
      const options = [...$("tag-suggestions").querySelectorAll(".tag-option")];
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        if (!options.length) {
          renderList();
          return;
        }
        highlight = e.key === "ArrowDown" ? (highlight + 1) % options.length : highlight <= 0 ? options.length - 1 : highlight - 1;
        renderList();
      } else if (e.key === "Enter" || e.key === "Tab" && entry.value.trim()) {
        const pick = highlight >= 0 ? options[highlight]?.dataset.tag : entry.value;
        if (pick && pick.trim()) {
          e.preventDefault();
          addTag(pick);
        } else if (e.key === "Enter") e.preventDefault();
      } else if (e.key === "Backspace" && !entry.value && chosen.length) {
        chosen.pop();
        renderChips();
        renderList();
      } else if (e.key === "Escape") {
        if (!$("tag-suggestions").classList.contains("hidden")) {
          e.stopPropagation();
          closeList();
        }
      }
    });
    $("tag-input").addEventListener("click", (e) => {
      if (e.target === $("tag-input") || e.target === $("tag-chips")) entry.focus();
    });
    renderChips();
    try {
      const info = await api("/api/tags");
      suggested = info.suggested;
      maxTags = info.max_tags;
      maxChars = info.max_chars;
    } catch {
      suggested = [];
    }
  }

  // src/casebuilder.ts
  var TYPES = ["boolean", "number", "integer", "date", "text", "enum"];
  var NAME = /^[a-z][a-z0-9_]{0,63}$/;
  var CHECK_LABELS = {
    in_text: "written in the case",
    not_in_text: "not found in the case",
    inferred: "inferred",
    not_stated: "not stated"
  };
  var templates = [];
  var currentTemplate;
  function valueControl(type, options, value) {
    if (type === "boolean") return `<select class="ev-value" aria-label="Value"><option value="">—</option><option value="true"${value === true ? " selected" : ""}>true</option><option value="false"${value === false ? " selected" : ""}>false</option></select>`;
    if (type === "enum") return `<select class="ev-value" aria-label="Value"><option value="">—</option>${options.map((o) => `<option${o === value ? " selected" : ""}>${esc(o)}</option>`).join("")}</select>`;
    const kind = type === "date" ? "date" : type === "text" ? "text" : "number";
    const step = type === "integer" ? ' step="1"' : type === "number" ? ' step="any"' : "";
    return `<input class="ev-value" aria-label="Value" type="${kind}"${step} value="${value === null || value === void 0 ? "" : esc(value)}">`;
  }
  function badge(row) {
    const e = row.entry;
    if (!e || e.status === "confirmed") return `<span class="ev-status confirmed">${e?.source === "template" ? "template" : "confirmed"}</span>`;
    const check = e.check ? ` · ${esc(CHECK_LABELS[e.check] || e.check)}` : "";
    return `<span class="ev-status unverified check-${esc(e.check || "")}">extracted${check}</span><button type="button" class="text-button ev-keep" title="Keep this value">✓ Keep</button>`;
  }
  function renderValueCell(row, value) {
    const type = row.querySelector(".ev-type").value;
    const options = row.querySelector(".ev-options").value.split(",").map((v) => v.trim()).filter(Boolean);
    row.querySelector(".ev-value-cell").innerHTML = valueControl(type, options, value);
    row.querySelector(".ev-options").classList.toggle("hidden", type !== "enum");
    row.querySelector(".ev-value")?.addEventListener("change", () => confirmRow(row, "user"));
  }
  function renderStatus(row) {
    const cell = row.querySelector(".ev-status-cell");
    cell.innerHTML = badge(row);
    cell.querySelector(".ev-keep")?.addEventListener("click", () => confirmRow(row));
  }
  function confirmRow(row, source) {
    row.entry = { source: source || row.entry?.source || "user", status: "confirmed" };
    renderStatus(row);
  }
  function addEvidence(spec = {}, value = null, entry) {
    const row = document.createElement("tr");
    row.className = "evidence-row";
    row.entry = entry || { source: "user", status: "confirmed" };
    const type = spec.type || "text";
    row.innerHTML = `<td><input class="ev-name" aria-label="Field name" maxlength="64" spellcheck="false" placeholder="field_name" value="${esc(spec.name || "")}" title="${esc(spec.description || "")}"><small class="ev-description">${esc(spec.description || "")}${spec.unit ? ` (${esc(spec.unit)})` : ""}</small></td><td><select class="ev-type" aria-label="Type">${TYPES.map((t) => `<option${t === type ? " selected" : ""}>${t}</option>`).join("")}</select><input class="ev-options${type === "enum" ? "" : " hidden"}" aria-label="Options" placeholder="low, medium, high" value="${esc((spec.options || []).join(", "))}"></td><td class="ev-value-cell"></td><td class="ev-status-cell"></td><td><button type="button" class="remove ev-remove" aria-label="Remove field">×</button></td>`;
    row.dataset.description = spec.description || "";
    if (spec.unit) row.dataset.unit = spec.unit;
    if (spec.required) row.dataset.required = "1";
    $("evidence-body").append(row);
    renderValueCell(row, value);
    renderStatus(row);
    row.querySelector(".ev-type").addEventListener("change", () => renderValueCell(row, null));
    row.querySelector(".ev-options").addEventListener("change", () => renderValueCell(row, readValue(row)));
    row.querySelector(".ev-remove").addEventListener("click", () => row.remove());
    return row;
  }
  function readValue(row) {
    const type = row.querySelector(".ev-type").value;
    const raw = row.querySelector(".ev-value").value.trim();
    if (raw === "") return null;
    if (type === "boolean") return raw === "true";
    if (type === "number" || type === "integer") {
      const n = Number(raw);
      if (!Number.isFinite(n) || type === "integer" && !Number.isInteger(n)) throw Error(`Evidence ${row.querySelector(".ev-name").value}: expected ${type}`);
      return n;
    }
    return raw;
  }
  function evidenceRows() {
    return [...document.querySelectorAll(".evidence-row")];
  }
  function getEvidence() {
    const schema = [], evidence = {};
    for (const row of evidenceRows()) {
      const name = row.querySelector(".ev-name").value.trim();
      if (!name && readValue(row) === null) continue;
      if (!NAME.test(name)) throw Error(`Evidence name "${name}": lowercase letters, digits and _, starting with a letter`);
      if (schema.some((s) => s.name === name)) throw Error(`Duplicate evidence field: ${name}`);
      const type = row.querySelector(".ev-type").value;
      const spec = { name, type };
      if (row.dataset.description) spec.description = row.dataset.description;
      if (row.dataset.unit) spec.unit = row.dataset.unit;
      if (row.dataset.required) spec.required = true;
      if (type === "enum") {
        spec.options = row.querySelector(".ev-options").value.split(",").map((v) => v.trim()).filter(Boolean);
        if (spec.options.length < 2) throw Error(`Evidence ${name}: an enum needs at least two options`);
      }
      schema.push(spec);
      const value = readValue(row);
      if (value !== null) evidence[name] = { value, source: row.entry?.source || "user", status: row.entry?.status || "confirmed", ...row.entry?.check ? { check: row.entry.check } : {} };
    }
    return { schema, evidence };
  }
  function selectedTemplate() {
    return templates.find((t) => t.id === $select("template-select").value);
  }
  function applyTemplate(t, withValues = true) {
    currentTemplate = t.id;
    $area("case-text").value = withValues ? t.case || "" : "";
    $input("case-title").value = "";
    setTags(t.tags || []);
    $("evidence-body").replaceChildren();
    for (const spec of t.evidence_schema || []) {
      const value = withValues ? t.evidence?.[spec.name] ?? null : null;
      addEvidence(spec, value, { source: value === null ? "user" : "template", status: "confirmed" });
    }
    loadDecisions(t.decisions);
    $("evidence-section").open = (t.evidence_schema || []).length > 0;
    $input("permutation").checked = t.permutation_check !== false;
    $("extract-status").textContent = "";
  }
  function blankCase() {
    currentTemplate = void 0;
    $area("case-text").value = "";
    $input("case-title").value = "";
    setTags([]);
    $("evidence-body").replaceChildren();
    $("evidence-section").open = false;
    $("questions").replaceChildren();
    addQuestion();
  }
  async function loadTemplates(selectId) {
    const data = await api("/api/templates");
    templates = data.templates;
    const group = (origin, label) => {
      const rows2 = templates.filter((t) => t.origin === origin);
      return rows2.length ? `<optgroup label="${label}">${rows2.map((t) => `<option value="${esc(t.id)}">${esc(t.title)}</option>`).join("")}</optgroup>` : "";
    };
    $select("template-select").innerHTML = '<option value="">Blank case</option>' + group("builtin", "Built-in") + group("project", "Project templates") + group("local", "My templates");
    if (selectId) $select("template-select").value = selectId;
    if (data.errors.length) toast(`${data.errors.length} template file(s) could not be read: ${data.errors[0].path}`);
  }
  async function saveTemplate() {
    const id = $input("template-id").value.trim();
    const title = $input("template-title").value.trim() || id;
    const withValues = $input("template-values").checked;
    const { schema, evidence } = getEvidence();
    const template = {
      id,
      title,
      description: $input("template-description").value.trim(),
      decisions: getDecisions(),
      evidence_schema: schema,
      permutation_check: $input("permutation").checked,
      ...getTags().length ? { tags: getTags() } : {},
      ...withValues && $area("case-text").value.trim() ? { case: $area("case-text").value } : {}
    };
    if (withValues) {
      template.example = true;
      template.evidence = Object.fromEntries(Object.entries(evidence).map(([k, v]) => [k, v.value]));
    }
    const overwrite = $input("template-overwrite").checked;
    const result = await api("/api/templates", { template, overwrite });
    await loadTemplates(result.id);
    currentTemplate = result.id;
    $("template-save-status").textContent = `Saved to ${result.saved.join(" and ")}`;
    toast("Template saved");
  }
  async function extractEvidence() {
    const button = $button("extract-evidence");
    const text = $area("case-text").value.trim();
    try {
      if (!text) throw Error("Describe the case first");
      const { schema } = getEvidence();
      if (!schema.length) throw Error("Declare the evidence fields first (a template defines them)");
      button.disabled = true;
      $("extract-status").innerHTML = '<span class="spinner"></span>Reading the case on this computer…';
      const result = await api("/api/extract-evidence", { text, schema, backend: $select("extract-backend").value });
      let filled = 0;
      for (const row of evidenceRows()) {
        const name = row.querySelector(".ev-name").value.trim();
        const found = result.fields.find((f) => f.name === name);
        if (!found) continue;
        if (found.value === null) continue;
        renderValueCell(row, found.value);
        row.entry = { source: "extracted", status: "unverified", check: found.check };
        renderStatus(row);
        filled++;
      }
      $("extract-status").textContent = `${filled} of ${result.fields.length} fields suggested by ${result.model || result.backend} in ${result.seconds.toFixed(1)} s. Check each value and keep it (✓) or correct it.`;
    } catch (e) {
      $("extract-status").innerHTML = `<span class="error-text">${esc(errorMessage(e))}</span>`;
    } finally {
      button.disabled = false;
    }
  }
  function keepAll() {
    for (const row of evidenceRows()) if (row.entry?.status === "unverified") confirmRow(row);
  }
  function buildCaseRequest() {
    const inBlock = (block, read) => {
      try {
        return read();
      } catch (e) {
        $(block).open = true;
        throw e;
      }
    };
    const text = inBlock("case-section", () => {
      const t = $area("case-text").value;
      if (!t.trim()) throw Error("Describe the case");
      return t;
    });
    const { schema, evidence } = inBlock("evidence-section", getEvidence);
    const decisions = inBlock("decisions-section", getDecisions);
    const title = $input("case-title").value.trim().split(/\s+/).join(" ");
    const tags = getTags();
    return {
      ...currentTemplate ? { template: currentTemplate } : {},
      space: space(),
      ...title ? { title } : {},
      ...tags.length ? { tags } : {},
      case: text,
      evidence_schema: schema,
      evidence,
      decisions,
      permutation_check: $input("permutation").checked
    };
  }
  function installCaseBuilder() {
    $select("template-select").addEventListener("change", () => {
      const row = selectedTemplate();
      if (row) {
        applyTemplate(row.template, $input("template-load-values").checked);
        $("template-description-line").textContent = row.description || "";
      } else {
        blankCase();
        $("template-description-line").textContent = "Start from an empty case.";
      }
    });
    installTags().catch(reportError);
    $("add-evidence").addEventListener("click", () => addEvidence());
    $("evidence-section").addEventListener("toggle", () => {
      if ($("evidence-section").open && !document.querySelector(".evidence-row")) addEvidence();
    });
    $("extract-evidence").addEventListener("click", () => {
      extractEvidence();
    });
    $("keep-all-evidence").addEventListener("click", keepAll);
    $("save-template").addEventListener("click", () => saveTemplate().catch((e) => {
      $("template-save-status").textContent = errorMessage(e);
      reportError(e);
    }));
    loadTemplates().then(() => {
      if (!document.querySelector(".decision-card")) blankCase();
    }).catch(reportError);
  }

  // src/status.ts
  var MODE_LABELS = { auto: "Automatic", cpu: "CPU only" };
  var profile = null;
  var statusTimer;
  var engineReady = false;
  var evaluating = false;
  var ENGINE_BADGES = { not_loaded: "Preparing engine…", loading: "Loading engine…", ready: "Local engine ready", error: "Engine unavailable" };
  var isEngineReady = () => engineReady;
  var engineProfile = () => profile;
  function setEvaluating(value) {
    evaluating = value;
  }
  async function refreshStatus() {
    clearTimeout(statusTimer);
    const data = await api("/api/status"), status = data.engine.status;
    engineReady = status === "ready";
    const badge2 = $("engine-badge");
    const mode = data.mode && MODE_LABELS[data.mode];
    badge2.textContent = (ENGINE_BADGES[status] || status) + (mode ? ` · ${mode}` : "");
    badge2.classList.toggle("ready", status === "ready");
    $("retry-engine").classList.toggle("hidden", status !== "error");
    $("startup-status").classList.toggle("hidden", engineReady);
    $("startup-status").textContent = status === "error" ? `Could not load the engine: ${data.engine.error || "local failure"}. Open Engine & privacy to retry.` : "Loading the local models automatically. You can enter your scenario while you wait.";
    const evaluate = $button("evaluate");
    evaluate.disabled = !engineReady || evaluating;
    evaluate.textContent = engineReady ? "Evaluate decision ↗" : status === "error" ? "Engine unavailable" : "Loading engine…";
    const total = data.stats.decisions, limit = data.library_limit ?? 200;
    $("library-count").textContent = String(total);
    $("library-count").title = `${total} of ${limit} decisions in the library`;
    const room = `${total} of ${limit} decisions in the library` + (total >= limit ? " · full: evaluating offers to archive them" : total >= limit - 20 ? ` · ${limit - total} left before the archive` : "");
    $("library-room").textContent = room;
    $("library-capacity").textContent = `The library keeps up to ${limit} decisions (all workspaces): ${total} so far. At the limit, every decision is compressed into the project data folder and the library starts empty.`;
    renderEngineProfile(data.engine);
    $("engine-details").textContent = JSON.stringify(data.engine, null, 2);
    profile = data.engine.profile || null;
    $("apply-profile").classList.toggle("hidden", !profile);
    if (status === "loading" || status === "not_loaded") statusTimer = window.setTimeout(() => refreshStatus().catch(reportError), 1e3);
  }

  // src/needle.ts
  var CHECK_LABELS2 = {
    in_text: "Written in the scenario",
    not_in_text: "Not found in the scenario - check it",
    inferred: "Inferred - check it",
    not_stated: "Not stated"
  };
  async function loadNeedle() {
    let status = null;
    try {
      status = await api("/api/needle");
    } catch {
      status = null;
    }
    const info = document.getElementById("needle-settings");
    if (info) info.textContent = !status ? "Status unavailable." : status.available ? `Installed (models/needle, hashes verified on first use). Used for short texts, up to 1,500 characters. Network: none - TypeLogic uses only the native Needle engine; the needle Python package and its telemetry are not used.` : `${status.reason} Network: none.`;
    return status;
  }

  // src/render.ts
  var current = null;
  var currentDecisionId = () => current?.decision_id;
  var reasons = {
    below_threshold: "Below threshold",
    abstention_not_declared: "Abstention not declared",
    explicit_abstention: "The model selected abstention",
    option_order_sensitive: "Sensitive to option order",
    order_check_disabled: "Order check disabled",
    specialist_order_robustness_not_verified: "Order not audited in the specialist",
    model_extracted_fields: "Model-extracted fields (unverified)",
    unconfirmed_evidence: "Extracted evidence not confirmed",
    missing_required_evidence: "Required evidence missing"
  };
  function orderedAnswers(answers, request) {
    const order = request?.case_request?.decision_order || [];
    const rank = (id) => {
      const i = order.indexOf(id);
      const n = /^(?:question|decision)_(\d+)$/.exec(id);
      return i >= 0 ? i : n ? Number(n[1]) : 1e6;
    };
    return Object.entries(answers).sort((a, b) => rank(a[0]) - rank(b[0]));
  }
  function questionTitle(id, a, questions, index) {
    const number = /^(?:question|decision)_(\d+)$/.exec(id)?.[1] || index + 1;
    return `${number}. ${a.instructions || questions?.[id]?.instructions || id}`;
  }
  function choiceCriteriaOf(id, a, questions) {
    const own = a.criteria && typeof a.criteria === "object" && !Array.isArray(a.criteria) ? a.criteria : void 0;
    const fallback = questions?.[id]?.criteria;
    return own || (fallback && !Array.isArray(fallback) ? fallback : void 0) || {};
  }
  function resultHTML(id, a, questions = {}, index = 0) {
    const entries = Object.entries(a.probabilities);
    const best = Math.max(...entries.map(([, v]) => v));
    const winner = entries.find(([, p]) => p === best)[0];
    const binaryLabel = winner === "true" ? "Yes" : "No";
    const value = a.type === "choice" ? a.choice : a.type === "noul" ? binaryLabel : a.legend?.[winner] || winner;
    const warnings = a.review_reasons.filter((r) => r !== "human_review_required" && r !== "below_threshold");
    const choiceCriteria = choiceCriteriaOf(id, a, questions);
    const optionLabel = (key) => a.type === "choice" ? choiceCriteria[key] ? `${key} — ${choiceCriteria[key]}` : key : a.legend?.[key] || { false: "No", true: "Yes" }[key] || key;
    const chanceLabel = a.type === "noul" ? binaryLabel : a.type === "choice" ? optionLabel(a.choice) : a.legend?.[winner] || winner;
    const chanceHelp = `Confidence is the model's probability for the most likely alternative (as in Jev), not a measured accuracy rate.
The decision head transforms logits into weights: pᵢ = exp(logitᵢ) / Σ exp(logitⱼ), with temperature applied by the engine. The contract normalizes the distribution.
Presented distribution: ${entries.map(([k, p]) => `${k}: ${p.toFixed(6)}`).join("; ")}.
Confidence = 100 × ${best.toFixed(6)} = ${(best * 100).toFixed(1)}%, rounded to one decimal. Largest weight: ${chanceLabel}. This does not disclose internal reasoning or verify the facts.`;
    const n = entries.length;
    const concentrationHelp = `Concentration measures how far the largest probability is above an even distribution, not the probability that the answer is correct.
1. Alternatives: n = ${n}; uniform = 1/n = ${(1 / n).toFixed(6)}.
2. Largest probability = ${best.toFixed(6)}.
3. C = (max(p) − 1/n) / (1 − 1/n).
4. C = (${best.toFixed(6)} − ${(1 / n).toFixed(6)}) / ${(1 - 1 / n).toFixed(6)} = ${a.concentration.toFixed(6)}; 100 × C = ${(a.concentration * 100).toFixed(1)}%.
0% is uniform; 100% puts all weight on one alternative. Accuracy validation requires independent labeled cases.`;
    const score = a.score ?? 0;
    const scoreHelp = a.type === "score" ? `Ordinal score: levels use indices starting at zero. Expected index = Σ index × probability.
${entries.map(([k, p]) => `${k} × ${p.toFixed(6)}`).join(" + ")} = ${score.toFixed(6)}.
The highlighted answer is the most likely level; the expected index can lie between levels. Indices are a convention, not a physical measurement.` : "";
    const bars = entries.map(([key, p]) => `<div class="probability ${p === best ? "top" : ""}"><span>${esc(optionLabel(key))}</span><div class="bar"><span style="width:${Math.min(100, p * 100)}%"></span></div><span>${(p * 100).toFixed(1)}%</span></div>`).join("");
    const numbered = a.type === "choice" && value !== void 0 && /^\d+$/.test(value) && !!choiceCriteria[value];
    const description = numbered ? `<p class="answer-description">Option ${esc(value)}</p>` : a.type === "choice" && value !== void 0 && choiceCriteria[value] ? `<p class="answer-description">${esc(choiceCriteria[value])}</p>` : "";
    const engineLabel = a.engine === "generalist" ? "Plumb-4B (generalist, direct option-letter readout)" : a.engine === "specialist" ? "Specialist (TypeLogic Qwen3.5-2B, trained decision head)" : a.engine === "clef" ? "Clef-Flash (9B decision model)" : a.engine === "ensemble" ? "Ensemble (two models, weighted product of probabilities)" : "";
    const engine = engineLabel ? `<p>Engine: ${esc(engineLabel)}</p>` : "";
    const order = a.order_diagnostic?.checked ? `<p>Order: ${a.order_diagnostic.unstable ? "sensitivity detected" : "no relevant change in two permutations"} ${helpIcon(helpTexts.order, "Explain order check")}</p>` : "";
    return `<article class="result-card"><h4 class="result-name">${esc(questionTitle(id, a, questions, index))}</h4><div class="result-value"><span class="answer-label">Answer:</span> ${esc(numbered ? choiceCriteria[value] : value)}</div>${description}${a.abstained ? '<span class="status-tag">Not enough information</span>' : ""}${bars}<div class="result-details"><p>Confidence: <strong>${(best * 100).toFixed(1)}% = ${esc(chanceLabel)}</strong> ${helpIcon(chanceHelp, "Explain confidence")}</p>${a.type === "score" ? `<p>Calculated score: ${score.toFixed(2)} / ${n - 1} ${helpIcon(scoreHelp, "Explain score")}</p>` : ""}<p>Concentration: ${(a.concentration * 100).toFixed(1)}% · no accuracy guarantee ${helpIcon(concentrationHelp, "Explain concentration")}</p>${engine}${order}${warnings.length ? `<p class="flags">${warnings.map((r) => esc(reasons[r] || r)).join(" · ")}</p>` : ""}</div></article>`;
  }
  function tagChips(tags, cls = "tag-list") {
    return tags?.length ? `<ul class="${cls}">${tags.map((t) => `<li class="tag-pill">${esc(t)}</li>`).join("")}</ul>` : "";
  }
  function identityHTML(record) {
    const integrity = record.integrity || {}, created = record.created, date = new Date(created);
    const formatted = Number.isNaN(date.getTime()) ? "Unknown date" : date.toLocaleString("en-GB", { dateStyle: "long", timeStyle: "medium" });
    const migrated = integrity.baseline_origin === "migrated_existing" ? '<p class="micro">Baseline established from an existing record during upgrade; its original content was not previously checksummed.</p>' : "";
    const generated = record.title_source && record.title_source !== "user" ? `<span class="title-source">${record.title_source.startsWith("model:") ? `Title written by ${esc(record.title_source.slice(6))}` : "Title taken from the case"}</span>` : "";
    const heading = record.title ? `<h3 class="decision-title">${esc(record.title)}${generated}</h3>${tagChips(record.tags)}` : "";
    return `<section class="decision-identity">${heading}<p class="decision-date">Created: <time datetime="${esc(created || "")}">${esc(formatted)}</time></p><p><strong>Decision ID:</strong> <code class="record-id">${esc(record.id || record.decision_id)}</code></p><p class="integrity-status ${integrity.verified ? "verified" : "failed"}">Integrity: ${integrity.verified ? "SHA-512 and MD5 match the stored baseline" : "verification failed"} ${helpIcon(helpTexts.integrity, "Explain record integrity")}</p><dl class="checksums"><dt>SHA-512</dt><dd>${esc(integrity.sha512 || "Unavailable")}</dd><dt>MD5</dt><dd>${esc(integrity.md5 || "Unavailable")}</dd></dl>${migrated}</section>`;
  }
  function exportGuide(history = false) {
    return `<div class="action-guide"><p><strong>Delete:</strong> removes the decision with its corrections and audit entries.</p>${history ? "<p><strong>Record correction:</strong> saves the observed alternative without retraining.</p>" : ""}<p><strong>Export Full JSON:</strong> downloads the stored decision with title, tags, case, evidence, technical answers, probabilities, unique ID and checked digests. <strong>Export Lite JSON:</strong> downloads only the title, tags, question text and typed answers; no case, probabilities, hashes or audit data. Single-decision downloads use stable eight-character filenames based on their ID and format.</p></div>`;
  }
  function renderResult(data) {
    current = data;
    $("machine-output").textContent = JSON.stringify(data, null, 2);
    $("latency").textContent = typeof data.latency_ms === "number" ? `${data.latency_ms.toLocaleString("en-US")} ms` : "";
    const results = $("results");
    results.classList.remove("empty");
    const fields = data.extracted_fields?.fields?.length ? `<details><summary>Fields extracted by Needle 3 (unverified)</summary>${data.extracted_fields.fields.map((f) => `<p class="result-details"><code>${esc(f.name)}</code>: <strong>${f.value === null ? "-" : esc(typeof f.value === "string" ? f.value : JSON.stringify(f.value))}</strong> · ${esc(CHECK_LABELS2[f.check] || f.check)}</p>`).join("")}</details>` : "";
    results.innerHTML = identityHTML(data) + orderedAnswers(data.answers, data.request).map(([id, a], i) => resultHTML(id, a, {}, i)).join("") + fields + '<div class="actions"><button id="dismiss-current" class="button danger ghost">Delete</button><button id="export-current" class="button secondary">Export Full JSON</button><button id="export-lite-current" class="button secondary">Export Lite JSON</button></div>' + exportGuide();
    $("dismiss-current").addEventListener("click", () => deleteCurrent());
    $("export-current").addEventListener("click", () => exportDecision(data.decision_id, "full"));
    $("export-lite-current").addEventListener("click", () => exportDecision(data.decision_id, "lite"));
  }
  function clearCurrentResults() {
    current = null;
    $("machine-output").textContent = "No completed evaluation.";
    $("results").classList.add("empty");
    $("results").textContent = "Decision deleted from the database.";
    $("latency").textContent = "";
  }
  async function deleteCurrent() {
    try {
      if (!current) return;
      await api("/api/delete", { id: current.decision_id });
      clearCurrentResults();
      toast("Decision and associated records deleted");
      await refreshStatus();
    } catch (e) {
      reportError(e);
    }
  }
  var ROLE_LABELS = { generalist: "Plumb (up to 16 options, titles, evidence)", specialist: "Specialist (catalogs)", clef: "Clef-Flash (catalogs)" };
  function renderEngineProfile(engine) {
    const core = engine.core;
    const models = engine.models || [];
    const catalog = models.some((m) => m.role === "clef") ? "Clef-Flash" : models.some((m) => m.role === "specialist") ? "specialist" : "";
    const limit = engine.routing?.max_generalist_options ?? 16;
    const facts = [
      ["Runtime", engine.runtime || "llama.cpp"],
      ...models.map((m) => [ROLE_LABELS[m.role] || m.role, m.file]),
      ["Execution", engine.device === "CPU" || !engine.device ? "Processor (CPU)" : `GPU ${engine.device}`],
      ["Why these models", engine.plan || "Not loaded yet"],
      ["Routing", models.some((m) => m.role === "generalist") ? `Up to ${limit} options: Plumb${catalog ? ` · more: ${catalog}` : ""}` : catalog ? `Every question: ${catalog}` : "No model loaded"],
      ["Output contract", "Choice · Yes/no · Score"],
      ["Deterministic core", core ? core.backend === "rust" ? `Rust ${core.core_version || ""}`.trim() : "Python reference" : "Rust"],
      ["Calibration", "Specialist and Plumb: local validation, not a general guarantee · Clef-Flash: not validated for your domain"],
      ["Network", "Local only · no external API"]
    ];
    $("engine-profile").innerHTML = facts.map(([label, value]) => `<div class="engine-fact"><span>${esc(label)}</span><strong>${esc(value)}</strong></div>`).join("");
  }

  // src/history.ts
  var PAGE_SIZE = 30;
  var UNTAGGED = "\0untagged";
  var rows = [];
  var filter = "all";
  var page = 1;
  var openId = null;
  var showAllTags = false;
  var selected = /* @__PURE__ */ new Set();
  var TAG_PREVIEW = 14;
  var tagsOf = (r) => Array.isArray(r.request.tags) ? r.request.tags : [];
  function setFilter(next) {
    if (next !== filter) {
      selected.clear();
      page = 1;
    }
    filter = next;
  }
  function showLibraryList() {
    openId = null;
    $("library-detail").classList.add("hidden");
    $("library-list").classList.remove("hidden");
  }
  var generation = 0;
  async function refreshHistory() {
    const mine = ++generation;
    const fresh = (await api(`/api/decisions?space=${encodeURIComponent(space())}`)).decisions;
    if (mine !== generation) return;
    rows = fresh;
    for (const id of [...selected]) if (!rows.some((r) => r.id === id)) selected.delete(id);
    setUsedTags(usedTags());
    if (filter !== "all" && !tagCounts().has(filter)) {
      filter = "all";
      page = 1;
    }
    renderFilters();
    renderGrid();
    if (openId) {
      const r = rows.find((row) => row.id === openId);
      if (r) renderDetail(r);
      else showLibraryList();
    }
  }
  function tagCounts() {
    const counts = /* @__PURE__ */ new Map();
    for (const r of rows) {
      const tags = tagsOf(r);
      if (!tags.length) counts.set(UNTAGGED, (counts.get(UNTAGGED) || 0) + 1);
      for (const t of tags) counts.set(t, (counts.get(t) || 0) + 1);
    }
    return counts;
  }
  function usedTags() {
    const counts = tagCounts();
    counts.delete(UNTAGGED);
    const preset = suggestedTags().filter((t) => counts.has(t));
    const own = [...counts.keys()].filter((t) => !suggestedTags().includes(t)).sort((a, b) => counts.get(b) - counts.get(a) || a.localeCompare(b));
    return [...preset, ...own];
  }
  function renderFilters() {
    const counts = tagCounts();
    const tags = usedTags();
    const visible = showAllTags || tags.length <= TAG_PREVIEW ? tags : tags.slice(0, TAG_PREVIEW);
    if (filter !== "all" && filter !== UNTAGGED && !visible.includes(filter)) visible.push(filter);
    const chip = (value, label, n, extra = "") => `<button type="button" class="filter-tag${extra}${filter === value ? " active" : ""}" role="tab" aria-selected="${filter === value}" data-filter="${esc(value)}">${label} <b>${n}</b></button>`;
    const custom = (t) => suggestedTags().includes(t) ? "" : " own";
    $("filter-tags").innerHTML = chip("all", "All", rows.length) + visible.map((t) => chip(t, esc(t), counts.get(t) || 0, custom(t))).join("") + (counts.has(UNTAGGED) && tags.length ? chip(UNTAGGED, "Untagged", counts.get(UNTAGGED) || 0, " untagged") : "") + (tags.length > TAG_PREVIEW ? `<button type="button" class="text-button more-tags" aria-expanded="${showAllTags}">${showAllTags ? "Fewer tags" : `+${tags.length - TAG_PREVIEW} more`}</button>` : "");
    $("filter-tags").querySelectorAll(".filter-tag").forEach((b) => b.addEventListener("click", () => {
      setFilter(b.dataset.filter || "all");
      renderFilters();
      renderGrid();
    }));
    $("filter-tags").querySelector(".more-tags")?.addEventListener("click", () => {
      showAllTags = !showAllTags;
      renderFilters();
    });
  }
  function weakestAnswer(r) {
    let best = null;
    Object.entries(r.response.answers || {}).forEach(([id, a], i) => {
      const top = Math.max(...Object.values(a.probabilities));
      if (!best || top < best.probability) best = { probability: top, title: questionTitle(id, a, r.request.questions, i) };
    });
    return best;
  }
  function filtered() {
    const list = filter === "all" ? rows : filter === UNTAGGED ? rows.filter((r) => !tagsOf(r).length) : rows.filter((r) => tagsOf(r).includes(filter));
    const order = $select("library-sort").value;
    if (order === "oldest") return [...list].reverse();
    if (order === "confidence") {
      return [...list].sort((a, b) => (weakestAnswer(a)?.probability ?? 2) - (weakestAnswer(b)?.probability ?? 2) || a.created.localeCompare(b.created));
    }
    return list;
  }
  async function exportTraining() {
    const result = await api(`/api/training-export?space=${encodeURIComponent(space())}`);
    if (!result.records.length) {
      toast("No corrected decisions to export yet. Open a decision and record a correction first.");
      return;
    }
    downloadText(`typelogic-training-${space()}.jsonl`, result.records.map((r) => JSON.stringify(r)).join("\n") + "\n");
    toast(`${result.records.length} decisions exported for training (${result.corrections} corrections). Nothing was retrained.`);
  }
  function answerSummary(id, a) {
    const top = Object.entries(a.probabilities).sort((x, y) => y[1] - x[1])[0];
    const value = a.type === "noul" ? top[0] === "true" ? "Yes" : "No" : a.type === "score" ? `${(a.score ?? 0).toFixed(2)}` : a.choice || top[0];
    const criteria = a.criteria && typeof a.criteria === "object" && !Array.isArray(a.criteria) ? a.criteria : {};
    const text = a.type === "choice" && criteria[value] ? /^\d+$/.test(value) ? criteria[value] : `${value} · ${criteria[value]}` : a.type === "score" ? `${value} · ${a.legend?.[top[0]] || top[0]}` : value;
    return `<li><span class="mini-q">${esc(a.instructions || id)}</span><span class="mini-a">${esc(text)} <em>${(top[1] * 100).toFixed(0)}%</em></span></li>`;
  }
  function cardHTML(r) {
    const date = new Date(r.created);
    const when = Number.isNaN(date.getTime()) ? "" : date.toLocaleString("en-GB", { dateStyle: "medium", timeStyle: "short" });
    const answers = orderedAnswers(r.response.answers || {}, r.request);
    const shown = answers.slice(0, 3).map(([id, a]) => answerSummary(id, a)).join("");
    const more = answers.length > 3 ? `<li class="mini-more">+${answers.length - 3} more</li>` : "";
    const weakest = weakestAnswer(r);
    return `<article class="library-card${selected.has(r.id) ? " selected" : ""}" data-id="${esc(r.id)}" tabindex="0" role="button" aria-label="Open ${esc(r.title)}"><div class="card-top"><label class="card-select" title="Select"><input type="checkbox" class="select-request" aria-label="Select decision"${selected.has(r.id) ? " checked" : ""}></label><time>${esc(when)}</time></div><h3>${esc(r.title)}</h3>${tagChips(tagsOf(r), "tag-list card-tags")}<ul class="mini-answers">${shown}${more}</ul><div class="card-foot">${weakest ? `Lowest confidence <strong>${(weakest.probability * 100).toFixed(0)}%</strong>` : ""}<span>${r.integrity?.verified === false ? "⚠ integrity check failed" : ""}</span></div></article>`;
  }
  function pageButtons(pages) {
    const want = new Set([1, pages, page - 2, page - 1, page, page + 1, page + 2].filter((n) => n >= 1 && n <= pages));
    const numbers = [...want].sort((a, b) => a - b);
    let out = `<button type="button" class="page-step" data-page="${page - 1}"${page === 1 ? " disabled" : ""} aria-label="Previous page">‹ Previous</button>`;
    numbers.forEach((n, i) => {
      if (i && n - numbers[i - 1] > 1) out += '<span class="page-gap">…</span>';
      out += `<button type="button" class="page-number${n === page ? " active" : ""}" data-page="${n}"${n === page ? ' aria-current="page"' : ""} aria-label="Page ${n}">${n}</button>`;
    });
    return out + `<button type="button" class="page-step" data-page="${page + 1}"${page === pages ? " disabled" : ""} aria-label="Next page">Next ›</button>`;
  }
  function renderGrid() {
    const list = filtered();
    const pages = Math.max(1, Math.ceil(list.length / PAGE_SIZE));
    page = Math.min(Math.max(1, page), pages);
    const start2 = (page - 1) * PAGE_SIZE;
    const shown = list.slice(start2, start2 + PAGE_SIZE);
    const label = filter === "all" ? "" : filter === UNTAGGED ? "untagged " : `“${filter}” `;
    $("library-grid").innerHTML = shown.length ? shown.map(cardHTML).join("") : `<div class="library-empty"><strong>No ${esc(label)}decisions in this workspace.</strong><span>Evaluate a decision on the New decision page; it appears here.</span></div>`;
    $("library-range").textContent = list.length ? `Showing ${start2 + 1}–${start2 + shown.length} of ${list.length}` : "";
    const pager = $("library-pager");
    pager.classList.toggle("hidden", pages <= 1);
    pager.innerHTML = pages > 1 ? pageButtons(pages) : "";
    pager.querySelectorAll("button[data-page]").forEach((b) => b.addEventListener("click", () => {
      page = Number(b.dataset.page);
      renderGrid();
      $("library-list").scrollIntoView({ block: "start" });
    }));
    document.querySelectorAll(".library-card").forEach((card) => {
      const id = card.dataset.id;
      const box = card.querySelector(".select-request");
      box.addEventListener("click", (e) => e.stopPropagation());
      card.querySelector(".card-select").addEventListener("click", (e) => e.stopPropagation());
      box.addEventListener("change", () => {
        if (box.checked) selected.add(id);
        else selected.delete(id);
        card.classList.toggle("selected", box.checked);
        updateSelection();
      });
      const open = () => {
        const r = rows.find((row) => row.id === id);
        if (r) openDetail(r);
      };
      card.addEventListener("click", open);
      card.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          open();
        }
      });
    });
    updateSelection();
  }
  function updateSelection() {
    const count = selected.size;
    $("selection-bar").classList.toggle("hidden", count === 0);
    $("selection-count").textContent = `${count} selected`;
    const boxes = document.querySelectorAll(".select-request");
    $input("select-all").checked = boxes.length > 0 && [...boxes].every((b) => b.checked);
  }
  async function deleteSelected() {
    try {
      const ids = [...selected];
      if (!ids.length) return;
      const result = await api("/api/delete", { ids });
      const failed = result.results.filter((r) => r.error);
      toast(`${result.deleted} decision${result.deleted === 1 ? "" : "s"} deleted` + (failed.length ? ` · ${failed.length} failed: ${failed[0].error}` : ""));
      selected.clear();
      const current2 = currentDecisionId();
      if (current2 && ids.includes(current2)) clearCurrentResults();
      await refreshHistory();
      await refreshStatus();
    } catch (e) {
      reportError(e);
    }
  }
  function optionText(a, key) {
    const criteria = a.criteria && typeof a.criteria === "object" && !Array.isArray(a.criteria) ? a.criteria : void 0;
    return a.type === "choice" && criteria?.[key] ? `${key} — ${criteria[key]}` : a.legend?.[key] || key;
  }
  function bindCorrection(view, r) {
    const form = view.querySelector(".feedback-form");
    form.classList.toggle("hidden");
    if (form.innerHTML) return;
    form.innerHTML = `<label>Question<select class="feedback-question">${Object.entries(r.response.answers).map(([id, a], i) => `<option value="${esc(id)}">${esc(questionTitle(id, a, r.request.questions, i))}</option>`).join("")}</select></label><label>Observed alternative<select class="feedback-option"></select></label><button class="button secondary save-feedback">Save correction</button><p class="micro">The label is stored; the model is not retrained automatically.</p>`;
    const question = form.querySelector(".feedback-question");
    const option = form.querySelector(".feedback-option");
    const update = () => {
      const a = r.response.answers[question.value];
      option.innerHTML = Object.keys(a.probabilities).map((key) => `<option value="${esc(key)}">${esc(optionText(a, key))}</option>`).join("");
    };
    question.addEventListener("change", update);
    update();
    form.querySelector(".save-feedback").addEventListener("click", async () => {
      try {
        const question_id = question.value, chosen3 = option.value;
        const target = Object.fromEntries(Object.keys(r.response.answers[question_id].probabilities).map((key) => [key, key === chosen3 ? 1 : 0]));
        await api("/api/feedback", { id: r.id, question_id, target });
        toast("Correction saved");
        await refreshStatus();
      } catch (e) {
        reportError(e);
      }
    });
  }
  function renderDetail(r) {
    const body = $("library-detail-body");
    body.innerHTML = `<article class="panel detail-card"><div class="actions detail-actions"><button class="button danger ghost detail-delete" title="Delete this decision and its associated records">Delete</button><span class="actions-gap"></span><button class="button secondary detail-correction">Record correction</button><button class="button secondary detail-export">Export Full JSON</button><button class="button secondary detail-export-lite">Export Lite JSON</button></div><div class="feedback-form hidden"></div>${identityHTML({ ...r, tags: tagsOf(r), title_source: r.response.title_source })}` + orderedAnswers(r.response.answers, r.request).map(([id, a], i) => resultHTML(id, a, r.request.questions, i)).join("") + `${exportGuide(true)}</article>`;
    body.querySelector(".detail-delete").addEventListener("click", async () => {
      try {
        await api("/api/delete", { id: r.id });
        if (currentDecisionId() === r.id) clearCurrentResults();
        toast("Decision and associated records deleted");
        showLibraryList();
        await refreshHistory();
        await refreshStatus();
      } catch (e) {
        reportError(e);
      }
    });
    body.querySelector(".detail-export").addEventListener("click", () => exportDecision(r.id, "full"));
    body.querySelector(".detail-export-lite").addEventListener("click", () => exportDecision(r.id, "lite"));
    body.querySelector(".detail-correction").addEventListener("click", () => bindCorrection(body, r));
  }
  function openDetail(r) {
    openId = r.id;
    renderDetail(r);
    $("library-list").classList.add("hidden");
    $("library-detail").classList.remove("hidden");
    window.scrollTo({ top: 0, behavior: "instant" });
  }
  function installLibrary() {
    $select("library-sort").addEventListener("change", () => {
      page = 1;
      renderGrid();
    });
    $("library-back").addEventListener("click", () => {
      showLibraryList();
      renderGrid();
    });
    $("bulk-delete").addEventListener("click", () => {
      deleteSelected();
    });
    $("clear-selection").addEventListener("click", () => {
      selected.clear();
      document.querySelectorAll(".select-request").forEach((b) => {
        b.checked = false;
      });
      document.querySelectorAll(".library-card.selected").forEach((c) => c.classList.remove("selected"));
      updateSelection();
    });
    $input("select-all").addEventListener("change", () => {
      const on = $input("select-all").checked;
      document.querySelectorAll(".select-request").forEach((box) => {
        box.checked = on;
        const card = box.closest(".library-card");
        card.classList.toggle("selected", on);
        if (on) selected.add(card.dataset.id);
        else selected.delete(card.dataset.id);
      });
      updateSelection();
    });
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape" && openId && !document.querySelector("#help-popup:not([hidden])") && !document.querySelector("#archive-dialog")?.open) {
        showLibraryList();
        renderGrid();
      }
    });
  }

  // src/modes.ts
  var MODES = ["auto", "cpu"];
  var LABELS = { auto: "Automatic", cpu: "CPU only" };
  var modeLabel = (mode) => mode && LABELS[mode] || "";
  var chosen2 = () => document.querySelector('input[name="engine-mode"]:checked')?.value || "auto";
  var combination = () => document.querySelector('input[name="engine-combine"]:checked')?.value || "route";
  var currentMode = "";
  async function loadModes() {
    const info = await api("/api/modes");
    $("mode-current").textContent = `Current mode: ${modeLabel(info.mode) || info.mode}${info.engine_status ? " · engine " + info.engine_status.replaceAll("_", " ") : ""}`;
    for (const mode of MODES) {
      const option = info.availability[mode];
      const input = document.querySelector(`input[name="engine-mode"][value="${mode}"]`);
      if (!input || !option) continue;
      input.checked = info.mode === mode;
      input.disabled = !option.available && info.mode !== mode;
      input.closest("label")?.classList.toggle("unavailable", !option.available);
      $(`mode-detail-${mode}`).textContent = option.detail || "";
    }
    $("mode-plan").textContent = info.plan ? `Loaded now: ${info.plan}.` : "";
    currentMode = info.mode;
    const combine = info.settings?.combine || "route";
    for (const input of document.querySelectorAll('input[name="engine-combine"]')) input.checked = input.value === combine;
  }
  async function applyMode() {
    const button = $button("apply-mode"), status = $("mode-status");
    button.disabled = true;
    status.textContent = "Applying…";
    try {
      const restart = chosen2() !== currentMode;
      const result = await api("/api/mode", { mode: chosen2(), combine: combination() });
      const how = combination() === "ensemble" ? "combined" : "routed";
      status.textContent = result.loading ? `Mode ${modeLabel(result.mode)} saved, questions ${how}. Loading the models…${result.note ? " " + result.note + "." : ""}` : `Saved: questions ${how}. The models stay loaded.`;
      toast(restart ? `Engine mode: ${modeLabel(result.mode)}` : `Questions up to 16 options: ${how}`);
      await refreshStatus();
      await loadModes();
    } catch (e) {
      status.textContent = "";
      reportError(e);
    } finally {
      button.disabled = false;
    }
  }
  function installModes() {
    $("apply-mode").addEventListener("click", () => {
      applyMode().catch(reportError);
    });
    loadModes().catch(reportError);
  }

  // src/evaluate.ts
  var TYPE_LABELS = { noul: "Yes / no", choice: "Choice", score: "Score" };
  var ENGINE_LABELS = { generalist: "Plumb", specialist: "Specialist", clef: "Clef-Flash", ensemble: "Combined", failed: "Failed", unknown: "Unknown" };
  var pct = (v, digits = 1) => v === null || v === void 0 ? "–" : `${(100 * v).toFixed(digits)}%`;
  var fix = (v, digits = 3) => v === null || v === void 0 ? "–" : v.toFixed(digits);
  var signed = (v) => v === null || v === void 0 ? "–" : `${v >= 0 ? "+" : "−"}${Math.abs(100 * v).toFixed(1)} pt`;
  var poll;
  var watching = false;
  var fileText = null;
  function groupTable(title, groups, labels = {}) {
    const rows2 = Object.entries(groups).map(([name, g]) => `<tr><th scope="row">${esc(labels[name] || name)}</th><td>${g.questions}</td><td>${pct(g.accuracy)}</td><td>${pct(g.chance)}</td><td>${fix(g.skill)}</td><td>${fix(g.brier)}</td><td>${fix(g.ece_10)}</td><td>${fix(g.nll)}</td><td>${g.valid < g.questions ? `${g.questions - g.valid}` : "0"}</td></tr>`).join("");
    return `<h4>${esc(title)}</h4><div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th></th><th>Questions</th><th>Accuracy</th><th>Chance</th><th>Skill</th><th>Brier</th><th>ECE</th><th>NLL</th><th>Failed</th></tr></thead><tbody>${rows2}</tbody></table></div>`;
  }
  function probesHtml(p) {
    const o = p.order, pos = p.position, pol = p.polarity, len = p.leniency;
    const line = (name, value, detail) => `<tr><th scope="row">${esc(name)}</th><td>${value}</td><td>${esc(detail)}</td></tr>`;
    return `<h4>Bias probes</h4><div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Probe</th><th>Result</th><th>What it measures</th></tr></thead><tbody>
    ${o.checked ? line("Option order", `${pct(o.flip_rate)} flipped`, `${o.top_choice_changed} of ${o.checked} Choice answers changed their top choice when the options were reordered; mean largest shift ${fix(o.mean_max_shift)}, max ${fix(o.max_shift)}`) : line("Option order", "–", "Not run (enable the order probe)")}
    ${line("First option", signed(pos.first_excess), `chosen ${pct(pos.predicted_first)} of ${pos.questions} Choice questions with 3+ options; gold first ${pct(pos.gold_first)}`)}
    ${line("Last option", signed(pos.last_excess), `chosen ${pct(pos.predicted_last)}; gold last ${pct(pos.gold_last)}`)}
    ${line("Yes polarity", signed(pol.yes_excess), `Yes answered on ${pct(pol.predicted_yes)} of ${pol.questions} Yes / no questions; gold Yes ${pct(pol.gold_yes)}`)}
    ${line("Score leniency", len.excess === null ? "–" : `${len.excess >= 0 ? "+" : "−"}${Math.abs(len.excess).toFixed(2)} levels`, `mean expected level ${fix(len.mean_expected_level, 2)} vs gold ${fix(len.mean_gold_level, 2)} on ${len.questions} Score questions`)}
  </tbody></table></div><p class="micro">${esc(p.note)}. Positive values mean the models pick that option more often than the gold answers do.</p>`;
  }
  function renderSummary(job) {
    const box = $("eval-result");
    const s = job.summary;
    if (!s) {
      box.innerHTML = '<div class="empty">No results yet.<p>Start an evaluation to see the Decision Index, calibration and bias probes.</p></div>';
      return;
    }
    const d = s.decision_index, o = s.overall;
    const tiers = d.tiers.map((t) => `<tr><th scope="row">${esc(t.tier)}</th><td>${t.weight}</td><td>${t.questions}</td><td>${pct(t.accuracy)}</td><td>${pct(t.chance)}</td><td>${fix(t.skill)}</td></tr>`).join("");
    const sel = Object.entries(s.selective).map(([t, v]) => `<tr><th scope="row">≥ ${esc(t)}</th><td>${pct(v.coverage)}</td><td>${pct(v.accuracy)}</td><td>${v.accepted}</td></tr>`).join("");
    const partial = job.status === "running" ? `<p class="notice">Partial results: ${job.done} of ${job.total} items answered.</p>` : job.status === "stopped" ? `<p class="notice">Stopped after ${job.done} of ${job.total} items; the numbers cover only those.</p>` : "";
    box.classList.remove("empty");
    box.innerHTML = `${partial}<div class="metrics eval-metrics">
      <div class="metric"><strong id="decision-index">${d.value === null ? "–" : d.value.toFixed(1)}</strong><span>Decision Index (0–100)</span></div>
      <div class="metric"><strong>${pct(o.accuracy)}</strong><span>Accuracy · ${o.correct} of ${o.questions}${o.valid < o.questions ? ` · ${o.questions - o.valid} failed` : ""}</span></div>
      <div class="metric"><strong>${fix(o.brier)}</strong><span>Brier · ECE ${fix(o.ece_10)}</span></div>
      <div class="metric"><strong>${d.speed === null ? "–" : d.speed.toFixed(0)}</strong><span>Speed · p50 ${fix(o.latency_p50_s, 2)} s, p95 ${fix(o.latency_p95_s, 2)} s</span></div>
    </div>
    <details class="eval-formula"><summary>How the Decision Index is computed</summary><p>${esc(d.formula)}.</p><p>Speed: ${esc(d.speed_formula)}.</p><p>${esc(d.note)}.</p>
      <div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Tier</th><th>Weight</th><th>Questions</th><th>Accuracy</th><th>Chance</th><th>Skill (≥ 0)</th></tr></thead><tbody>${tiers}</tbody></table></div></details>
    ${groupTable("By tier", s.by_tier)}${groupTable("By question type", s.by_type, TYPE_LABELS)}${groupTable("By engine", s.by_engine, ENGINE_LABELS)}
    ${probesHtml(s.probes)}
    <h4>Selective accuracy</h4><div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Top probability</th><th>Coverage</th><th>Accuracy</th><th>Answers</th></tr></thead><tbody>${sel}</tbody></table></div>
    <p class="micro">Accuracy counts failed answers as wrong. Skill = (accuracy − chance) / (1 − chance), chance = mean of 1 / number of options. Brier is multi-class; ECE uses ten top-probability bins.${o.ordinal_mae !== null ? ` Ordinal MAE (Score): ${fix(o.ordinal_mae, 2)} levels.` : ""}</p>`;
  }
  function renderJob(job) {
    const running = job.status === "running";
    $button("eval-start").disabled = running;
    $button("eval-stop").classList.toggle("hidden", !running);
    const progress = $("eval-progress");
    progress.classList.toggle("hidden", job.status === "idle");
    const share = job.total ? (job.done || 0) / job.total : 0;
    $("eval-bar").firstElementChild.style.width = `${(100 * share).toFixed(1)}%`;
    const states = { running: "Running", done: "Finished", stopped: "Stopped", idle: "" };
    $("eval-status").textContent = job.status === "idle" ? "" : `${states[job.status] || job.status}: ${job.done} of ${job.total} items · ${Math.round(job.elapsed_s || 0)} s${job.saved ? ` · saved as data/evaluations/${job.saved}` : ""}${job.error ? ` · ${job.error}` : ""}`;
    renderSummary(job);
  }
  async function refreshJob() {
    const job = await api("/api/eval");
    renderJob(job);
    if (job.status === "running") {
      clearTimeout(poll);
      poll = window.setTimeout(() => {
        refreshJob().catch(reportError);
      }, 1500);
    } else if (watching) {
      watching = false;
      loadSaved().catch(reportError);
    }
    return job;
  }
  async function loadSaved() {
    const { evaluations } = await api("/api/evaluations");
    const box = $("eval-saved");
    if (!evaluations.length) {
      box.innerHTML = '<p class="micro">No saved evaluation yet. Each finished run is saved in data/evaluations.</p>';
      return;
    }
    const label = (c) => `${c.source === "jevbench" ? "JevBench" : "File"} · ${ENGINE_LABELS[String(c.engine)] || "Automatic"}${c.combine === "ensemble" ? " · combined" : ""}`;
    box.innerHTML = `<div class="evidence-scroll"><table class="evidence-table eval-table"><thead><tr><th>Date (UTC)</th><th>Run</th><th>Questions</th><th>Index</th><th>Accuracy</th><th>Brier</th><th></th></tr></thead><tbody>${evaluations.map((e) => `<tr><td>${esc(e.created.slice(5, 16).replace("T", " "))}</td><td>${esc(label(e.config || {}))}${e.status === "stopped" ? " (stopped)" : ""}</td><td>${e.questions ?? "–"}</td><td>${e.decision_index === null ? "–" : e.decision_index.toFixed(1)}</td><td>${pct(e.accuracy)}</td><td>${fix(e.brier)}</td><td><button type="button" class="text-button" data-eval="${esc(e.name)}" data-format="json">JSON</button> <button type="button" class="text-button" data-eval="${esc(e.name)}" data-format="jsonl">JSONL</button></td></tr>`).join("")}</tbody></table></div>`;
    box.querySelectorAll("button[data-eval]").forEach((b) => b.addEventListener("click", () => {
      downloadSaved(b.dataset.eval || "", b.dataset.format || "json").catch(reportError);
    }));
  }
  async function downloadSaved(name, format) {
    const token = document.querySelector('meta[name="typelogic-token"]').content;
    const response = await fetch(`/api/evaluation?name=${encodeURIComponent(name)}&format=${format}`, { headers: { "X-TypeLogic-Token": token } });
    if (!response.ok) throw Error(`Download failed (${response.status})`);
    downloadText(format === "jsonl" ? name.replace(/\.json$/, ".jsonl") : name, await response.text(), format === "jsonl" ? "application/x-ndjson" : "application/json");
  }
  function sourceChanged() {
    const file = $input("eval-source-file").checked;
    $("eval-tiers").classList.toggle("hidden", file);
    $("eval-file-line").classList.toggle("hidden", !file);
  }
  async function start() {
    const file = $input("eval-source-file").checked;
    const tiers = Array.from(document.querySelectorAll('input[name="eval-tier"]:checked')).map((i) => i.value);
    if (!file && !tiers.length) {
      toast("Choose at least one JevBench tier.");
      return;
    }
    if (file && !fileText) {
      toast("Choose a JSONL file first.");
      return;
    }
    const limit = $input("eval-limit").value.trim();
    const engine = $select("eval-engine").value;
    const body = {
      source: file ? "file" : "jevbench",
      engine,
      combine: engine === "auto" ? $select("eval-combine").value : "route",
      probes: $input("eval-probes").checked
    };
    if (file) body.jsonl = fileText;
    else body.tiers = tiers;
    if (limit) {
      const n = Number(limit);
      if (!Number.isInteger(n) || n < 1) {
        toast("The limit is a whole number of items.");
        return;
      }
      body.limit = n;
    }
    await api("/api/eval/start", body);
    watching = true;
    toast("Evaluation started. You can keep using TypeLogic; answers are slower meanwhile.");
    await refreshJob();
  }
  async function loadEvaluate() {
    const status = await api("/api/status");
    const loaded = status.engine.engines || [];
    for (const option of Array.from($select("eval-engine").options)) {
      option.disabled = option.value !== "auto" && !loaded.includes(option.value);
    }
    if ($select("eval-engine").selectedOptions[0]?.disabled) $select("eval-engine").value = "auto";
    await Promise.all([refreshJob(), loadSaved()]);
  }
  function installEvaluate() {
    $input("eval-source-jevbench").addEventListener("change", sourceChanged);
    $input("eval-source-file").addEventListener("change", sourceChanged);
    $select("eval-engine").addEventListener("change", () => {
      $select("eval-combine").disabled = $select("eval-engine").value !== "auto";
    });
    $input("eval-file").addEventListener("change", async () => {
      const f = $input("eval-file").files?.[0];
      fileText = f ? await f.text() : null;
      $("eval-file-info").textContent = f ? `${f.name}: ${fileText?.split("\n").filter((l) => l.trim()).length} rows` : "";
    });
    $("eval-start").addEventListener("click", () => {
      start().catch(reportError);
    });
    $("eval-stop").addEventListener("click", () => {
      api("/api/eval/stop", {}).then(() => refreshJob()).catch(reportError);
    });
    sourceChanged();
  }

  // src/app.ts
  var PAGE_TITLES = {
    decide: "New decision",
    library: "Library",
    evaluate: "Evaluate",
    settings: "Engine & privacy",
    about: "About TypeLogic AI"
  };
  function showPage(page2) {
    window.scrollTo({ top: 0, behavior: "instant" });
    document.querySelectorAll(".page").forEach((n) => n.classList.toggle("hidden", n.id !== `page-${page2}`));
    document.querySelectorAll(".nav").forEach((n) => n.classList.toggle("active", n.dataset.page === page2));
    $("page-title").textContent = PAGE_TITLES[page2];
    if (page2 === "library") {
      showLibraryList();
      refreshHistory().catch(reportError);
    }
    if (page2 === "settings") {
      loadModes().catch(reportError);
      loadNeedle();
    }
    if (page2 === "evaluate") loadEvaluate().catch(reportError);
    refreshStatus().catch(reportError);
  }
  async function loadSettings() {
    const settings = await api("/api/settings");
    $select("extract-backend").value = settings.extract_backend;
  }
  function confirmArchive(info) {
    const dialog = $("archive-dialog");
    $("archive-text").textContent = `The library holds ${info.count} decisions; the maximum is ${info.limit}. To continue, all decision files and data will be compressed into one ${info.format === "rar" ? ".rar" : ".zip"} file, ${info.name}, saved in the data folder of the TypeLogic AI project (${info.folder}), and removed from the library.`;
    $("archive-note").textContent = info.note ? `${info.note}. Install WinRAR to get .rar files.` : "The archive is written and tested before anything is removed.";
    return new Promise((resolve) => {
      const done = (value) => {
        dialog.close();
        resolve(value);
      };
      $("archive-confirm").onclick = () => done(true);
      $("archive-cancel").onclick = () => done(false);
      dialog.oncancel = (e) => {
        e.preventDefault();
        done(false);
      };
      dialog.showModal();
    });
  }
  async function ensureRoom() {
    const info = await api("/api/archive");
    if (!info.full) return true;
    if (!await confirmArchive(info)) {
      toast("Not evaluated: the library is full. Archive it to continue, or delete decisions in the Library.");
      return false;
    }
    $("results").classList.remove("empty");
    $("results").innerHTML = `<div class="loading"><span class="spinner"></span>Compressing ${info.count} decisions into ${esc(info.name)}…</div>`;
    const result = await api("/api/archive", { confirm: true });
    toast(`${result.decisions} decisions archived to ${result.path}. The library starts empty.`);
    await refreshStatus();
    return true;
  }
  function installDecide() {
    $("add-question").addEventListener("click", () => addQuestion());
    installCaseBuilder();
    $select("extract-backend").addEventListener("change", () => api("/api/settings", { extract_backend: $select("extract-backend").value }).catch(reportError));
    loadSettings().catch(reportError);
    $("preview-request").addEventListener("click", () => {
      try {
        $("request-output").textContent = JSON.stringify(buildCaseRequest(), null, 2);
      } catch (e) {
        reportError(e);
      }
    });
    $("copy-request").addEventListener("click", async () => {
      try {
        const text = JSON.stringify(buildCaseRequest(), null, 2);
        $("request-output").textContent = text;
        if (!navigator.clipboard?.writeText) throw Error("Clipboard unavailable. Select and copy the request preview.");
        await navigator.clipboard.writeText(text);
        toast("Request JSON copied; no evaluation or record was created.");
      } catch (e) {
        reportError(e);
      }
    });
    $("evaluate").addEventListener("click", async () => {
      try {
        const payload = buildCaseRequest();
        setEvaluating(true);
        $button("evaluate").disabled = true;
        if (!await ensureRoom()) return;
        $("results").classList.remove("empty");
        $("results").innerHTML = '<div class="loading"><span class="spinner"></span>Evaluating on your computer…<p class="micro">The first load takes longer. Order checks run additional passes. Without a title, a short one is written at the end.</p></div>';
        renderResult(await api("/api/evaluate", payload));
        await refreshStatus();
      } catch (e) {
        $("results").innerHTML = `<div class="error-box">${esc(errorMessage(e))}</div>`;
      } finally {
        setEvaluating(false);
        $button("evaluate").disabled = !isEngineReady();
      }
    });
  }
  function installSettings() {
    installModes();
    installEvaluate();
    $("retry-engine").addEventListener("click", async () => {
      try {
        await api("/api/load", {});
        await refreshStatus();
      } catch (e) {
        reportError(e);
      }
    });
    $("reload-status").addEventListener("click", () => refreshStatus().catch(reportError));
    $("apply-profile").addEventListener("click", () => {
      const profile2 = engineProfile();
      if (!profile2) return;
      loadQuestions(profile2);
      showPage("decide");
      toast("Profile questions applied");
    });
    $("refresh-history").addEventListener("click", () => refreshHistory().catch(reportError));
    const closeMenu = () => document.querySelectorAll("details.menu").forEach((m) => {
      m.open = false;
    });
    $("export-training").addEventListener("click", () => {
      closeMenu();
      exportTraining().catch(reportError);
    });
    $("export-all").addEventListener("click", async () => {
      closeMenu();
      try {
        download("typelogic-export.json", await api("/api/export"));
      } catch (e) {
        reportError(e);
      }
    });
    document.addEventListener("click", (e) => {
      if (!(e.target instanceof Element && e.target.closest("details.menu"))) closeMenu();
    });
  }
  function installTheme() {
    const picker = $select("theme-select");
    picker.value = window.TypeLogicTheme.get();
    picker.addEventListener("change", () => window.TypeLogicTheme.set(picker.value));
    window.addEventListener("typelogic-theme-change", () => {
      picker.value = window.TypeLogicTheme.get();
    });
  }
  installHelp();
  installTheme();
  document.querySelectorAll(".nav").forEach((n) => n.addEventListener("click", () => showPage(n.dataset.page || "decide")));
  $("space").addEventListener("change", () => {
    refreshHistory().catch(reportError);
  });
  installDecide();
  installLibrary();
  installSettings();
  refreshStatus().catch(reportError);
  document.querySelectorAll(".help-icon[data-help]").forEach((button) => {
    button.innerHTML = helpGlyph();
  });
})();
