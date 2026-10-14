/* User administration (admins only): create, promote/demote, reset password, delete. */

const listEl = document.getElementById("user-list");
const statusEl = document.getElementById("user-status");
const addForm = document.getElementById("add-user");

async function api(method, url, body) {
  const res = await fetch(url, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  if (Auth.unauth(res)) return null;
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const d = data.detail;
    Auth.toast(typeof d === "string" ? d : `Failed (${res.status})`);
    return null;
  }
  return data;
}

function render(users) {
  const me = Auth.state.user?.id;
  statusEl.textContent = `${users.length} users · ${users.filter((u) => u.is_admin).length} admins`;
  listEl.innerHTML = `<table class="feeds users">
    <thead><tr><th>User</th><th>Role</th><th>Votes</th><th>Read</th><th>Chats</th><th>Created</th><th></th></tr></thead>
    <tbody>${users.map((u) => `
      <tr data-id="${Number(u.id)}">
        <td><b>${esc(u.name)}</b>${u.id === me ? ` <span class="muted">(you)</span>` : ""}${u.id === 1 ? ` <span class="muted">owner</span>` : ""}</td>
        <td><button type="button" class="toggle role" aria-pressed="${!!u.is_admin}"
              ${u.id === 1 || u.id === me ? "disabled" : ""} title="${u.is_admin ? "Demote to user" : "Promote to admin"}">
              <span class="toggle-track" aria-hidden="true"></span>${u.is_admin ? "admin" : "user"}</button></td>
        <td>${u.votes}</td><td>${u.reads}</td><td>${u.chats}</td>
        <td class="muted nowrap">${esc(fmtShort(u.created_at))}</td>
        <td class="nowrap">
          <button type="button" class="btn reset">Set password</button>
          <button type="button" class="btn del" ${u.id === 1 || u.id === me ? "disabled" : ""}>Delete</button>
        </td>
      </tr>`).join("")}</tbody></table>`;

  listEl.querySelectorAll("tr[data-id]").forEach((row) => {
    const id = Number(row.dataset.id);
    const u = users.find((x) => x.id === id);
    row.querySelector(".role").onclick = async () => {
      const d = await api("PATCH", `/api/admin/users/${id}`, { is_admin: !u.is_admin });
      if (d) render(d.users);
    };
    row.querySelector(".reset").onclick = async () => {
      const pw = prompt(`New password for ${u.name} (min. 8 characters):`);
      if (pw == null) return;
      if (pw.length < 8) return Auth.toast("Password too short (min. 8).");
      const d = await api("PATCH", `/api/admin/users/${id}`, { password: pw });
      if (d) { render(d.users); Auth.toast(`Password of ${u.name} changed.`); }
    };
    row.querySelector(".del").onclick = async () => {
      if (!confirm(`Delete ${u.name}? Their preferences, votes, read state and chats are removed.`)) return;
      const d = await api("DELETE", `/api/admin/users/${id}`);
      if (d) render(d.users);
    };
  });
}

async function load() {
  const d = await api("GET", "/api/admin/users");
  if (d) render(d.users);
}

addForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = addForm.elements;
  const d = await api("POST", "/api/admin/users", {
    name: f.name.value.trim(), password: f.password.value, is_admin: f.is_admin.checked,
  });
  if (!d) return;
  addForm.reset();
  render(d.users);
  Auth.toast("User created.");
});

(async function boot() {
  await Auth.init();
  load();
})();
