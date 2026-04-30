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
      if (target === "progress") loadProgress();
      if (target === "folders") loadFoldersTab();
      if (target === "mcp") loadMcpTab();
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

  // ─── Runs (with expandable per-run event log) ─────────────────────
  let _runsState = {
    expanded: new Set(),     // run_ids that are expanded
    eventsCache: new Map(),  // run_id → events[]
    activeRunId: null,
    autoRefreshTimer: null,
  };

  async function loadRuns() {
    const tbody = $("#runs-table tbody");
    const onlyActive = document.getElementById("runs-only-active")?.checked;
    if (!tbody.children.length || tbody.querySelector("td.muted")) {
      tbody.innerHTML = `<tr><td colspan="9" class="muted">Laster…</td></tr>`;
    }
    try {
      const { runs } = await api("GET", "/api/runs?limit=20");
      if (!runs.length) {
        tbody.innerHTML = `<tr><td colspan="9" class="muted">Ingen kjøringer ennå. Trykk "Kjør synkronisering nå" i sidemenyen.</td></tr>`;
        return;
      }

      const visible = onlyActive
        ? runs.filter((r) => !r.finished_at)
        : runs;

      // Track active run for auto-refresh
      _runsState.activeRunId =
        runs.find((r) => !r.finished_at)?.id ?? null;

      tbody.innerHTML = visible
        .map((r) => renderRunRow(r))
        .join("");

      // Wire up expand/collapse
      tbody.querySelectorAll(".run-row[data-run-id]").forEach((row) => {
        row.addEventListener("click", () => toggleRunRow(row));
      });

      // Reload events for already-expanded runs
      for (const rid of _runsState.expanded) {
        if (visible.find((r) => r.id === rid)) {
          await loadRunEvents(rid);
        }
      }

      // Auto-refresh while a run is active
      maybeStartRunsAutoRefresh();
    } catch (e) {
      tbody.innerHTML = `<tr><td colspan="9" class="err">Feil: ${escapeHtml(e.message)}</td></tr>`;
    }
  }

  function renderRunRow(r) {
    const expanded = _runsState.expanded.has(r.id);
    const isActive = !r.finished_at;
    const statusBadge = isActive
      ? `<span class="phase-tag" style="background: var(--green-dim); color: var(--green);">PÅGÅR</span>`
      : r.errors > 0
      ? `<span class="phase-tag" style="background: var(--rose-dim); color: #b03030;">FEIL</span>`
      : `<span class="phase-tag" style="background: var(--obsidian-soft); color: var(--obsidian);">FERDIG</span>`;
    const arrow = expanded ? "▾" : "▸";

    const events = _runsState.eventsCache.get(r.id) || [];
    const eventsRow = expanded
      ? `<tr class="run-events-row">
           <td colspan="9">
             <div class="run-events-shell">
               ${renderEvents(events, isActive)}
             </div>
           </td>
         </tr>`
      : "";

    return `
      <tr class="run-row" data-run-id="${r.id}" data-active="${isActive}">
        <td class="run-toggle">${arrow}</td>
        <td>${statusBadge}</td>
        <td>${fmtTime(r.started_at)}</td>
        <td>${fmtDuration(r.started_at, r.finished_at)}</td>
        <td>${r.drives_scanned ?? 0}</td>
        <td>${r.files_indexed ?? 0}</td>
        <td>${r.files_deleted ?? 0}</td>
        <td>${r.files_skipped ?? 0}</td>
        <td class="${r.errors > 0 ? "err" : "ok"}">${r.errors ?? 0}</td>
      </tr>
      ${eventsRow}`;
  }

  function renderEvents(events, isActive) {
    if (!events.length) {
      return isActive
        ? `<p class="muted">Henter hendelser…</p>`
        : `<p class="muted">Ingen detaljerte hendelser registrert for denne kjøringen.</p>`;
    }
    // Events come sorted DESC; reverse for chronological reading.
    const ordered = [...events].reverse();
    return `<ul class="event-list">
      ${ordered
        .map((e) => {
          const cls = `event-${e.level}`;
          const time = new Date(e.ts * 1000).toLocaleTimeString("nb-NO");
          const ctx = [e.drive_label, e.file_name].filter(Boolean).join(" · ");
          return `
            <li class="event-item ${cls}">
              <span class="event-time">${time}</span>
              <span class="event-name">${escapeHtml(e.event)}</span>
              ${ctx ? `<span class="event-ctx">${escapeHtml(ctx)}</span>` : ""}
              <span class="event-msg">${escapeHtml(e.message || "")}</span>
            </li>`;
        })
        .join("")}
    </ul>`;
  }

  async function toggleRunRow(rowEl) {
    const runId = parseInt(rowEl.dataset.runId, 10);
    if (_runsState.expanded.has(runId)) {
      _runsState.expanded.delete(runId);
    } else {
      _runsState.expanded.add(runId);
      await loadRunEvents(runId);
    }
    await loadRuns();
  }

  async function loadRunEvents(runId) {
    try {
      const r = await api("GET", `/api/events?run_id=${runId}&limit=400`);
      _runsState.eventsCache.set(runId, r.events || []);
    } catch (e) {
      console.warn("events load failed:", e);
    }
  }

  function maybeStartRunsAutoRefresh() {
    const isOnRunsTab = document
      .querySelector('[data-tab-pane="runs"]')
      ?.classList.contains("is-active");
    const hasActive = _runsState.activeRunId != null;

    if (isOnRunsTab && hasActive && !_runsState.autoRefreshTimer) {
      _runsState.autoRefreshTimer = setInterval(() => loadRuns(), 3000);
    } else if ((!isOnRunsTab || !hasActive) && _runsState.autoRefreshTimer) {
      clearInterval(_runsState.autoRefreshTimer);
      _runsState.autoRefreshTimer = null;
    }
  }

  document.getElementById("runs-refresh-btn")?.addEventListener("click", loadRuns);
  document.getElementById("runs-only-active")?.addEventListener("change", loadRuns);

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

  // Maps a settings-group name → the /api/test endpoint that exercises it.
  const GROUP_TESTS = {
    "Microsoft Graph": "graph",
    "OpenAI Embeddings": "openai",
    "Gemini Embeddings": "gemini",
    "Pinecone": "pinecone",
  };

  function renderSettings() {
    const formEl = $("#settings-form");
    const groups = {};
    for (const f of _fields) {
      if (!groups[f.group]) groups[f.group] = [];
      groups[f.group].push(f);
    }
    const showAdvanced = $("#show-advanced").checked;

    formEl.innerHTML = Object.entries(groups)
      .map(([group, fields]) => {
        const test = GROUP_TESTS[group];
        const testBtn = test
          ? `<button type="button" class="btn btn-secondary group-test-btn" data-group-test="${test}">
               <span class="group-test-dot" data-state=""></span>
               <span class="group-test-label">Test tilkobling</span>
             </button>`
          : "";
        return `
          <section class="settings-group" data-group-name="${escapeHtml(group)}">
            <header class="settings-group-head">
              <div>
                <h2>${escapeHtml(group)}</h2>
                <p class="group-desc">${groupBlurb(group)}</p>
              </div>
              ${testBtn}
            </header>
            <pre class="group-test-output" data-group-output="${test || ''}"></pre>
            ${fields.map(renderField).join("")}
          </section>
        `;
      })
      .join("");

    // Wire up per-group test buttons.
    $$("[data-group-test]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        const which = btn.dataset.groupTest;
        const dot = btn.querySelector(".group-test-dot");
        const lbl = btn.querySelector(".group-test-label");
        const out = btn
          .closest(".settings-group")
          .querySelector(".group-test-output");
        btn.disabled = true;
        dot.dataset.state = "pending";
        lbl.textContent = "Tester…";
        out.classList.remove("is-shown");
        try {
          const r = await api("POST", `/api/test/${which}`);
          dot.dataset.state = r.ok ? "ok" : "err";
          lbl.textContent = r.ok ? "OK" : "Feilet";
          out.textContent = JSON.stringify(r, null, 2);
          out.classList.add("is-shown");
        } catch (e) {
          dot.dataset.state = "err";
          lbl.textContent = "Feilet";
          out.textContent = e.message;
          out.classList.add("is-shown");
        } finally {
          btn.disabled = false;
        }
      });
    });

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

  // "Test alle tilkoblinger" — clicks every per-group test button in order
  // and updates the toolbar dot to the worst result.
  document
    .getElementById("test-all-groups-btn")
    ?.addEventListener("click", async () => {
      const toolbarDot = document.querySelector(
        "#test-all-groups-btn .group-test-dot"
      );
      toolbarDot.dataset.state = "pending";
      let worst = "ok";
      for (const btn of $$("[data-group-test]")) {
        btn.click();
        // Wait for this test to finish before kicking off the next one,
        // by polling its dot until it's no longer "pending".
        const dot = btn.querySelector(".group-test-dot");
        await new Promise((res) => {
          const tick = () => {
            if (dot.dataset.state && dot.dataset.state !== "pending") {
              if (dot.dataset.state === "err") worst = "err";
              else if (dot.dataset.state === "warn" && worst !== "err")
                worst = "warn";
              res();
            } else {
              setTimeout(tick, 100);
            }
          };
          tick();
        });
      }
      toolbarDot.dataset.state = worst;
    });

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
    status.style.color = "";
    status.textContent = "Trigger sync…";
    try {
      const r = await api("POST", "/api/sync");
      if (r.status === "started") {
        status.style.color = "#1e8b6f";
        status.textContent = "Synkronisering startet. Følg med i Kjøringer.";
      } else if (r.status === "already_running") {
        status.style.color = "#b07a14";
        status.textContent = r.reason || "En synkronisering pågår allerede.";
      } else if (r.status === "skipped") {
        status.style.color = "#b03030";
        const errs = r.init_errors || {};
        const errSummary = Object.entries(errs)
          .map(([k, v]) => `${k}: ${v}`)
          .join(" · ");
        status.textContent =
          (r.reason || "Synkronisering hoppet over.") +
          (errSummary ? ` [${errSummary}]` : "");
      } else {
        status.textContent = JSON.stringify(r);
      }
      setTimeout(loadStats, 2000);
    } catch (e) {
      status.style.color = "#b03030";
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

  // ─── Progress (Fremdrift) ────────────────────────────────────────
  let _progressTimer = null;

  function fmtSecs(sec) {
    if (sec == null) return "—";
    if (sec < 60) return `${sec} s`;
    if (sec < 3600) return `${Math.floor(sec / 60)} m ${sec % 60} s`;
    if (sec < 86400) return `${Math.floor(sec / 3600)} t ${Math.floor((sec % 3600) / 60)} m`;
    return `${Math.floor(sec / 86400)} d ${Math.floor((sec % 86400) / 3600)} t`;
  }

  async function loadProgress() {
    try {
      const p = await api("GET", "/api/progress");
      const s = p.summary;
      const pct = s.total_estimated
        ? Math.min(100, (s.total_processed / s.total_estimated) * 100)
        : (s.total_seen ? Math.min(100, (s.total_processed / s.total_seen) * 100) : 0);

      document.getElementById("progress-bar-fill").style.width = pct.toFixed(1) + "%";
      document.getElementById("progress-processed").textContent = s.total_processed;
      document.getElementById("progress-seen").textContent = s.total_seen;
      document.getElementById("progress-estimated").textContent = s.total_estimated || "—";
      document.getElementById("progress-rate").textContent = s.files_per_minute;
      document.getElementById("progress-elapsed").textContent = fmtSecs(s.elapsed_seconds);
      document.getElementById("progress-eta").textContent =
        s.eta_seconds != null ? `Estimert gjenstår: ${fmtSecs(s.eta_seconds)}` : "Estimat ikke tilgjengelig";

      document.getElementById("progress-drives-done").textContent = s.drives_done;
      document.getElementById("progress-drives-active").textContent = s.drives_active;
      document.getElementById("progress-drives-pending").textContent = s.drives_pending;

      const summaryLine = (() => {
        if (s.drives_active > 0)
          return `Synkronisering pågår — ${s.drives_active} drive${s.drives_active === 1 ? "" : "s"} aktive akkurat nå.`;
        if (p.next_run_at) {
          const next = new Date(p.next_run_at * 1000);
          return `Ingen aktiv kjøring. Neste planlagt: ${next.toLocaleTimeString("nb-NO")}.`;
        }
        return "Ingen aktiv kjøring.";
      })();
      document.getElementById("progress-summary-line").textContent = summaryLine;

      const grid = document.getElementById("progress-drives-grid");
      if (!p.drives.length) {
        grid.innerHTML = `<p class="muted">Ingen drives spores ennå.</p>`;
      } else {
        grid.innerHTML = p.drives
          .map((d) => {
            const total = d.estimated_total || d.files_seen || 0;
            const done = d.files_processed || 0;
            const pct = total ? Math.min(100, (done / total) * 100) : 0;
            const phaseClass = d.phase === "done" ? "done" : d.phase === "syncing" ? "active" : "pending";
            return `
              <article class="progress-drive-card progress-drive-${phaseClass}">
                <header>
                  <h4>${escapeHtml(d.drive_label || "(ukjent)")}</h4>
                  <span class="phase-tag">${escapeHtml(d.phase || "")}</span>
                </header>
                <div class="progress-bar-track tiny">
                  <div class="progress-bar-fill" style="width: ${pct.toFixed(1)}%"></div>
                </div>
                <div class="progress-drive-stats muted small">
                  <span>${done} / ${total || "?"}</span>
                  <span>${d.current_file ? "📄 " + escapeHtml(truncate(d.current_file, 40)) : ""}</span>
                </div>
              </article>`;
          })
          .join("");
      }
    } catch (e) {
      console.warn("progress failed:", e);
    }
  }

  function maybeStartProgressAutoRefresh() {
    const isActive = document
      .querySelector('[data-tab-pane="progress"]')
      ?.classList.contains("is-active");
    if (isActive && !_progressTimer) {
      _progressTimer = setInterval(loadProgress, 3000);
    } else if (!isActive && _progressTimer) {
      clearInterval(_progressTimer);
      _progressTimer = null;
    }
  }
  $$(".admin-tab").forEach((tab) =>
    tab.addEventListener("click", () => setTimeout(maybeStartProgressAutoRefresh, 50))
  );

  // ─── Folders tab (interactive picker) ─────────────────────────────
  let _folderState = { user: null, includes: new Set(), expanded: new Map() };

  async function loadFoldersTab() {
    // Populate user dropdown from /api/graph/users (if available).
    const sel = document.getElementById("folder-user-select");
    sel.innerHTML = `<option value="">Laster brukere…</option>`;
    try {
      const r = await api("GET", "/api/graph/users");
      sel.innerHTML =
        `<option value="">— velg bruker —</option>` +
        r.users
          .map(
            (u) =>
              `<option value="${escapeHtml(u.upn || u.id)}">${escapeHtml(
                u.display_name || u.upn || u.id
              )}</option>`
          )
          .join("");
    } catch (e) {
      sel.innerHTML = `<option value="">Feil: ${escapeHtml(e.message)}</option>`;
    }
    document.getElementById("folder-load-btn").disabled = false;
  }

  document.getElementById("folder-user-select")?.addEventListener("change", (e) => {
    _folderState.user = e.target.value || null;
    document.getElementById("folder-load-btn").disabled = !_folderState.user;
  });

  document.getElementById("folder-load-btn")?.addEventListener("click", async () => {
    if (!_folderState.user) return;
    const tree = document.getElementById("folder-tree");
    tree.innerHTML = `<p class="muted">Henter mapper…</p>`;
    try {
      const r = await api("GET", `/api/graph/folders?user=${encodeURIComponent(_folderState.user)}`);
      _folderState.includes = new Set(r.selected_paths || []);
      tree.innerHTML = renderFolderTree(r.folders, "");
      wireFolderTree();
    } catch (e) {
      tree.innerHTML = `<p class="err">Feil: ${escapeHtml(e.message)}</p>`;
    }
  });

  function renderFolderTree(folders, indent) {
    if (!folders || !folders.length)
      return `<p class="muted">Ingen mapper.</p>`;
    return `<ul class="folder-list">${folders
      .map((f) => {
        const checked = _folderState.includes.has(f.path) ? "checked" : "";
        return `
          <li class="folder-item">
            <label>
              <input type="checkbox" data-folder-path="${escapeHtml(f.path)}" ${checked}/>
              <span class="folder-name">${escapeHtml(f.name)}</span>
              <span class="muted small">${f.child_count ?? 0} elementer</span>
            </label>
          </li>`;
      })
      .join("")}</ul>`;
  }

  function wireFolderTree() {
    $$("[data-folder-path]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) _folderState.includes.add(cb.dataset.folderPath);
        else _folderState.includes.delete(cb.dataset.folderPath);
      });
    });
  }

  // ─── SharePoint sites picker ──────────────────────────────────────
  let _sitesState = { selectedDriveIds: new Set(), sites: [] };

  document.getElementById("sites-load-btn")?.addEventListener("click", async () => {
    const tree = document.getElementById("sites-tree");
    const status = document.getElementById("sites-load-status");
    tree.innerHTML = `<p class="muted">Henter SharePoint-områder…</p>`;
    status.textContent = "";
    try {
      const r = await api("GET", "/api/graph/sites");
      if (r.error) {
        tree.innerHTML = `<p class="err">Feil: ${escapeHtml(r.error)}</p>`;
        return;
      }
      _sitesState.sites = r.sites || [];
      _sitesState.selectedDriveIds = new Set();
      for (const s of _sitesState.sites) {
        for (const d of s.drives || []) {
          if (d.selected) _sitesState.selectedDriveIds.add(d.id);
        }
      }
      tree.innerHTML = renderSitesTree(_sitesState.sites);
      wireSitesTree();
      status.textContent = `${_sitesState.sites.length} områder funnet · ${_sitesState.selectedDriveIds.size} valgt`;
    } catch (e) {
      tree.innerHTML = `<p class="err">Feil: ${escapeHtml(e.message)}</p>`;
    }
  });

  function renderSitesTree(sites) {
    if (!sites.length) {
      return `<p class="muted">Ingen SharePoint-områder funnet. Sjekk at Sites.Read.All er gitt admin consent i Entra ID.</p>`;
    }
    return `<ul class="folder-list">${sites
      .map((s) => `
        <li class="folder-item site-block">
          <div class="site-head">
            <strong>${escapeHtml(s.name)}</strong>
            ${s.web_url ? `<a href="${escapeHtml(s.web_url)}" target="_blank" rel="noopener" class="muted small">↗ åpne</a>` : ""}
          </div>
          <ul class="folder-list nested">
            ${(s.drives || [])
              .map((d) => {
                const checked = _sitesState.selectedDriveIds.has(d.id) ? "checked" : "";
                return `
                  <li class="folder-item">
                    <label>
                      <input type="checkbox" data-site-drive="${escapeHtml(d.id)}" ${checked}/>
                      <span class="folder-name">${escapeHtml(d.name)}</span>
                      <span class="muted small">${escapeHtml(d.drive_type)}</span>
                    </label>
                  </li>`;
              })
              .join("")}
          </ul>
        </li>`)
      .join("")}</ul>`;
  }

  function wireSitesTree() {
    $$("[data-site-drive]").forEach((cb) => {
      cb.addEventListener("change", () => {
        if (cb.checked) _sitesState.selectedDriveIds.add(cb.dataset.siteDrive);
        else _sitesState.selectedDriveIds.delete(cb.dataset.siteDrive);
        const status = document.getElementById("sites-load-status");
        if (status) status.textContent = `${_sitesState.selectedDriveIds.size} valgt`;
      });
    });
  }

  document.getElementById("sites-save-btn")?.addEventListener("click", async () => {
    const status = document.getElementById("sites-save-status");
    status.textContent = "Lagrer…";
    try {
      const r = await api("POST", "/api/graph/sharepoint-selection", {
        drive_ids: Array.from(_sitesState.selectedDriveIds),
      });
      status.style.color = "#1e8b6f";
      status.textContent = `Lagret ${r.count} drive(s). Tar effekt på neste synkronisering.`;
    } catch (e) {
      status.style.color = "#b03030";
      status.textContent = `Feil: ${e.message}`;
    }
  });

  document.getElementById("folder-save-btn")?.addEventListener("click", async () => {
    if (!_folderState.user) return;
    const status = document.getElementById("folder-save-status");
    status.textContent = "Lagrer…";
    try {
      const paths = Array.from(_folderState.includes);
      await api("POST", "/api/graph/folder-selection", {
        user: _folderState.user,
        paths,
      });
      status.style.color = "#1e8b6f";
      status.textContent = `Lagret ${paths.length} mappe(r) for ${_folderState.user}.`;
    } catch (e) {
      status.style.color = "#b03030";
      status.textContent = `Feil: ${e.message}`;
    }
  });

  // ─── MCP tab ──────────────────────────────────────────────────────
  async function loadMcpTab() {
    try {
      const r = await api("GET", "/api/mcp/info");
      document.getElementById("mcp-url").textContent = r.url || "MCP er deaktivert";

      const status = document.getElementById("mcp-status");
      if (!r.enabled) {
        status.textContent = "Deaktivert";
        status.style.background = "var(--grey-100)";
      } else if (!r.bearer_token_set) {
        status.textContent = "Mangler bearer-token";
        status.style.background = "var(--cream-dim)";
        status.style.color = "#b07a14";
      } else {
        status.textContent = "Aktiv";
        status.style.background = "var(--green-dim)";
        status.style.color = "var(--green)";
      }

      // Token display: don't render the actual token; tell user how to retrieve.
      const tokenPlaceholder = r.bearer_token_set
        ? "<MCP_BEARER_TOKEN — hent fra Railway/Settings>"
        : "<sett MCP_BEARER_TOKEN først>";
      const url = r.url || "https://<din-railway-url>/mcp";

      // Claude Desktop config
      document.getElementById("mcp-claude-cfg").textContent = JSON.stringify(
        {
          mcpServers: {
            "loki-byggkon": {
              url: url,
              transport: "streamable-http",
              headers: {
                Authorization: `Bearer ${tokenPlaceholder}`,
              },
            },
          },
        },
        null,
        2
      );

      // Cursor config
      document.getElementById("mcp-cursor-cfg").textContent = JSON.stringify(
        {
          mcpServers: {
            "loki-byggkon": {
              url: url,
              transport: "streamable-http",
              env: {
                AUTH_HEADER: `Bearer ${tokenPlaceholder}`,
              },
            },
          },
        },
        null,
        2
      );

      // Continue
      document.getElementById("mcp-continue-cfg").textContent = JSON.stringify(
        {
          experimental: {
            modelContextProtocolServers: [
              {
                transport: {
                  type: "streamable-http",
                  url: url,
                  headers: { Authorization: `Bearer ${tokenPlaceholder}` },
                },
                name: "loki-byggkon",
              },
            ],
          },
        },
        null,
        2
      );

      // Generic
      document.getElementById("mcp-generic-cfg").textContent = JSON.stringify(
        {
          name: "loki-byggkon",
          transport: "streamable-http",
          url: url,
          headers: {
            Authorization: `Bearer ${tokenPlaceholder}`,
            Accept: "application/json, text/event-stream",
          },
        },
        null,
        2
      );
    } catch (e) {
      console.warn("mcp info failed:", e);
    }
  }

  document.addEventListener("click", (e) => {
    const target = e.target.closest("[data-copy]");
    if (!target) return;
    const elId = target.dataset.copy;
    const el = document.getElementById(elId);
    if (!el) return;
    const text = el.textContent || "";
    navigator.clipboard
      .writeText(text)
      .then(() => {
        const orig = target.textContent;
        target.textContent = "Kopiert ✓";
        setTimeout(() => {
          target.textContent = orig;
        }, 1200);
      })
      .catch(() => {
        target.textContent = "Kunne ikke kopiere";
      });
  });

  // ─── Init ─────────────────────────────────────────────────────────
  loadStats();
  setInterval(loadStats, 30000);
})();
