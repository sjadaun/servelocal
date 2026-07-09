const API = "/api/v1";

const CATEGORY_LABELS = {
  breakfast: "Breakfast", lunch: "Lunch", dinner: "Dinner",
  school: "School", snack: "Snack", other: "Other",
};
const CATEGORY_COLOR_VAR = {
  breakfast: "--cat-breakfast", lunch: "--cat-lunch", dinner: "--cat-dinner",
  school: "--cat-school", snack: "--cat-snack", other: "--cat-other",
};
const WEEKDAY_LABELS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

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

function initTheme() {
  const saved = localStorage.getItem("theme") ||
    (window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.setAttribute("data-theme", saved);
}

document.getElementById("theme-toggle").addEventListener("click", () => {
  const current = document.documentElement.getAttribute("data-theme");
  const next = current === "dark" ? "light" : "dark";
  document.documentElement.setAttribute("data-theme", next);
  localStorage.setItem("theme", next);
});

initTheme();

// --------------------------------------------------------------- tabs -----

function showTab(name) {
  document.querySelectorAll(".tab-panel").forEach(el =>
    el.classList.toggle("hidden", el.id !== `tab-${name}`));
  document.querySelectorAll(".tab-btn").forEach(btn =>
    btn.classList.toggle("active", btn.dataset.tab === name));
  if (name === "next") loadNext();
  if (name === "all") loadAllMeals();
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

async function loadNext() {
  const card = document.getElementById("next-card");
  try {
    const meal = await apiCall("/next");
    if (!meal) {
      card.innerHTML = `<div class="empty">No meals scheduled yet.<br>Add one from the "Add" tab.</div>`;
    } else {
      const colorVar = CATEGORY_COLOR_VAR[meal.category] || "--cat-other";
      let html = `
        <span class="cat-badge" style="background:var(${colorVar})">${CATEGORY_LABELS[meal.category] || meal.category}</span>
        <div class="meal-name">${escapeHtml(meal.name)}</div>
        <div class="meal-time">Ready by ${meal.scheduled_time}</div>
        <div class="countdown">${formatCountdown(meal.when)}</div>
      `;
      if (meal.prep_minutes) {
        const startBy = new Date(new Date(meal.when).getTime() - meal.prep_minutes * 60000);
        const hh = String(startBy.getHours()).padStart(2, "0");
        const mm = String(startBy.getMinutes()).padStart(2, "0");
        html += `<div class="detail-row"><span class="label">Start prep</span> by ${hh}:${mm}</div>`;
      }
      if (meal.temperature) {
        html += `<div class="detail-row"><span class="label">Temperature</span> ${escapeHtml(meal.temperature)}</div>`;
      }
      if (meal.notes) {
        html += `<div class="detail-row"><span class="label">Notes</span> ${escapeHtml(meal.notes)}</div>`;
      }
      card.innerHTML = html;
    }
  } catch (e) {
    card.innerHTML = `<div class="empty">Couldn't load next meal.</div>`;
  }

  try {
    const today = await apiCall("/today");
    const list = document.getElementById("today-list");
    list.innerHTML = "";
    if (today.length === 0) {
      list.innerHTML = `<li class="muted">Nothing scheduled today.</li>`;
    }
    for (const item of today) {
      const colorVar = CATEGORY_COLOR_VAR[item.category] || "--cat-other";
      const li = document.createElement("li");
      li.className = "today-item" + (item.done ? " done" : "");
      li.innerHTML = `
        <span class="dot" style="background:var(${colorVar})"></span>
        <div class="item-main">
          <div class="item-title">${escapeHtml(item.name)}</div>
          <div class="item-sub">${CATEGORY_LABELS[item.category] || item.category}</div>
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
      const colorVar = CATEGORY_COLOR_VAR[m.category] || "--cat-other";
      const repeatLabel = m.repeat_type === "weekly"
        ? m.repeat_days.map(d => WEEKDAY_LABELS[d]).join(", ")
        : m.repeat_type === "daily" ? "Daily" : "Once";
      const li = document.createElement("li");
      li.className = "meal-item";
      li.innerHTML = `
        <span class="dot" style="background:var(${colorVar})"></span>
        <div class="item-main">
          <div class="item-title">${escapeHtml(m.name)}</div>
          <div class="item-sub">${m.scheduled_time} · ${repeatLabel}</div>
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
  if (!confirm("Delete this meal?")) return;
  await apiCall(`/meals/${id}`, { method: "DELETE" });
  loadAllMeals();
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

repeatChips.querySelectorAll(".chip").forEach(btn => {
  btn.addEventListener("click", () => {
    repeatChips.querySelectorAll(".chip").forEach(c => c.classList.remove("active"));
    btn.classList.add("active");
    repeatInput.value = btn.dataset.repeat;
    weekdayPicker.classList.toggle("hidden", btn.dataset.repeat !== "weekly");
  });
});

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
    temperature: document.getElementById("temperature").value,
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
  document.getElementById("temperature").value = m.temperature;
  document.getElementById("notes").value = m.notes;
  document.getElementById("start_date").value = m.start_date;
  document.getElementById("end_date").value = m.end_date || "";

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

  formTitle.textContent = "Edit meal";
  cancelBtn.classList.remove("hidden");
  showTab("add");
}

function escapeHtml(str) {
  const div = document.createElement("div");
  div.textContent = str ?? "";
  return div.innerHTML;
}

// ------------------------------------------------------------- init -----

loadNext();
setInterval(() => {
  if (!document.getElementById("tab-next").classList.contains("hidden")) loadNext();
}, 30000);
