"use strict";

const $ = (id) => document.getElementById(id);
const COMPENSATION_HOURS = 8;
const numberFormat = new Intl.NumberFormat("hu-HU", { maximumFractionDigits: 2 });

const state = { editingId: null, previewToken: 0, toastTimer: null, summary: null };

function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (key === "class") node.className = value;
    else if (key === "onclick") node.addEventListener("click", value);
    else node.setAttribute(key, value);
  }
  node.append(...children);
  return node;
}

const hours = (minutes) => `${numberFormat.format(minutes / 60)} óra`;
const shortHours = (minutes) => `${numberFormat.format(minutes / 60)} ó`;
const formatDate = (iso) => iso.slice(0, 10);
const pad = (n) => String(n).padStart(2, "0");

function today() {
  const d = new Date();
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

// Accepts 17:30, 17.30, 1730, 930 and 9; returns HH:MM in 24-hour form, or null.
function normalizeTime(text) {
  const value = text.trim();
  let match = value.match(/^(\d{1,2})[:.](\d{2})$/);
  if (!match && /^\d{1,4}$/.test(value)) {
    match = value.length <= 2
      ? [value, value, "00"]
      : [value, value.slice(0, -2), value.slice(-2)];
  }
  if (!match) return null;
  const hour = Number(match[1]);
  const minute = Number(match[2]);
  return hour <= 23 && minute <= 59 ? `${pad(hour)}:${pad(minute)}` : null;
}

// Accepts 2026-11-10, 2026.11.10., 2026. 11. 10. and 20261110; returns YYYY-MM-DD or null.
function normalizeDate(text) {
  const value = text.trim();
  const match = value.match(/^(\d{4})[-.\/\s]+(\d{1,2})[-.\/\s]+(\d{1,2})\.?$/)
    || value.match(/^(\d{4})(\d{2})(\d{2})$/);
  if (!match) return null;
  const [year, month, day] = match.slice(1).map(Number);
  const d = new Date(Date.UTC(year, month - 1, day));
  const valid = d.getUTCMonth() === month - 1 && d.getUTCDate() === day;
  return valid ? `${match[1]}-${pad(month)}-${pad(day)}` : null;
}

// Accepts 2026-11, 2026.11. and 202611; returns YYYY-MM or null.
function normalizeMonth(text) {
  const value = text.trim();
  const match = value.match(/^(\d{4})[-.\/\s]+(\d{1,2})\.?$/) || value.match(/^(\d{4})(\d{2})$/);
  if (!match) return null;
  const month = Number(match[2]);
  return month >= 1 && month <= 12 ? `${match[1]}-${pad(month)}` : null;
}

// Text field in a fixed format, with the browser's calendar popup behind a button.
function attachPicker(input) {
  const kind = input.dataset.picker;
  const normalize = kind === "month" ? normalizeMonth : normalizeDate;
  const native = el("input", { type: kind, class: "native-picker", tabindex: "-1", "aria-hidden": "true" });
  const button = el("button", {
    type: "button",
    class: "picker-button",
    title: "Naptár",
    "aria-label": "Naptár megnyitása",
    onclick: guarded(() => {
      native.value = normalize(input.value) ?? "";
      native.showPicker();
    }),
  });
  const wrap = el("span", { class: "date-wrap" });
  input.replaceWith(wrap);
  wrap.append(input, native, button);
  button.hidden = native.type !== kind || !native.showPicker;

  input.addEventListener("change", () => {
    input.value = normalize(input.value) ?? input.value;
  });
  native.addEventListener("change", () => {
    if (!native.value) return;
    input.value = native.value;
    input.dispatchEvent(new Event("change"));
  });
}

function readDateTime(field) {
  const day = normalizeDate($(`${field}-date`).value);
  const time = normalizeTime($(`${field}-time`).value);
  return day && time ? `${day}T${time}` : "";
}

function writeDateTime(field, value) {
  const [day, time] = value.split("T");
  $(`${field}-date`).value = day;
  $(`${field}-time`).value = time;
}

// Values are wall-clock times, so the arithmetic runs in UTC to avoid DST shifts.
function addMinutes(value, minutes) {
  const [year, month, day, hour, minute] = value.split(/[-T:]/).map(Number);
  const d = new Date(Date.UTC(year, month - 1, day, hour, minute) + minutes * 60000);
  return `${d.getUTCFullYear()}-${pad(d.getUTCMonth() + 1)}-${pad(d.getUTCDate())}`
    + `T${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}`;
}

function setEndAfterStart(minutes) {
  const base = readDateTime("start");
  if (!base) {
    showError("Előbb add meg a kezdés dátumát és idejét.");
    return;
  }
  clearError();
  writeDateTime("end", addMinutes(base, minutes));
  updatePreview();
}

function setDefaultDates() {
  $("start-date").value = today();
  $("end-date").value = today();
}

function showError(message) {
  const toast = $("toast");
  toast.textContent = message;
  toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => { toast.hidden = true; }, 7000);
}

function clearError() {
  clearTimeout(state.toastTimer);
  $("toast").hidden = true;
}

async function api(path, options = {}) {
  const init = { method: options.method || "GET" };
  if (options.body !== undefined) {
    init.headers = { "Content-Type": "application/json" };
    init.body = JSON.stringify(options.body);
  }
  let response;
  try {
    response = await fetch(path, init);
  } catch {
    throw new Error("Nem érhető el a szerver.");
  }
  if (response.status === 204) return null;
  const data = await response.json().catch(() => null);
  if (!response.ok) throw new Error(data?.error || "Váratlan szerverhiba.");
  return data;
}

// Runs a UI action and reports its failure to the user.
function guarded(action) {
  return async (event) => {
    event?.preventDefault?.();
    try {
      await action(event);
    } catch (error) {
      showError(error.message);
    }
  };
}

function periodQuery() {
  const month = $("month").value;
  return month ? `?month=${encodeURIComponent(month)}` : "";
}

async function loadSummary() {
  const data = await api("/api/summary");
  state.summary = data;
  const needed = data.compensation_max_minutes;
  $("open-hours").textContent = hours(data.open_weighted_minutes);
  $("total-compensated").textContent = hours(data.compensated_weighted_minutes);
  $("total-compensated-days").textContent =
    `${numberFormat.format(data.compensated_weighted_minutes / needed)} nap, szorzóval számolva`;
  $("total-overtime").textContent = hours(data.overtime_minutes);
  $("total-weighted").textContent = hours(data.weighted_minutes);
  $("total-entries").textContent = `összesen, ${data.entry_count} bejegyzés`;
  $("total-weighted-note").textContent =
    `ebből lecsúsztatva: ${hours(data.compensated_weighted_minutes)}`;

  const progress = Math.min(data.open_weighted_minutes / needed, 1);
  $("meter-fill").style.width = `${progress * 100}%`;
  $("meter").setAttribute("aria-valuenow", String(progress * COMPENSATION_HOURS));
  $("balance-note").textContent = data.available_days > 0
    ? `${data.available_days} teljes nap lecsúsztatható`
    : `Még ${hours(needed - data.open_weighted_minutes)} (szorzóval) egy teljes 8 órás naphoz`;
  $("open-compensate").disabled = data.open_weighted_minutes < data.compensation_step_minutes;
}

function chips(breakdown) {
  return el("div", { class: "chips" }, ...breakdown.map((part) =>
    el("span", { class: "chip", title: part.label }, `${numberFormat.format(part.percent)}% · ${shortHours(part.minutes)}`)));
}

function periodCell(entry) {
  const [startDay, startTime] = entry.start.split("T");
  const [endDay, endTime] = entry.end.split("T");
  const range = startDay === endDay
    ? `${startTime} – ${endTime}`
    : `${startTime} – ${formatDate(endDay)} ${endTime}`;
  return el("td", { class: "period" }, formatDate(startDay), el("small", {}, range));
}

function statusCell(entry) {
  const td = el("td", { class: "status" });
  if (entry.overtime_minutes === 0) {
    td.append("– Munkaidőben", el("small", {}, "nem számít túlórának"));
  } else if (entry.open_weighted_minutes <= 0) {
    td.append("✓ Lecsúsztatva");
  } else if (entry.compensated_weighted_minutes > 0) {
    td.append("◐ Részben lecsúsztatva",
      el("small", {}, `szorzóval ${shortHours(entry.open_weighted_minutes)} nyitott`));
  } else {
    td.append("○ Nyitott");
  }
  if (entry.overtime_minutes > 0 && entry.working_time_minutes > 0) {
    td.append(el("small", {}, `munkaidőben: ${shortHours(entry.working_time_minutes)}`));
  }
  return td;
}

function entryRow(entry) {
  return el("tr", {},
    el("td", { class: "ticket" }, entry.ticket),
    el("td", { class: "description" }, entry.description),
    periodCell(entry),
    el("td", { class: "num" }, shortHours(entry.overtime_minutes)),
    el("td", {}, chips(entry.breakdown)),
    el("td", { class: "num" }, shortHours(entry.weighted_minutes)),
    statusCell(entry),
    el("td", {}, el("div", { class: "actions" },
      el("button", { type: "button", class: "small", onclick: () => startEdit(entry) }, "Szerkesztés"),
      el("button", { type: "button", class: "small", onclick: guarded(() => deleteEntry(entry)) }, "Törlés"),
    )),
  );
}

async function loadEntries() {
  const params = new URLSearchParams();
  if ($("month").value) params.set("month", $("month").value);
  if ($("search").value.trim()) params.set("q", $("search").value.trim());
  const query = params.size ? `?${params}` : "";
  const entries = await api(`/api/entries${query}`);
  $("entries").replaceChildren(...entries.map(entryRow));
  $("entries-empty").hidden = entries.length > 0;
  $("export").href = `/api/export.csv${periodQuery()}`;
}

async function refresh() {
  await Promise.all([loadSummary(), loadEntries()]);
}

function resetForm() {
  state.editingId = null;
  $("entry-form").reset();
  setDefaultDates();
  $("form-title").textContent = "Új túlóra";
  $("submit-entry").textContent = "Hozzáadás";
  $("cancel-edit").hidden = true;
  $("preview").replaceChildren();
}

function startEdit(entry) {
  state.editingId = entry.id;
  $("ticket").value = entry.ticket;
  $("description").value = entry.description;
  writeDateTime("start", entry.start);
  writeDateTime("end", entry.end);
  $("form-title").textContent = `Szerkesztés: ${entry.ticket}`;
  $("submit-entry").textContent = "Mentés";
  $("cancel-edit").hidden = false;
  updatePreview();
  $("entry-form").scrollIntoView({ behavior: "smooth", block: "center" });
  $("ticket").focus({ preventScroll: true });
}

async function submitEntry() {
  const start = readDateTime("start");
  const body = {
    ticket: $("ticket").value,
    description: $("description").value,
    start,
    end: readDateTime("end"),
  };
  if (state.editingId === null) {
    await api("/api/entries", { method: "POST", body });
  } else {
    await api(`/api/entries/${state.editingId}`, { method: "PUT", body });
  }
  clearError();
  resetForm();
  // Keep the saved entry visible when it belongs to another month.
  if ($("month").value && $("month").value !== start.slice(0, 7)) {
    $("month").value = start.slice(0, 7);
  }
  await refresh();
}

async function deleteEntry(entry) {
  if (!confirm(`Biztosan törlöd ezt a bejegyzést: ${entry.ticket}?`)) return;
  await api(`/api/entries/${entry.id}`, { method: "DELETE" });
  if (state.editingId === entry.id) resetForm();
  await refresh();
}

async function updatePreview() {
  const token = ++state.previewToken;
  const preview = $("preview");
  const badTime = ["start-time", "end-time"].some((id) =>
    $(id).value.trim() && !normalizeTime($(id).value));
  if (badTime) {
    preview.replaceChildren("Hibás idő, 24 órás ÓÓ:PP formában add meg, például 17:30.");
    return;
  }
  const badDate = ["start-date", "end-date"].some((id) =>
    $(id).value.trim() && !normalizeDate($(id).value));
  if (badDate) {
    preview.replaceChildren("Hibás dátum, ÉÉÉÉ-HH-NN formában add meg, például 2026-11-10.");
    return;
  }
  const start = readDateTime("start");
  const end = readDateTime("end");
  if (!start || !end) {
    preview.replaceChildren();
    return;
  }
  try {
    const result = await api("/api/preview", { method: "POST", body: { start, end } });
    if (token !== state.previewToken) return;
    if (result.overtime_minutes === 0) {
      preview.replaceChildren("Ez az időszak teljes egészében munkaidő, nem számít túlórának.");
      return;
    }
    const strong = el("strong", {},
      `${hours(result.overtime_minutes)} túlóra, szorzóval ${hours(result.weighted_minutes)}`);
    const parts = [strong, chips(result.breakdown)];
    if (result.working_time_minutes > 0) {
      parts.push(`(${shortHours(result.working_time_minutes)} munkaidő nem számít)`);
    }
    preview.replaceChildren(...parts);
  } catch (error) {
    if (token === state.previewToken) preview.replaceChildren(error.message);
  }
}

function setTab(name) {
  for (const tab of ["entries", "compensations", "calendar", "backup"]) {
    const active = tab === name;
    $(`tab-${tab}`).setAttribute("aria-selected", String(active));
    $(`view-${tab}`).hidden = !active;
  }
}

async function undoCompensation(comp) {
  if (!confirm(`Visszavonod a(z) ${formatDate(comp.day)} napi lecsúsztatást?`)) return;
  await api(`/api/compensations/${comp.id}`, { method: "DELETE" });
  await Promise.all([loadCompensations(), refresh()]);
}

function compensationMarkdown(comp) {
  const plain = (minutes) => numberFormat.format(minutes / 60);
  const cell = (text) => text.replace(/\s*\n\s*/g, " ").replaceAll("|", "\\|");
  const dateTime = (value) => value.replace("T", " ");
  const row = (cells) => `| ${cells.join(" | ")} |`;
  const sum = (key) => comp.items.reduce((total, item) => total + item[key], 0);

  const lines = [
    `## Lecsúsztatás: ${formatDate(comp.day)}`,
    "",
    `- **Levont idő:** ${hours(comp.minutes)} (szorzóval számolva)`,
  ];
  if (comp.note) lines.push(`- **Megjegyzés:** ${comp.note}`);
  lines.push(
    "",
    row(["Hibajegy", "Leírás", "Kezdés", "Vége", "Ledolgozott óra", "Szorzók", "Szorzóval (óra)", "Levonva (óra)"]),
    row(["---", "---", "---", "---", "---:", "---", "---:", "---:"]),
    ...comp.items.map((item) => row([
      cell(item.ticket),
      cell(item.description),
      dateTime(item.start),
      dateTime(item.end),
      plain(item.overtime_minutes),
      item.breakdown.map((part) => `${numberFormat.format(part.percent)}%: ${plain(part.minutes)} óra`).join(", "),
      plain(item.weighted_minutes),
      plain(item.minutes),
    ])),
    row(["**Összesen**", "", "", "", plain(sum("overtime_minutes")), "", plain(sum("weighted_minutes")), plain(sum("minutes"))]),
  );
  return lines.join("\n");
}

// navigator.clipboard only exists on HTTPS and localhost, so plain HTTP needs the fallback.
async function copyText(text) {
  if (navigator.clipboard?.writeText) {
    try {
      await navigator.clipboard.writeText(text);
      return;
    } catch {
      // Fall through to the legacy path.
    }
  }
  const previousFocus = document.activeElement;
  const area = el("textarea", { class: "sr-only", readonly: "" });
  area.value = text;
  document.body.append(area);
  area.select();
  const copied = document.execCommand("copy");
  area.remove();
  previousFocus?.focus();
  if (!copied) throw new Error("A vágólapra másolás nem sikerült.");
}

async function copyCompensation(comp, button) {
  await copyText(compensationMarkdown(comp));
  clearError();
  const label = "Másolás Markdown formátumban";
  button.textContent = "✓ Másolva";
  setTimeout(() => { button.textContent = label; }, 2000);
}

async function loadCompensations() {
  const list = await api("/api/compensations");
  $("compensations-empty").hidden = list.length > 0;
  $("compensations").replaceChildren(...list.map((comp) => {
    const info = el("div", {},
      el("div", { class: "comp-day" }, `${formatDate(comp.day)} · ${hours(comp.minutes)} szorzóval`));
    if (comp.note) info.append(el("div", { class: "comp-note" }, comp.note));
    return el("article", { class: "card" },
      el("div", { class: "comp-head" }, info,
        el("div", { class: "actions" },
          el("button", {
            type: "button",
            class: "small",
            onclick: guarded((event) => copyCompensation(comp, event.currentTarget)),
          }, "Másolás Markdown formátumban"),
          el("button", { type: "button", class: "small", onclick: guarded(() => undoCompensation(comp)) }, "Visszavonás"),
        )),
      el("div", { class: "chips comp-items" }, ...comp.items.map((item) =>
        el("span", { class: "chip", title: formatDate(item.start) }, `${item.ticket} · ${shortHours(item.minutes)}`))),
    );
  }));
}

async function submitCompensation() {
  await api("/api/compensations", {
    method: "POST",
    body: {
      day: normalizeDate($("compensate-day").value) ?? "",
      hours: Number($("compensate-hours").value),
      note: $("compensate-note").value,
    },
  });
  clearError();
  $("compensate-dialog").close();
  await Promise.all([loadCompensations(), refresh()]);
}

// Offers every half-hour step the open balance covers, up to the daily limit.
function fillCompensationHours() {
  const { open_weighted_minutes: open, compensation_max_minutes: max, compensation_step_minutes: step } = state.summary;
  const count = Math.floor(Math.min(open, max) / step);
  const options = Array.from({ length: count }, (_, i) => {
    const value = ((i + 1) * step) / 60;
    return el("option", { value: String(value) }, `${numberFormat.format(value)} óra`);
  });
  $("compensate-hours").replaceChildren(...options);
  $("compensate-hours").value = String((count * step) / 60);
}

function openCompensationDialog() {
  $("compensate-form").reset();
  $("compensate-day").value = today();
  $("compensate-error").hidden = true;
  fillCompensationHours();
  $("compensate-dialog").showModal();
}

async function deleteOverride(day) {
  await api(`/api/calendar/${day}`, { method: "DELETE" });
  await Promise.all([loadCalendar(), refresh()]);
}

async function loadCalendar() {
  const year = $("calendar-year").value;
  const data = await api(`/api/calendar?year=${encodeURIComponent(year)}`);
  $("overrides-empty").hidden = data.overrides.length > 0;
  $("overrides").replaceChildren(...data.overrides.map((item) =>
    el("li", {},
      el("span", { class: "date" }, formatDate(item.day)),
      el("span", { class: "chip" }, item.kind === "rest" ? "Pihenőnap" : "Munkanap"),
      el("span", { class: "grow" }, item.note),
      el("button", { type: "button", class: "small", onclick: guarded(() => deleteOverride(item.day)) }, "Törlés"),
    )));
  $("holidays").replaceChildren(...data.holidays.map((item) =>
    el("li", {},
      el("span", { class: "date" }, formatDate(item.day)),
      el("span", { class: "grow" }, item.name),
    )));
}

async function submitCalendar() {
  const day = normalizeDate($("calendar-day").value);
  if (!day) throw new Error("Adj meg egy érvényes napot ÉÉÉÉ-HH-NN formában.");
  await api(`/api/calendar/${day}`, {
    method: "PUT",
    body: { kind: $("calendar-kind").value, note: $("calendar-note").value },
  });
  clearError();
  $("calendar-form").reset();
  $("calendar-year").value = day.slice(0, 4);
  await Promise.all([loadCalendar(), refresh()]);
}

async function restoreBackup() {
  const file = $("backup-file").files[0];
  if (!file) throw new Error("Válassz egy mentésfájlt.");
  let data;
  try {
    data = JSON.parse(await file.text());
  } catch {
    throw new Error("A fájl nem érvényes JSON mentés.");
  }
  if (!confirm("A visszaállítás az összes jelenlegi adatot lecseréli a mentés tartalmára. Folytatod?")) return;
  const result = await api("/api/backup", { method: "POST", body: data });
  clearError();
  $("restore-form").reset();
  resetForm();
  $("restore-result").textContent =
    `Visszaállítva: ${result.entries} bejegyzés, ${result.compensations} lecsúsztatás, `
    + `${result.calendar_overrides} egyedi nap. Az előző állapot mentése: data/backups/${result.snapshot}`;
  $("restore-result").hidden = false;
  await refresh();
}

function debounce(fn, delay) {
  let timer;
  return () => {
    clearTimeout(timer);
    timer = setTimeout(fn, delay);
  };
}

async function loadConfig() {
  const { rates } = await api("/api/config");
  const percent = (value) => `${numberFormat.format(value)}%`;
  $("kind-rest").textContent = `Pihenőnap (${percent(rates.restday)} / ${percent(rates.restday_night)})`;
  $("kind-work").textContent = `Munkanap (${percent(rates.evening)} / ${percent(rates.night)})`;
}

function init() {
  document.querySelectorAll("[data-picker]").forEach(attachPicker);
  $("month").value = today().slice(0, 7);
  $("calendar-year").value = today().slice(0, 4);
  setDefaultDates();

  $("entry-form").addEventListener("submit", guarded(submitEntry));
  $("cancel-edit").addEventListener("click", resetForm);
  for (const field of ["start", "end"]) {
    const time = $(`${field}-time`);
    time.addEventListener("change", () => {
      time.value = normalizeTime(time.value) ?? time.value;
      updatePreview();
    });
    $(`${field}-date`).addEventListener("change", () => {
      const startDay = normalizeDate($("start-date").value);
      if (field === "start" && startDay) $("end-date").value = startDay;
      updatePreview();
    });
  }
  for (const button of document.querySelectorAll("[data-minutes]")) {
    button.addEventListener("click", () => setEndAfterStart(Number(button.dataset.minutes)));
  }

  $("month").addEventListener("change", guarded(loadEntries));
  $("all-months").addEventListener("click", guarded(() => {
    $("month").value = "";
    return loadEntries();
  }));
  const reloadEntries = guarded(loadEntries);
  $("search").addEventListener("input", debounce(reloadEntries, 200));

  $("tab-entries").addEventListener("click", () => setTab("entries"));
  $("tab-compensations").addEventListener("click", guarded(async () => {
    setTab("compensations");
    await loadCompensations();
  }));
  $("tab-calendar").addEventListener("click", guarded(async () => {
    setTab("calendar");
    await loadCalendar();
  }));

  $("tab-backup").addEventListener("click", () => {
    setTab("backup");
    $("restore-result").hidden = true;
  });
  $("restore-form").addEventListener("submit", guarded(restoreBackup));

  $("open-compensate").addEventListener("click", openCompensationDialog);
  $("compensate-cancel").addEventListener("click", () => $("compensate-dialog").close());
  $("compensate-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    try {
      await submitCompensation();
    } catch (error) {
      // A toast would sit behind the modal dialog.
      $("compensate-error").textContent = error.message;
      $("compensate-error").hidden = false;
    }
  });

  $("calendar-form").addEventListener("submit", guarded(submitCalendar));
  $("calendar-year").addEventListener("change", guarded(loadCalendar));

  guarded(loadConfig)();
  guarded(refresh)();
}

init();
