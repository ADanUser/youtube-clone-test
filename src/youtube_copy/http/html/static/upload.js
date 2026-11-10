"use strict";

const form = document.querySelector("#upload-form");
const fields = document.querySelector("#fields");
const cancel = document.querySelector("#cancel");
const progress = document.querySelector("#progress");
const statusLine = document.querySelector("#status");
let xhr = null;
let pollTimer = null;

function csrf() {
  return document.cookie.split("; ").find(item => item.startsWith("youtube_csrf="))?.split("=")[1] || "";
}

async function pollStatus(id) {
  try {
    const response = await fetch(`/api/v1/videos/${id}/status`, {cache: "no-store"});
    if (!response.ok) throw new Error("Статус недоступен. Проверьте вход в аккаунт.");
    const video = await response.json();
    if (video.processing_state === "ready") {
      statusLine.textContent = "Видео готово. ";
      const watch = document.createElement("a");
      watch.href = `/watch/${encodeURIComponent(id)}`;
      watch.textContent = "Смотреть видео";
      statusLine.append(watch);
    } else if (video.processing_state === "failed") {
      statusLine.textContent = "Обработка завершилась ошибкой. Можно загрузить файл повторно.";
    } else {
      statusLine.textContent = "Файл принят. Ожидаем завершения обработки.";
      pollTimer = setTimeout(() => pollStatus(id), 3000);
    }
  } catch (error) {
    statusLine.textContent = error.message;
  }
}

fetch("/api/v1/auth/me", {cache: "no-store"}).then(response => {
  if (!response.ok) throw new Error();
  document.querySelector("#account").textContent = "Вы вошли в аккаунт. Видео будет загружено на ваш канал.";
  fields.disabled = false;
}).catch(() => {
  document.querySelector("#account").textContent = "Сначала войдите в аккаунт. На этом этапе вход и регистрация доступны через API /docs.";
});

cancel.addEventListener("click", () => xhr?.abort());
window.addEventListener("pagehide", () => {
  clearTimeout(pollTimer);
  xhr?.abort();
});

form.addEventListener("submit", async event => {
  event.preventDefault();
  const file = form.elements.file.files[0];
  if (!file || !file.size || file.size > Number(form.dataset.maxBytes)) {
    statusLine.textContent = "Выберите непустой файл в пределах указанного лимита.";
    return;
  }
  clearTimeout(pollTimer);
  fields.disabled = true;
  progress.value = 0;
  statusLine.textContent = "Создаём запись видео…";
  try {
    const response = await fetch("/api/v1/videos", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf()},
      body: JSON.stringify({title: form.elements.title.value, description: form.elements.description.value, visibility: form.elements.visibility.value})
    });
    if (!response.ok) throw new Error(`Не удалось создать видео (HTTP ${response.status}).`);
    const video = await response.json();
    xhr = new XMLHttpRequest();
    xhr.open("POST", `/api/v1/videos/${video.id}/upload`);
    xhr.setRequestHeader("Content-Type", "application/octet-stream");
    xhr.setRequestHeader("X-CSRF-Token", csrf());
    xhr.upload.onprogress = event => {
      if (event.lengthComputable) {
        progress.value = Math.round(event.loaded / event.total * 100);
        statusLine.textContent = progress.value === 100 ? "Передача завершена. Сохраняем файл…" : `Передано ${progress.value}%.`;
      }
    };
    xhr.onload = () => {
      if (xhr.status === 202) {
        progress.value = 100;
        pollStatus(video.id);
      } else {
        statusLine.textContent = `Загрузка не принята (HTTP ${xhr.status}). Проверьте лимиты и повторите попытку.`;
      }
    };
    xhr.onerror = () => { statusLine.textContent = "Соединение прервано. Повторите загрузку."; };
    xhr.onabort = () => { statusLine.textContent = "Передача отменена. Можно загрузить файл снова."; };
    xhr.onloadend = () => { fields.disabled = false; cancel.disabled = true; xhr = null; };
    cancel.disabled = false;
    statusLine.textContent = "Передаём файл…";
    xhr.send(file);
  } catch (error) {
    statusLine.textContent = error.message;
    fields.disabled = false;
    cancel.disabled = true;
  }
});
