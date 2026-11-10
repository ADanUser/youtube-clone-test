"use strict";
function cookie(name) { return document.cookie.split("; ").find(item => item.startsWith(`${name}=`))?.split("=")[1] || ""; }
function toast(message) { const node = document.querySelector("#toast"); if (!node) return; node.textContent = message; node.classList.remove("hidden"); setTimeout(() => node.classList.add("hidden"), 2600); }
document.querySelector("[data-logout]")?.addEventListener("click", async () => { const response = await fetch("/api/v1/auth/logout", {method: "POST", headers: {"X-CSRF-Token": cookie("youtube_csrf")}}); if (response.ok) location.assign("/"); else toast("Не удалось выйти. Повторите попытку."); });
const authForm = document.querySelector("[data-auth-form]");
authForm?.addEventListener("submit", async event => { event.preventDefault(); const button = authForm.querySelector("button"); const error = authForm.querySelector("[data-form-error]"); button.disabled = true; error.classList.add("hidden"); try { const csrfResponse = await fetch("/api/v1/auth/csrf", {cache: "no-store"}); if (!csrfResponse.ok) throw new Error("Не удалось начать безопасный вход."); const {csrf_token: csrf} = await csrfResponse.json(); const values = Object.fromEntries(new FormData(authForm)); if (values.channel_title === "") delete values.channel_title; const response = await fetch(`/api/v1/auth/${authForm.dataset.mode}`, {method: "POST", headers: {"Content-Type": "application/json", "X-CSRF-Token": csrf}, body: JSON.stringify(values)}); if (!response.ok) { const messages = {401: "Неверный логин или пароль.", 409: "Аккаунт с такими данными уже существует.", 422: "Проверьте заполнение полей и длину пароля.", 429: "Слишком много попыток. Попробуйте позже."}; throw new Error(messages[response.status] || "Не удалось выполнить запрос."); } location.assign("/"); } catch (reason) { error.textContent = reason.message; error.classList.remove("hidden"); } finally { button.disabled = false; } });
document.querySelectorAll("[data-studio-row]").forEach(row => { row.querySelector("[data-save]").addEventListener("click", async event => { event.currentTarget.disabled = true; const response = await fetch(`/api/v1/videos/${row.dataset.id}`, {method: "PATCH", headers: {"Content-Type": "application/json", "X-CSRF-Token": cookie("youtube_csrf")}, body: JSON.stringify({title: row.querySelector("[data-title]").value, description: row.querySelector("[data-description]").value, visibility: row.querySelector("[data-visibility]").value})}); toast(response.ok ? "Изменения сохранены" : "Не удалось сохранить изменения"); event.currentTarget.disabled = false; }); });

async function mutate(url, method, body) {
  return fetch(url, {method, headers: {"Content-Type": "application/json", "X-CSRF-Token": cookie("youtube_csrf")}, body: body ? JSON.stringify(body) : undefined});
}
const videoRoot = document.querySelector("[data-video-id]");
document.querySelector("[data-like]")?.addEventListener("click", async event => {
  if (!cookie("youtube_csrf")) { location.assign("/login"); return; }
  const active = event.currentTarget.dataset.active === "true";
  const response = await mutate(`/api/v1/videos/${videoRoot.dataset.videoId}/like`, active ? "DELETE" : "PUT");
  if (!response.ok) { toast("Не удалось изменить лайк"); return; }
  event.currentTarget.dataset.active = String(!active);
  event.currentTarget.firstChild.textContent = active ? "♡ " : "♥ ";
  const count = event.currentTarget.querySelector("[data-like-count]");
  count.textContent = String(Number(count.textContent) + (active ? -1 : 1));
});
document.querySelector("[data-comment-form]")?.addEventListener("submit", async event => {
  event.preventDefault();
  const body = event.currentTarget.elements.body.value;
  const response = await mutate(`/api/v1/videos/${videoRoot.dataset.videoId}/comments`, "POST", {body});
  if (response.ok) location.reload(); else toast("Не удалось добавить комментарий");
});
document.querySelectorAll("[data-delete-comment]").forEach(button => button.addEventListener("click", async () => {
  const response = await mutate(`/api/v1/comments/${button.dataset.deleteComment}`, "DELETE");
  if (response.ok) location.reload(); else toast("Не удалось удалить комментарий");
}));
const playlistDialog = document.querySelector("[data-playlist-dialog]");
document.querySelector("[data-playlist-open]")?.addEventListener("click", () => playlistDialog.showModal());
const reportDialog = document.querySelector("[data-report-dialog]");
document.querySelector("[data-report-open]")?.addEventListener("click", () => reportDialog.showModal());
document.querySelector("[data-report-form]")?.addEventListener("submit", async event => {
  event.preventDefault();
  const response = await mutate(`/api/v1/videos/${videoRoot.dataset.videoId}/reports`, "POST", {reason: event.currentTarget.elements.reason.value});
  if (response.ok) { reportDialog.close(); toast("Жалоба отправлена"); } else if (response.status === 409) toast("Жалоба уже ожидает решения"); else toast("Не удалось отправить жалобу");
});
document.querySelectorAll("[data-add-playlist]").forEach(button => button.addEventListener("click", async () => {
  const response = await mutate(`/api/v1/playlists/${button.dataset.addPlaylist}/videos/${videoRoot.dataset.videoId}`, "PUT");
  toast(response.ok ? "Видео сохранено" : "Видео нельзя добавить в этот плейлист");
  if (response.ok) playlistDialog.close();
}));
document.querySelector("[data-subscribe]")?.addEventListener("click", async event => {
  const root = document.querySelector("[data-channel-id]");
  const active = event.currentTarget.dataset.active === "true";
  const response = await mutate(`/api/v1/channels/${root.dataset.channelId}/subscription`, active ? "DELETE" : "PUT");
  if (!response.ok) { toast("Не удалось изменить подписку"); return; }
  event.currentTarget.dataset.active = String(!active);
  event.currentTarget.textContent = active ? "Подписаться" : "Вы подписаны";
  const count = root.querySelector("[data-subscriber-count]");
  count.textContent = String(Number(count.textContent) + (active ? -1 : 1));
});
document.querySelector("[data-playlist-create]")?.addEventListener("submit", async event => {
  event.preventDefault();
  const values = Object.fromEntries(new FormData(event.currentTarget));
  const response = await mutate("/api/v1/playlists", "POST", values);
  if (response.ok) location.assign(`/playlist/${(await response.json()).id}`); else toast("Не удалось создать плейлист");
});
document.querySelectorAll("[data-move]").forEach(button => button.addEventListener("click", async () => {
  const item = button.closest("[data-video-order]");
  const sibling = button.dataset.move === "up" ? item.previousElementSibling : item.nextElementSibling;
  if (!sibling) return;
  if (button.dataset.move === "up") item.parentNode.insertBefore(item, sibling); else item.parentNode.insertBefore(sibling, item);
  const root = document.querySelector("[data-playlist-id]");
  const video_ids = [...document.querySelectorAll("[data-video-order]")].map(node => node.dataset.videoOrder);
  const response = await mutate(`/api/v1/playlists/${root.dataset.playlistId}/order`, "PUT", {video_ids});
  toast(response.ok ? "Порядок сохранён" : "Не удалось изменить порядок");
}));
document.querySelectorAll("[data-remove-video]").forEach(button => button.addEventListener("click", async () => {
  const item = button.closest("[data-video-order]");
  const root = document.querySelector("[data-playlist-id]");
  const response = await mutate(`/api/v1/playlists/${root.dataset.playlistId}/videos/${item.dataset.videoOrder}`, "DELETE");
  if (response.ok) item.remove(); else toast("Не удалось удалить видео из плейлиста");
}));
document.querySelectorAll("[data-moderate-form]").forEach(form => form.addEventListener("submit", async event => {
  event.preventDefault(); const action = event.submitter.value; const row = form.closest("[data-report-row]"); const reason = form.elements.reason.value;
  const response = action === "reject" ? await mutate(`/api/v1/moderation/reports/${row.dataset.reportId}/decision`, "POST", {status: "rejected", reason}) : await mutate(`/api/v1/moderation/videos/${row.dataset.videoId}`, "POST", {action, reason, report_id: row.dataset.reportId});
  if (response.ok) { row.remove(); toast("Решение сохранено"); } else toast("Не удалось сохранить решение");
}));
document.querySelectorAll("[data-user-row]").forEach(row => row.querySelector("[data-user-save]").addEventListener("click", async event => {
  event.currentTarget.disabled = true; const response = await mutate(`/api/v1/admin/users/${row.dataset.userId}`, "PATCH", {role: row.querySelector("[data-role]").value, is_active: row.querySelector("[data-active]").checked});
  toast(response.ok ? "Аккаунт обновлён" : response.status === 409 ? "Нельзя отключить последнего активного администратора" : "Не удалось обновить аккаунт"); event.currentTarget.disabled = false;
}));
