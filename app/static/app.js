// Небольшие улучшения поверх обычных форм: без скриптов всё, кроме ползунков, продолжает работать.

// Кнопки «Скопировать»
document.querySelectorAll("[data-copy]").forEach((button) => {
  button.addEventListener("click", async () => {
    const text = document.querySelector(button.dataset.copy).value;
    try {
      await navigator.clipboard.writeText(text);
      const label = button.textContent;
      button.textContent = "Скопировано";
      setTimeout(() => (button.textContent = label), 1500);
    } catch {
      document.querySelector(button.dataset.copy).select();
    }
  });
});

// Подтверждение необратимых действий
document.querySelectorAll("[data-confirm]").forEach((el) => {
  el.addEventListener("click", (e) => {
    if (!confirm(el.dataset.confirm)) e.preventDefault();
  });
});

// Страница перезагружается, когда на сервере меняется состояние дегустации
const poll = document.querySelector("[data-poll-url]");
if (poll) {
  const interval = Number(poll.dataset.pollEvery || 5000);
  setInterval(async () => {
    if (document.hidden) return;
    // не сбрасываем форму, пока человек её заполняет
    if (poll.dataset.pollPause && document.querySelector(poll.dataset.pollPause)) return;
    try {
      const res = await fetch(poll.dataset.pollUrl, { cache: "no-store" });
      const { v } = await res.json();
      if (v !== poll.dataset.pollVersion) location.reload();
    } catch {
      /* сеть на площадке бывает нестабильной — попробуем в следующий раз */
    }
  }, interval);
}

// Ползунок для широких шкал: значение уходит на сервер только после того, как его тронули
document.querySelectorAll("[data-slider]").forEach((box) => {
  const range = box.querySelector("input[type=range]");
  const hidden = box.querySelector("input[type=hidden]");
  const output = box.querySelector("output");
  const sync = () => {
    hidden.value = range.value;
    output.textContent = range.value;
  };
  range.addEventListener("input", sync);
  range.addEventListener("change", sync);
});

// Отпечаток устройства для поиска повторных входов; на сервере хранится только его хеш
const fp = document.querySelector("input[name=fp]");
if (fp) {
  fp.value = [
    navigator.language,
    navigator.platform,
    screen.width + "x" + screen.height + "@" + devicePixelRatio,
    Intl.DateTimeFormat().resolvedOptions().timeZone,
    navigator.hardwareConcurrency,
    navigator.maxTouchPoints,
  ].join("|");
}

// Добавление строки критерия на странице шкал
const addCriterion = document.querySelector("[data-add-criterion]");
if (addCriterion) {
  addCriterion.addEventListener("click", () => {
    const template = document.querySelector("#criterion-row");
    document.querySelector("#criteria-body").appendChild(template.content.cloneNode(true));
  });
}
