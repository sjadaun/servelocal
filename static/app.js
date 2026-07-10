const API = "/api/v1";

const CATEGORY_LABELS = {
  breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner",
  snack: "Snack", other: "Other",
};
const CATEGORY_COLOR_VAR = {
  breakfast: "--cat-breakfast", lunch: "--cat-lunch", dinner: "--cat-dinner",
  snack: "--cat-snack", other: "--cat-other",
};
const GOING_OUT_COLOR_VAR = "--going-out";
const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
const MONTH_NAMES = ["January","February","March","April","May","June","July",
  "August","September","October","November","December"];

function pad2(n) { return String(n).padStart(2, "0"); }
function todayIso() {
  const d = new Date();
  return `${d.getFullYear()}-${pad2(d.getMonth()+1)}-${pad2(d.getDate())}`;
}
function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}
function colorVarFor(item) {
  return item.going_out ? GOING_OUT_COLOR_VAR : (CATEGORY_COLOR_VAR[item.category] || "--cat-other");
}

// ---------------------------------------------------------------- API -----

async function apiCall(path, options = {}) {
  const res = await fetch(API + path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await res.json();
  if (!body.success) throw new Error(body.error || "Request failed");
  return body.data;
}

// -------------------------------------------------------------- theme -----
// The theme is ONE setting shared with the physical display (see
// database.py / app.py's /api/v1/theme) -- not a per-browser preference.
// "auto" resolves server-side from the Pi's clock, same as the display.

const themeToggleBtn = document.getElementById("theme-toggle");
const themeMenu = document.getElementById("theme-menu");

function applyTheme(mode, resolved) {
  document.documentElement.setAttribute("data-theme", resolved);
  document.documentElement.setAttribute("data-theme-mode", mode);
  themeMenu.querySelectorAll(".theme-option").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.mode === mode));
  localStorage.setItem("themeCache", JSON.stringify({ mode, resolved }));
}

// paint instantly from the last known value (or a light-mode guess) so
// there's no flash while the real value loads from the server
(function paintThemeFromCache() {
  const cached = JSON.parse(localStorage.getItem("themeCache") || "null");
  if (cached) {
    applyTheme(cached.mode, cached.resolved);
  } else {
    const guess = window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
    applyTheme("auto", guess);
  }
})();

async function loadTheme() {
  try {
    const data = await apiCall("/theme");
    applyTheme(data.mode, data.resolved);
  } catch (e) { /* keep the cached/guessed value */ }
}
loadTheme();

themeToggleBtn.addEventListener("click", (e) => {
  e.stopPropagation();
  themeMenu.classList.toggle("hidden");
});
document.addEventListener("click", (e) => {
  if (!themeMenu.contains(e.target) && e.target !== themeToggleBtn) {
    themeMenu.classList.add("hidden");
  }
});
themeMenu.querySelectorAll(".theme-option").forEach(btn => {
  btn.addEventListener("click", async () => {
    const mode = btn.dataset.mode;
    themeMenu.classList.add("hidden");
    try {
      const data = await apiCall("/theme", { method: "PUT", body: JSON.stringify({ mode }) });
      applyTheme(data.mode, data.resolved);
    } catch (err) {
      alert(err.message);
    }
  });
});

// --------------------------------------------------------------- tabs -----

function showTab(name) {
  document.querySelectorAll(".tab-panel").forEach(el =>
    el.classList.toggle("hidden", el.id !== `tab-${name}`));
  document.querySelectorAll(".tab-btn").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.tab === name));
  if (name === "next") loadNext();
  if (name === "all") loadAllMeals();
  if (name === "calendar") { loadCalendarMonth(); loadDayPanel(calSelectedDate); }
}
document.querySelectorAll(".tab-btn").forEach(btn =>
  btn.addEventListener("click", () => showTab(btn.dataset.tab)));

document.getElementById("add-from-list").addEventListener("click", () => {
  resetForm();
  showTab("add");
});

// ---------------------------------------------------------- next tab -----

function formatCountdown(whenIso) {
  const now = new Date();
  const when = new Date(whenIso);
  let mins = Math.round((when - now) / 60000);
  if (mins <= 0) return "now";
  const h = Math.floor(mins / 60), m = mins % 60;
  return h > 0 ? `in ${h}h ${m}m` : `in ${m}m`;
}

function renderOccurrenceDetails(meal) {
  const colorVar = colorVarFor(meal);
  let html = `
    <span class="cat-badge" style="background:var(${colorVar})">${meal.going_out ? "Eating Out" : (CATEGORY_LABELS[meal.category] || meal.category)}</span>
    <div class="meal-name">${escapeHtml(meal.name)}</div>
    <div class="meal-time">${meal.going_out ? "There by" : "Ready by"} ${meal.scheduled_time}</div>
    <div class="countdown">${formatCountdown(meal.when)}</div>
  `;
  if (meal.going_out && meal.going_out_place) {
    html += `<div class="detail-row"><span class="label">Where</span> ${escapeHtml(meal.going_out_place)}</div>`;
  }
  if (meal.prep_minutes) {
    const startBy = new Date(new Date(meal.when).getTime() - meal.prep_minutes * 60000);
    const hh = pad2(startBy.getHours()), mm = pad2(startBy.getMinutes());
    html += `<div class="detail-row"><span class="label">${meal.going_out ? "Leave by" : "Start prep"}</span> ${hh}:${mm}</div>`;
  }
  if (meal.notes) {
    html += `<div class="detail-row"><span class="label">Notes</span> ${escapeHtml(meal.notes)}</div>`;
  }
  const dateStr = meal.when.slice(0, 10);
  html += `<button type="button" id="mark-done-btn" class="btn btn-primary mark-done-btn"
             data-slot-id="${meal.slot_id}" data-date="${dateStr}">✓ Mark as done</button>`;
  return html;
}

let markDoneUndoTimer = null;

function wireMarkDoneButton() {
  const btn = document.getElementById("mark-done-btn");
  if (!btn) return;
  btn.addEventListener("click", async () => {
    const slotId = btn.dataset.slotId;
    const dateStr = btn.dataset.date;
    btn.disabled = true;
    btn.textContent = "Marking…";
    try {
      await apiCall(`/meals/${slotId}/occurrences/${dateStr}/complete`, {
        method: "POST", body: JSON.stringify({ completed: true }),
      });
      showMarkedDoneState(slotId, dateStr);
      loadTodayList(); // reflect it in today's list right away
    } catch (err) {
      btn.disabled = false;
      btn.textContent = "✓ Mark as done";
      alert(err.message);
    }
  });
}

function showMarkedDoneState(slotId, dateStr) {
  const card = document.getElementById("next-card");
  card.innerHTML = `
    <div class="empty done-confirm">
      ✅ Marked as done
      <button type="button" id="undo-done-btn" class="btn btn-small">Undo</button>
    </div>
  `;
  document.getElementById("undo-done-btn").addEventListener("click", async () => {
    clearTimeout(markDoneUndoTimer);
    try {
      await apiCall(`/meals/${slotId}/occurrences/${dateStr}/complete`, {
        method: "POST", body: JSON.stringify({ completed: false }),
      });
    } catch (err) { /* fall through to reload regardless */ }
    loadNext();
  });
  markDoneUndoTimer = setTimeout(loadNext, 20000);
}

async function loadNext() {
  const card = document.getElementById("next-card");
  try {
    const meal = await apiCall("/next");
    card.innerHTML = meal
      ? renderOccurrenceDetails(meal)
      : `<div class="empty">No meals scheduled yet.<br>Add one from the "Add" tab.</div>`;
    wireMarkDoneButton();
  } catch (e) {
    card.innerHTML = `<div class="empty">Couldn't load next meal.</div>`;
  }
  loadTodayList();
}

async function loadTodayList() {
  try {
    const today = await apiCall("/today");
    const list = document.getElementById("today-list");
    list.innerHTML = "";
    if (today.length === 0) {
      list.innerHTML = `<li class="muted">Nothing scheduled today.</li>`;
    }
    for (const item of today) {
      const li = document.createElement("li");
      li.className = "today-item" + (item.done ? " done" : "");
      li.innerHTML = `
        <span class="dot" style="background:var(${colorVarFor(item)})"></span>
        <div class="item-main">
          <div class="item-title">${item.completed ? "✓ " : ""}${escapeHtml(item.name)}${item.going_out ? '<span class="badge-going-out">Out</span>' : ""}</div>
          <div class="item-sub">${item.going_out ? escapeHtml(item.going_out_place || "Eating out") : (CATEGORY_LABELS[item.category] || item.category)}</div>
        </div>
        <div class="item-time">${item.scheduled_time}</div>
      `;
      list.appendChild(li);
    }
  } catch (e) { /* non-fatal */ }
}

// ------------------------------------------------------- all meals tab ----

async function loadAllMeals() {
  const list = document.getElementById("meal-list");
  list.innerHTML = `<li class="muted">Loading…</li>`;
  try {
    const meals = await apiCall("/meals");
    list.innerHTML = "";
    if (meals.length === 0) {
      list.innerHTML = `<li class="muted">No meals yet. Tap "+ New" to add one.</li>`;
      return;
    }
    for (const m of meals) {
      const repeatLabel = m.repeat_type === "weekly"
        ? m.repeat_days.map(d => WEEKDAY_LABELS[d]).join(", ")
        : m.repeat_type === "daily" ? "Daily"
        : m.repeat_type === "monthly" ? `Monthly (day ${m.start_date.split("-")[2]})`
        : "Once";
      const li = document.createElement("li");
      li.className = "meal-item";
      li.innerHTML = `
        <span class="dot" style="background:var(${colorVarFor(m)})"></span>
        <div class="item-main">
          <div class="item-title">${escapeHtml(m.name)}${m.going_out ? '<span class="badge-going-out">Out</span>' : ""}</div>
          <div class="item-sub">${m.scheduled_time} · ${repeatLabel}${m.going_out && m.going_out_place ? " · " + escapeHtml(m.going_out_place) : ""}</div>
        </div>
        <div class="item-actions">
          <button class="icon-action edit-btn" data-id="${m.id}" title="Edit">✏️</button>
          <button class="icon-action danger del-btn" data-id="${m.id}" title="Delete">🗑️</button>
        </div>
      `;
      list.appendChild(li);
    }
    list.querySelectorAll(".edit-btn").forEach(btn =>
      btn.addEventListener("click", () => editMeal(btn.dataset.id, meals)));
    list.querySelectorAll(".del-btn").forEach(btn =>
      btn.addEventListener("click", () => deleteMeal(btn.dataset.id)));
  } catch (e) {
    list.innerHTML = `<li class="muted">Couldn't load meals.</li>`;
  }
}

async function deleteMeal(id) {
  if (!confirm("Delete this meal (and its entire recurring series)?")) return;
  await apiCall(`/meals/${id}`, { method: "DELETE" });
  loadAllMeals();
}

// ------------------------------------------------------------ calendar ----

let calYear, calMonth0; // month0 is 0-11
let calSelectedDate = todayIso();
let calCounts = {};

(function initCalState() {
  const d = new Date();
  calYear = d.getFullYear();
  calMonth0 = d.getMonth();
})();

document.getElementById("cal-prev").addEventListener("click", () => {
  calMonth0--; if (calMonth0 < 0) { calMonth0 = 11; calYear--; }
  loadCalendarMonth();
});
document.getElementById("cal-next").addEventListener("click", () => {
  calMonth0++; if (calMonth0 > 11) { calMonth0 = 0; calYear++; }
  loadCalendarMonth();
});
document.getElementById("add-for-day").addEventListener("click", () => {
  resetForm();
  document.getElementById("start_date").value = calSelectedDate;
  showTab("add");
});

async function loadCalendarMonth() {
  document.getElementById("cal-month-label").textContent = `${MONTH_NAMES[calMonth0]} ${calYear}`;
  try {
    calCounts = await apiCall(`/calendar?year=${calYear}&month=${calMonth0 + 1}`);
  } catch (e) {
    calCounts = {};
  }
  renderCalGrid();
}

function renderCalGrid() {
  const grid = document.getElementById("cal-grid");
  grid.innerHTML = "";
  const firstDow = (new Date(calYear, calMonth0, 1).getDay() + 6) % 7; // Monday=0
  const daysInMonth = new Date(calYear, calMonth0 + 1, 0).getDate();
  const today = todayIso();

  for (let i = 0; i < firstDow; i++) {
    const blank = document.createElement("div");
    blank.className = "cal-day empty";
    grid.appendChild(blank);
  }
  for (let day = 1; day <= daysInMonth; day++) {
    const dateStr = `${calYear}-${pad2(calMonth0 + 1)}-${pad2(day)}`;
    const cell = document.createElement("div");
    cell.className = "cal-day";
    if (dateStr === today) cell.classList.add("today");
    if (dateStr === calSelectedDate) cell.classList.add("selected");
    cell.innerHTML = `${day}${calCounts[dateStr] ? '<span class="cal-dot"></span>' : ""}`;
    cell.addEventListener("click", () => {
      calSelectedDate = dateStr;
      renderCalGrid();
      loadDayPanel(dateStr);
    });
    grid.appendChild(cell);
  }
}

function formatDayTitle(dateStr) {
  const d = new Date(dateStr + "T00:00:00");
  return d.toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" });
}

async function loadDayPanel(dateStr) {
  document.getElementById("day-panel-title").textContent = formatDayTitle(dateStr);
  const list = document.getElementById("day-occurrence-list");
  list.innerHTML = `<li class="muted">Loading…</li>`;
  try {
    const occs = await apiCall(`/calendar/${dateStr}`);
    list.innerHTML = "";
    if (occs.length === 0) {
      list.innerHTML = `<li class="muted">Nothing planned. Tap "+ New" to add something.</li>`;
      return;
    }
    for (const occ of occs) {
      list.appendChild(buildOccurrenceItem(occ, dateStr));
    }
  } catch (e) {
    list.innerHTML = `<li class="muted">Couldn't load this day.</li>`;
  }
}

function buildOccurrenceItem(occ, dateStr) {
  const li = document.createElement("li");
  li.className = "meal-item" + (occ.cancelled ? " cancelled" : "");
  const repeatTag = occ.cancelled ? '<span class="badge-edited">Skipped</span>'
    : occ.has_override ? '<span class="badge-edited">Edited</span>' : "";
  li.innerHTML = `
    <span class="dot" style="background:var(${colorVarFor(occ)})"></span>
    <div class="item-main">
      <div class="item-title">${escapeHtml(occ.name)}${occ.going_out ? '<span class="badge-going-out">Out</span>' : ""}${repeatTag}</div>
      <div class="item-sub">${occ.scheduled_time} · ${occ.going_out ? escapeHtml(occ.going_out_place || "Eating out") : (CATEGORY_LABELS[occ.category] || occ.category)}</div>
    </div>
    <div class="item-actions"></div>
  `;
  const actions = li.querySelector(".item-actions");

  if (occ.cancelled) {
    const restoreBtn = document.createElement("button");
    restoreBtn.className = "icon-action";
    restoreBtn.title = "Restore";
    restoreBtn.textContent = "↺";
    restoreBtn.addEventListener("click", () => clearOccurrence(occ.slot_id, dateStr));
    actions.appendChild(restoreBtn);
  } else {
    const editBtn = document.createElement("button");
    editBtn.className = "icon-action";
    editBtn.title = "Edit just this day";
    editBtn.textContent = "✏️";
    editBtn.addEventListener("click", () => toggleOccurrenceEditor(li, occ, dateStr));
    actions.appendChild(editBtn);

    const skipBtn = document.createElement("button");
    skipBtn.className = "icon-action danger";
    skipBtn.title = "Skip this day";
    skipBtn.textContent = "⤫";
    skipBtn.addEventListener("click", () => skipOccurrence(occ.slot_id, dateStr));
    actions.appendChild(skipBtn);

    if (occ.has_override) {
      const revertBtn = document.createElement("button");
      revertBtn.className = "icon-action";
      revertBtn.title = "Revert to series default";
      revertBtn.textContent = "↺";
      revertBtn.addEventListener("click", () => clearOccurrence(occ.slot_id, dateStr));
      actions.appendChild(revertBtn);
    }
  }
  return li;
}

async function skipOccurrence(slotId, dateStr) {
  if (!confirm("Skip this one occurrence? The rest of the series is unaffected.")) return;
  await apiCall(`/meals/${slotId}/occurrences/${dateStr}`, {
    method: "PUT", body: JSON.stringify({ cancelled: true }),
  });
  loadDayPanel(dateStr);
  loadCalendarMonth();
}

async function clearOccurrence(slotId, dateStr) {
  await apiCall(`/meals/${slotId}/occurrences/${dateStr}`, { method: "DELETE" });
  loadDayPanel(dateStr);
  loadCalendarMonth();
}

function toggleOccurrenceEditor(itemLi, occ, dateStr) {
  const existing = itemLi.nextElementSibling;
  if (existing && existing.classList.contains("occurrence-editor")) {
    existing.remove();
    return;
  }
  document.querySelectorAll(".occurrence-editor").forEach(el => el.remove());

  const tpl = document.getElementById("occurrence-editor-template");
  const node = tpl.content.firstElementChild.cloneNode(true);

  // category chips
  const catGroup = node.querySelector(".oe-category");
  Object.entries(CATEGORY_LABELS).forEach(([id, label]) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = "chip" + (id === occ.category ? " active" : "");
    btn.dataset.category = id;
    btn.textContent = label;
    btn.addEventListener("click", () => {
      catGroup.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
      btn.classList.add("active");
    });
    catGroup.appendChild(btn);
  });

  const goingOutCb = node.querySelector(".oe-going-out");
  const placeWrap = node.querySelector(".oe-place-wrap");
  const placeInput = node.querySelector(".oe-place");
  const timeLabel = node.querySelector(".oe-time-label .label-text");
  const prepLabel = node.querySelector(".oe-prep-label .label-text");
  const timeInput = node.querySelector(".oe-time");
  const prepInput = node.querySelector(".oe-prep");
  const notesInput = node.querySelector(".oe-notes");

  function syncGoingOut() {
    const isOut = goingOutCb.checked;
    placeWrap.classList.toggle("hidden", !isOut);
    timeLabel.textContent = isOut ? "There by" : "Ready by";
    prepLabel.textContent = isOut ? "Leave (min before)" : "Prep (min before)";
  }
  goingOutCb.checked = occ.going_out;
  goingOutCb.addEventListener("change", syncGoingOut);
  syncGoingOut();

  placeInput.value = occ.going_out_place || "";
  timeInput.value = occ.scheduled_time;
  prepInput.value = occ.prep_minutes;
  notesInput.value = occ.notes || "";

  node.querySelector(".oe-cancel").addEventListener("click", () => node.remove());
  node.querySelector(".oe-save").addEventListener("click", async () => {
    const payload = {
      category: catGroup.querySelector(".chip.active")?.dataset.category || occ.category,
      going_out: goingOutCb.checked,
      going_out_place: placeInput.value,
      scheduled_time: timeInput.value,
      prep_minutes: parseInt(prepInput.value || 0),
      notes: notesInput.value,
    };
    try {
      await apiCall(`/meals/${occ.slot_id}/occurrences/${dateStr}`, {
        method: "PUT", body: JSON.stringify(payload),
      });
      loadDayPanel(dateStr);
      loadCalendarMonth();
    } catch (err) {
      alert(err.message);
    }
  });

  itemLi.insertAdjacentElement("afterend", node);
}

// ------------------------------------------------------------- form -----

const form = document.getElementById("meal-form");
const weekdayPicker = document.getElementById("weekday-picker");
const cancelBtn = document.getElementById("cancel-btn");
const formTitle = document.getElementById("form-title");
const categoryChips = document.getElementById("category-chips");
const categoryInput = document.getElementById("category");
const repeatChips = document.getElementById("repeat-chips");
const repeatInput = document.getElementById("repeat_type");
const goingOutCheckbox = document.getElementById("going_out");
const goingOutPlaceWrap = document.getElementById("going-out-place-wrap");
const goingOutPlaceInput = document.getElementById("going_out_place");
const timeLabelText = document.querySelector("#time-label .label-text");
const prepLabelText = document.querySelector("#prep-label .label-text");

Object.entries(CATEGORY_LABELS).forEach(([id, label]) => {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "chip" + (id === "breakfast" ? " active" : "");
  btn.dataset.category = id;
  btn.textContent = label;
  btn.addEventListener("click", () => {
    categoryChips.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    categoryInput.value = id;
  });
  categoryChips.appendChild(btn);
});

const monthlyHint = document.getElementById("monthly-hint");
const startDateInput = document.getElementById("start_date");

function ordinal(n) {
  const s = ["th", "st", "nd", "rd"], v = n % 100;
  return n + (s[(v - 20) % 10] || s[v] || s[0]);
}
function updateMonthlyHint() {
  if (repeatInput.value !== "monthly" || !startDateInput.value) {
    monthlyHint.classList.add("hidden");
    return;
  }
  const day = parseInt(startDateInput.value.split("-")[2]);
  monthlyHint.textContent = `Repeats on the ${ordinal(day)} of every month (shifted to the last day in shorter months).`;
  monthlyHint.classList.remove("hidden");
}
startDateInput.addEventListener("change", updateMonthlyHint);

repeatChips.querySelectorAll(".chip").forEach(btn => {
  btn.addEventListener("click", () => {
    repeatChips.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    repeatInput.value = btn.dataset.repeat;
    weekdayPicker.classList.toggle("hidden", btn.dataset.repeat !== "weekly");
    updateMonthlyHint();
  });
});

function syncGoingOutForm() {
  const isOut = goingOutCheckbox.checked;
  goingOutPlaceWrap.classList.toggle("hidden", !isOut);
  goingOutPlaceInput.required = isOut;
  timeLabelText.textContent = isOut ? "There by" : "Ready by";
  prepLabelText.textContent = isOut ? "Leave (min before)" : "Prep (min before)";
}
goingOutCheckbox.addEventListener("change", syncGoingOutForm);

document.getElementById("start_date").valueAsDate = new Date();

function resetForm() {
  form.reset();
  document.getElementById("meal-id").value = "";
  weekdayPicker.classList.add("hidden");
  weekdayPicker.querySelectorAll("input").forEach(cb => cb.checked = false);
  document.getElementById("start_date").valueAsDate = new Date();
  cancelBtn.classList.add("hidden");
  formTitle.textContent = "Add a meal";

  categoryChips.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
  categoryChips.querySelector('[data-category="breakfast"]').classList.add("active");
  categoryInput.value = "breakfast";

  repeatChips.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
  repeatChips.querySelector('[data-repeat="once"]').classList.add("active");
  repeatInput.value = "once";
  updateMonthlyHint();

  goingOutCheckbox.checked = false;
  syncGoingOutForm();
}

cancelBtn.addEventListener("click", () => { resetForm(); showTab("all"); });

form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const id = document.getElementById("meal-id").value;
  const repeat_days = [...weekdayPicker.querySelectorAll("input:checked")]
    .map(cb => parseInt(cb.value));

  const payload = {
    name: document.getElementById("name").value,
    category: categoryInput.value,
    scheduled_time: document.getElementById("scheduled_time").value,
    prep_minutes: parseInt(document.getElementById("prep_minutes").value || 0),
    going_out: goingOutCheckbox.checked,
    going_out_place: goingOutPlaceInput.value,
    notes: document.getElementById("notes").value,
    repeat_type: repeatInput.value,
    repeat_days,
    start_date: document.getElementById("start_date").value,
    end_date: document.getElementById("end_date").value,
  };

  try {
    if (id) {
      await apiCall(`/meals/${id}`, { method: "PUT", body: JSON.stringify(payload) });
    } else {
      await apiCall("/meals", { method: "POST", body: JSON.stringify(payload) });
    }
    resetForm();
    showTab("all");
  } catch (err) {
    alert(err.message);
  }
});

function editMeal(id, meals) {
  const m = meals.find(x => String(x.id) === String(id));
  if (!m) return;
  document.getElementById("meal-id").value = m.id;
  document.getElementById("name").value = m.name;
  document.getElementById("scheduled_time").value = m.scheduled_time;
  document.getElementById("prep_minutes").value = m.prep_minutes;
  document.getElementById("notes").value = m.notes;
  document.getElementById("start_date").value = m.start_date;
  document.getElementById("end_date").value = m.end_date || "";

  goingOutCheckbox.checked = m.going_out;
  goingOutPlaceInput.value = m.going_out_place || "";
  syncGoingOutForm();

  categoryChips.querySelectorAll(".chip").forEach(c =>
    c.classList.toggle("active", c.dataset.category === m.category));
  categoryInput.value = m.category;

  repeatChips.querySelectorAll(".chip").forEach(c =>
    c.classList.toggle("active", c.dataset.repeat === m.repeat_type));
  repeatInput.value = m.repeat_type;
  weekdayPicker.classList.toggle("hidden", m.repeat_type !== "weekly");
  weekdayPicker.querySelectorAll("input").forEach(cb => {
    cb.checked = m.repeat_days.includes(parseInt(cb.value));
  });
  updateMonthlyHint();

  formTitle.textContent = "Edit meal (entire series)";
  cancelBtn.classList.remove("hidden");
  showTab("add");
}

// ------------------------------------------------------------- init -----

loadNext();
setInterval(() => {
  if (!document.getElementById("tab-next").classList.contains("hidden")) loadNext();
}, 30000);
