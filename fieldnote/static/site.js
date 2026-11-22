document.querySelectorAll("form[data-busy]").forEach((form) => {
  form.addEventListener("submit", () => {
    const button = form.querySelector("[type=submit]");
    if (!button) return;
    button.disabled = true;
    if (button.dataset.busy) button.textContent = button.dataset.busy;
  });
});

const filter = document.querySelector("#task-filter");
const rows = Array.from(document.querySelectorAll("table.plan tbody tr"));
const chips = Array.from(document.querySelectorAll("[data-chip]"));
const empty = document.querySelector("#filter-empty");
let chip = "all";

function applyFilter() {
  if (!rows.length) return;
  const query = (filter && filter.value || "").trim().toLowerCase();
  let shown = 0;
  rows.forEach((row) => {
    const hay = (row.dataset.hay || "").toLowerCase();
    const textOk = !query || hay.includes(query);
    const chipOk =
      chip === "all" ||
      (chip === "blocked" && row.dataset.blocked === "yes") ||
      (chip === "overdue" && row.dataset.overdue === "yes") ||
      (chip === "drift" && row.dataset.drift === "yes");
    const visible = textOk && chipOk;
    row.hidden = !visible;
    if (visible) shown += 1;
  });
  if (empty) empty.hidden = shown !== 0;
}

if (filter) filter.addEventListener("input", applyFilter);
chips.forEach((button) => {
  button.addEventListener("click", () => {
    chip = button.dataset.chip || "all";
    chips.forEach((other) => other.classList.toggle("on", other === button));
    applyFilter();
  });
});
