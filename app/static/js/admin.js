/* Admin UI for Loki AI for Byggkon — vanilla JS, no build step. */
(() => {
  "use strict";

  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

  // ─── Tabs ─────────────────────────────────────────────────────────
  $$(".admin-tab").forEach((tab) => {
    tab.addEventListener("click", () => {
      const target = tab.dataset.tab;
      $$(".admin-tab").forEach((t) => t.classList.toggle("is-active", t === tab));
      $$(".tab-pane").forEach((p) =>
        p.classList.toggle("is-active", p.dataset.tabPane === target)
      );
      if (target === "runs") loadRuns();
      if (target === "settings") loadSettings();
      if (target === "health") loadHealth();
    });
  });

  // ─── Helpers ──────────────────────────────────────────────────────
  async function api(method, path, body) {
    const opts = {
      method,
      headers: { "Content-Type": "application/json" },
      credentials: "same-origin",
    };
    if (body !== undefined) opts.body = JSON.stringify(body);
    const resp = await fetch(path, opts);
    if (resp.status === 401) {
      window.location.href = "/login";
      throw new Error("Not signed in");
    }
    if (!resp.ok) {
      const text = await resp.text();
      throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
    }
    return resp.json();
  }

  function fmtTime(ts) {
    if (!ts) return "—";
    return new Date(ts * 1000).toLocaleString("nb-NO", {
      day: "2-digit",
      month: "2-digit",
      year: "numeric",
      hour: "2-digit",
      minute: "2-digit",
    });
  }

  function fmtDuration(start, end) {
    if (!start) return "—";
    const e = end ?? Date.now() / 1000;
    const sec = Math.max(0, Math.round(e - start));
    if (sec < 60) return `${sec} s`;
    if (sec < 3600) return `${Math.floor(sec / 60)} m ${sec % 60} s`;
    return `${Math.floor(sec / 3600)} t ${Math.floor((sec % 3600) / 60)} m`;
  }

  // ─── Dashboard ────────────────────────────────────────────────────
  async function loadStats() {
    try {
      const s = await api("GET", "/api/stats");
      $("#kpi-indexed-files").textContent = s.indexed_files ?? 0;
      $("#kpi-tracked-drives").textContent =
        `${s.tracked_drives ?? 0} drives spores`;

      const total =
        (s.pinecone && (s.pinecone.total_vector_count ?? s.pinecone.totalVectorCount)) ??
        "—";
      $("#kpi-vectors").textContent = total;

      if (s.last_run) {
        $("#kpi-last-run").textContent = fmtTime(s.last_run.finished_at);
        $("#kpi-last-run-detail").textContent = `${
          s.last_run.files_indexed ?? 0
        } indeksert · ${s.last_run.files_skipped ?? 0} hoppet`;
        $("#kpi-last-errors").textContent = s.last_run.errors ?? 0;
      }
    } catch (e) {
      console.warn("stats failed:", e);
    }
  }

  // ─── Runs ─────────────────────────────────────────────────────────
  async function loadRuns() {
    const tbody = $("#runs-table tbody");
    tbody.innerHTML = `<tr><td colspan="8" class="muted">Laster…</td></tr>`;
    try {
      const { runs } = await api("GET", "/api/runs?limit=20");
      if (!runs.length) {
        tbody.innerHTML = `<tr><td colspan="8" class="muted">Ingen kjøringer ennå.</td></tr>`;
        return;
      }
      tbody.innerHTML = runs
        .map(
          (r) => `
            <tr>
              <td>${fmtTime(r.started_at)}</td>
              <td>${fmtDuration(r.started_at, r.finished_at)}</td>
              <td>${r.drives_scanned ?? 0}</td>
              <td>${r.files_indexed ?? 0}</td>
              <td>${r.files_deleted ?? 0}</td>
              <td>${r.files_skipped ?? 0}</td>
              <td class="${r.errors > 0 ? "err" : "ok"}">${r.errors ?? 0}</td>
              <td title="${escapeHtml(r.notes || "")}">${
            r.notes ? truncate(r.notes, 60) : "—"
          }</td>
            </tr>`
        )
        .join("");
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="8" class="err">Feil: ${escapeHtml(
        e.message
      )}</td></tr>`;
    }
  }

  function truncate(s, n) {
    if (!s) return "";
    return s.length > n ? s.slice(0, n - 1) + "…" : s;
  }
  function escapeHtml(s) {
    return String(s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  // ─── Settings ─────────────────────────────────────────────────────
  let _fields = null;
  let _currentSettings = null;

  async function loadSettings() {
    const formEl = $("#settings-form");
    formEl.innerHTML = `<p class="muted">Laster…</p>`;
    try {
      const [{ fields }, { overrides, effective }] = await Promise.all([
        api("GET", "/api/fields"),
        api("GET", "/api/settings"),
      ]);
      _fields = fields;
      _currentSettings = { overrides, effective };
      renderSettings();
    } catch (e) {
      formEl.innerHTML = `<p class="err">Feil: ${escapeHtml(e.message)}</p>`;
    }
  }

  function renderSettings() {
    const formEl = $("#settings-form");
    const groups = {};
    for (const f of _fields) {
      if (!groups[f.group]) groups[f.group] = [];
      groups[f.group].push(f);
    }
    const showAdvanced = $("#show-advanced").checked;

    formEl.innerHTML = Object.entries(groups)
      .map(([group, fields]) => `
        <section class="settings-group">
          <h2>${escapeHtml(group)}</h2>
          <p class="group-desc">${groupBlurb(group)}</p>
          ${fields.map(renderField).join("")}
        </section>
      `)
      .join("");

    // Toggle advanced rows
    $$(".settings-row.is-advanced").forEach((row) =>
      row.classList.toggle("is-shown", showAdvanced)
    );
  }

  function groupBlurb(group) {
    const blurbs = {
      "Microsoft Graph": "Krever en app-registrering med Application permissions og admin consent.",
      "Synkronisering": "Hva skal indekseres, og hva skal hoppes over.",
      "Tidsplan": "Hvor ofte synkroniseringen kjører.",
      "OpenAI Embeddings": "Modell og API-nøkkel for å lage vektorer.",
      "Pinecone": "Hvor vektorene lagres og søkes mot.",
      "Unstructured / Chunking": "Hvordan dokumenter parses og deles opp.",
      "System": "Plassering av state og kjøretids&shy;parametere.",
      "Admin": "Beskyttelse av dette UI-et.",
      "Branding": "Hvordan tjenesten vises i topbar og titler.",
    };
    return blurbs[group] || "";
  }

  function renderField(f) {
    const { overrides, effective } = _currentSettings;
    const hasOverride = Object.prototype.hasOwnProperty.call(overrides, f.key);
    const displayValue = hasOverride ? overrides[f.key] : effective[f.key];

    const badges = [];
    if (f.requires_restart)
      badges.push(`<span class="badge badge-warn">restart</span>`);
    if (f.kind === "password")
      badges.push(`<span class="badge badge-secret">hemmelig</span>`);
    if (f.advanced) badges.push(`<span class="badge badge-advanced">avansert</span>`);

    return `
      <div class="settings-row ${f.advanced ? "is-advanced" : ""}" data-key="${escapeHtml(f.key)}">
        <div class="row-head">
          <span class="row-label">${escapeHtml(f.label)}</span>
          <span class="row-key">${escapeHtml(f.key)}</span>
          ${badges.join(" ")}
        </div>
        ${f.description ? `<p class="row-desc">${escapeHtml(f.description)}</p>` : ""}
        <div class="row-input">${renderInput(f, displayValue, hasOverride)}</div>
      </div>`;
  }

  function renderInput(f, value, hasOverride) {
    const placeholder = f.placeholder ? `placeholder="${escapeHtml(f.placeholder)}"` : "";
    const valStr = value == null ? "" : String(value);
    if (f.kind === "bool") {
      return `<label class="switch">
                <input type="checkbox" data-input="${escapeHtml(f.key)}" data-kind="bool" ${
        valStr === "true" || value === true ? "checked" : ""
      } />
                <span>${value === true || valStr === "true" ? "På" : "Av"}</span>
              </label>`;
    }
    if (f.kind === "enum") {
      return `<select data-input="${escapeHtml(f.key)}" data-kind="enum" class="input">
                <option value="">${
                  hasOverride ? "(fjern overstyring)" : "(samme som env)"
                }</option>
                ${f.options
                  .map(
                    (o) => `<option value="${escapeHtml(o)}" ${
                      String(value) === o ? "selected" : ""
                    }>${escapeHtml(o)}</option>`
                  )
                  .join("")}
              </select>`;
    }
    if (f.kind === "textarea") {
      return `<textarea data-input="${escapeHtml(f.key)}" data-kind="text" ${placeholder}>${escapeHtml(
        valStr
      )}</textarea>`;
    }
    const inputType =
      f.kind === "password" ? "password" : f.kind === "number" ? "number" : "text";
    return `<input type="${inputType}" data-input="${escapeHtml(
      f.key
    )}" data-kind="${escapeHtml(f.kind)}" class="input"
            value="${escapeHtml(valStr)}" ${placeholder} />`;
  }

  $("#show-advanced")?.addEventListener("change", () => {
    $$(".settings-row.is-advanced").forEach((row) =>
      row.classList.toggle("is-shown", $("#show-advanced").checked)
    );
  });

  $("#save-settings-btn")?.addEventListener("click", async () => {
    const status = $("#save-status");
    status.textContent = "Lagrer…";
    status.style.color = "";

    const updates = {};
    $$("[data-input]").forEach((el) => {
      const key = el.dataset.input;
      const kind = el.dataset.kind;
      let val;
      if (kind === "bool") val = el.checked;
      else if (kind === "number") val = el.value === "" ? null : Number(el.value);
      else val = el.value === "" ? null : el.value;
      updates[key] = val;
    });

    // Compare against current overrides — only send actual changes.
    const { overrides } = _currentSettings;
    const diff = {};
    for (const [k, v] of Object.entries(updates)) {
      const before = overrides[k];
      const hasOverride = Object.prototype.hasOwnProperty.call(overrides, k);
      // Always send password fields if they have a non-empty new value
      // (we show masked, so user is typing fresh).
      const fieldDef = _fields.find((f) => f.key === k);
      if (fieldDef?.kind === "password") {
        if (v && !looksMasked(v)) diff[k] = v;
        else if (v === null && hasOverride) diff[k] = null;
        continue;
      }
      if (v === null && hasOverride) {
        diff[k] = null; // clear override
      } else if (v !== null && String(before ?? "") !== String(v)) {
        diff[k] = v;
      }
    }

    if (!Object.keys(diff).length) {
      status.textContent = "Ingen endringer.";
      return;
    }

    try {
      const res = await api("PATCH", "/api/settings", { updates: diff });
      const restart = res.restart_required_for ?? [];
      if (restart.length) {
        status.style.color = "#b07a14";
        status.textContent = `Lagret. Restart kreves for: ${restart.join(", ")}`;
      } else {
        status.style.color = "#1e8b6f";
        status.textContent = "Lagret. Endringer er aktive.";
      }
      await loadSettings();
    } catch (e) {
      status.style.color = "#b03030";
      status.textContent = `Feil: ${e.message}`;
    }
  });

  function looksMasked(s) {
    return typeof s === "string" && /•/.test(s);
  }

  // ─── Tests ────────────────────────────────────────────────────────
  $$("[data-test]").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const which = btn.dataset.test;
      const card = $(`#test-${which}`);
      const status = card.querySelector(".test-status");
      const out = card.querySelector(".test-output");
      btn.disabled = true;
      status.textContent = "Tester…";
      status.dataset.state = "";
      out.classList.add("is-shown");
      out.textContent = "";
      try {
        const r = await api("POST", `/api/test/${which}`);
        out.textContent = JSON.stringify(r, null, 2);
        status.textContent = r.ok ? "OK" : "Feilet";
        status.dataset.state = r.ok ? "ok" : "err";
      } catch (e) {
        out.textContent = e.message;
        status.textContent = "Feilet";
        status.dataset.state = "err";
      } finally {
        btn.disabled = false;
      }
    });
  });

  // ─── Run sync now ─────────────────────────────────────────────────
  $("#run-sync-btn")?.addEventListener("click", async () => {
    const status = $("#run-sync-status");
    const btn = $("#run-sync-btn");
    btn.disabled = true;
    status.textContent = "Trigger sync…";
    try {
      await api("POST", "/api/sync");
      status.textContent = "Synkronisering startet. Følg med i Kjøringer.";
      setTimeout(loadStats, 2000);
    } catch (e) {
      status.textContent = `Feil: ${e.message}`;
    } finally {
      btn.disabled = false;
    }
  });

  // ─── Health (Systemstatus) ───────────────────────────────────────
  const HEALTH_LABELS = {
    application: "Applikasjon",
    graph: "Microsoft Graph",
    openai: "OpenAI Embeddings",
    gemini: "Gemini Embedding 2",
    pinecone_openai: "Pinecone — OpenAI-indeks",
    pinecone_gemini: "Pinecone — Gemini-indeks",
    state_db: "State-database (SQLite)",
    disk: "Disk / volume",
    scheduler: "Scheduler",
    sync_history: "Synkroniserings­historikk",
  };

  let _healthAutoTimer = null;

  async function loadHealth() {
    const grid = document.getElementById("health-grid");
    const overall = document.getElementById("health-overall");
    const updated = document.getElementById("health-updated");
    grid.innerHTML = `<p class="muted">Tester…</p>`;
    overall.textContent = "Tester…";
    overall.dataset.state = "";

    try {
      const r = await api("GET", "/api/health");
      overall.textContent =
        r.overall === "ok" ? "Alt OK" : r.overall === "warn" ? "Advarsler" : "Feil";
      overall.dataset.state = r.overall;
      updated.textContent = `Oppdatert ${new Date(r.generated_at * 1000).toLocaleTimeString("nb-NO")}`;

      grid.innerHTML = Object.entries(HEALTH_LABELS)
        .map(([key, label]) => {
          const c = r.checks[key] || { status: "err", detail: "Mangler", ms: 0 };
          return `
            <article class="health-card health-${c.status}">
              <div class="health-card-head">
                <span class="health-dot"></span>
                <h3>${escapeHtml(label)}</h3>
                <span class="health-ms">${c.ms ?? 0} ms</span>
              </div>
              <p class="health-detail">${escapeHtml(c.detail || "—")}</p>
            </article>`;
        })
        .join("");
    } catch (e) {
      grid.innerHTML = `<p class="err">Feil: ${escapeHtml(e.message)}</p>`;
      overall.textContent = "Feil";
      overall.dataset.state = "err";
    }
  }

  document.getElementById("health-refresh-btn")?.addEventListener("click", loadHealth);

  // Auto-refresh while the Systemstatus tab is active.
  function maybeStartHealthAutoRefresh() {
    const isActive = document
      .querySelector('[data-tab-pane="health"]')
      ?.classList.contains("is-active");
    if (isActive && !_healthAutoTimer) {
      _healthAutoTimer = setInterval(loadHealth, 30000);
    } else if (!isActive && _healthAutoTimer) {
      clearInterval(_healthAutoTimer);
      _healthAutoTimer = null;
    }
  }
  $$(".admin-tab").forEach((tab) =>
    tab.addEventListener("click", () => setTimeout(maybeStartHealthAutoRefresh, 50))
  );

  // ─── Init ─────────────────────────────────────────────────────────
  loadStats();
  setInterval(loadStats, 30000);
})();
