/**
 * HomelabOps /mobile companion — thin client over existing APIs.
 * No business logic: topology, health, patcher, backup_verifier only.
 */
(function () {
  const DISK_WARN = 90;
  const CERT_WARN_DAYS = 14;
  const CONFIRM_PATCH =
    "Ich bestätige das Einspielen der Updates auf diesem Host. Es erfolgt kein automatischer Neustart.";
  const POWER_LABELS = {
    start: "starten",
    stop: "stoppen",
    shutdown: "herunterfahren",
    reboot: "neu starten",
  };

  const section = document.body.getAttribute("data-mobile-section") || "lage";
  const toastEl = document.getElementById("m-toast");
  const sheetEl = document.getElementById("m-sheet");
  const sheetBody = document.getElementById("m-sheet-body");

  function esc(s) {
    return String(s == null ? "" : s)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function toast(text, isError) {
    if (!toastEl) return;
    toastEl.textContent = text || "";
    toastEl.hidden = !text;
    toastEl.classList.toggle("is-error", !!isError);
    if (text) {
      clearTimeout(toast._t);
      toast._t = setTimeout(() => {
        toastEl.hidden = true;
      }, 4200);
    }
  }

  async function parseError(res) {
    try {
      const d = await res.json();
      if (typeof d.detail === "string") return d.detail;
      if (Array.isArray(d.detail)) return d.detail.map((x) => x.msg || x).join(" ");
      return d.message || res.statusText || "Fehler";
    } catch (_) {
      return res.statusText || "Fehler";
    }
  }

  async function fetchJSON(url, opts) {
    const res = await fetch(url, opts);
    if (!res.ok) throw new Error(await parseError(res));
    return res.json();
  }

  async function fetchSoft(url) {
    try {
      const res = await fetch(url);
      if (!res.ok) return null;
      return await res.json();
    } catch (_) {
      return null;
    }
  }

  function statusOf(ent) {
    const s = ent && ent.status;
    return typeof s === "string" ? s : (s && s.value) || "unknown";
  }

  function kindOf(ent) {
    const k = ent && ent.kind;
    return typeof k === "string" ? k : (k && k.value) || "";
  }

  function firstIp(ent) {
    const ips = (ent && ent.ip_addresses) || [];
    return ips[0] || "";
  }

  function diskPct(ent) {
    const meta = (ent && ent.meta) || {};
    const raw = meta.disk_pct;
    const n = Number(raw);
    return Number.isFinite(n) ? n : null;
  }

  function isDown(ent) {
    const st = statusOf(ent);
    return st !== "running" && st !== "paused";
  }

  function kindLabel(kind) {
    if (kind === "lxc") return "LXC";
    if (kind === "qemu") return "VM";
    if (kind === "host") return "Linux";
    if (kind === "node") return "Node";
    return kind || "Host";
  }

  function chip(cls, text) {
    return '<span class="chip ' + cls + '">' + esc(text) + "</span>";
  }

  function closeSheet() {
    if (!sheetEl) return;
    sheetEl.hidden = true;
    if (sheetBody) sheetBody.innerHTML = "";
  }

  function openSheet(html) {
    if (!sheetEl || !sheetBody) return;
    sheetBody.innerHTML = html;
    sheetEl.hidden = false;
    const focus = sheetBody.querySelector("button, a, [tabindex]");
    if (focus) focus.focus();
  }

  function confirmSheet({ title, body, caption, confirmLabel, danger, onConfirm }) {
    openSheet(
      '<h2 id="m-sheet-title" class="m-sheet-title">' +
        esc(title) +
        "</h2>" +
        '<p class="m-sheet-text">' +
        esc(body) +
        "</p>" +
        (caption ? '<p class="m-card-meta">' + esc(caption) + "</p>" : "") +
        '<div class="m-sheet-actions">' +
        '<button type="button" class="btn ' +
        (danger ? "btn-danger" : "btn-primary") +
        '" data-m-confirm="ok">' +
        esc(confirmLabel) +
        "</button>" +
        '<button type="button" class="btn btn-ghost" data-m-confirm="cancel">Abbrechen</button>' +
        "</div>"
    );
    const ok = sheetBody.querySelector('[data-m-confirm="ok"]');
    const cancel = sheetBody.querySelector('[data-m-confirm="cancel"]');
    cancel?.addEventListener("click", closeSheet);
    ok?.addEventListener("click", async () => {
      ok.disabled = true;
      try {
        await onConfirm();
        closeSheet();
      } catch (e) {
        toast(String(e.message || e), true);
        ok.disabled = false;
      }
    });
  }

  async function pollJob(url, { done } = {}) {
    for (let i = 0; i < 90; i++) {
      await new Promise((r) => setTimeout(r, 2000));
      const data = await fetchSoft(url);
      const job = data && (data.job || data);
      if (!job) continue;
      const st = job.status || "";
      if (job.done || st === "success" || st === "partial" || st === "failed") {
        if (done) done(job);
        return job;
      }
    }
    return null;
  }

  function listEntities(topo) {
    const guests = (topo && topo.guests) || [];
    const hosts = (topo && topo.hosts) || [];
    return guests.concat(hosts);
  }

  function targetMap(patcher) {
    const map = new Map();
    for (const t of (patcher && patcher.targets) || []) {
      map.set(t.id, t);
    }
    return map;
  }

  function latestRunByStack(runs) {
    const map = new Map();
    for (const run of runs || []) {
      const key = String(run.parent_id || "") + "::" + String(run.stack || "");
      if (!map.has(key)) map.set(key, run);
    }
    return map;
  }

  function jobByStack(jobs) {
    const map = new Map();
    for (const job of jobs || []) {
      if (!job || job.done) continue;
      const key = String(job.parent_id || "") + "::" + String(job.project || "");
      map.set(key, job);
    }
    return map;
  }

  function lageModel(topo, checks, stacks, runs, jobs, patcher) {
    const entities = listEntities(topo);
    const nodes = (topo && topo.nodes) || [];
    const down = entities.filter(isDown);
    const lastRuns = latestRunByStack(runs);
    const active = jobByStack(jobs);
    let backupFail = 0;
    const failNames = [];
    for (const st of stacks || []) {
      const key = st.parent_id + "::" + st.stack;
      if (active.has(key)) continue;
      const run = lastRuns.get(key);
      if (run && run.status === "failed") {
        backupFail += 1;
        failNames.push(st.stack);
      }
    }
    const diskEnts = entities.concat(nodes).filter((e) => {
      const pct = diskPct(e);
      return pct != null && pct >= DISK_WARN;
    });
    const certs = (checks || []).filter(
      (c) => c.cert_days_left != null && Number(c.cert_days_left) <= CERT_WARN_DAYS
    );
    const updateHosts = ((patcher && patcher.targets) || []).filter(
      (t) => t.monitored !== false && Number(t.pending) > 0
    );
    const pendingSum = updateHosts.reduce((n, t) => n + Number(t.pending || 0), 0);
    return {
      refreshed: (topo && (topo.refreshed_at || topo.time)) || "",
      down,
      backupFail,
      failNames,
      diskEnts,
      certs,
      updateHosts,
      pendingSum,
    };
  }

  function renderLage(model) {
    const grid = document.getElementById("m-lage-grid");
    const pills = document.getElementById("m-lage-pills");
    const stamp = document.getElementById("m-lage-stamp");
    if (stamp) {
      stamp.textContent = model.refreshed
        ? "Zuletzt: " + model.refreshed
        : "Noch keine Discovery.";
    }
    if (!grid) return;
    const cards = [
      {
        n: model.down.length,
        label: "Hosts down",
        cap: model.down.map((e) => e.name).slice(0, 3).join(", ") || "—",
        tone: model.down.length ? "danger" : "ok",
      },
      {
        n: model.backupFail,
        label: "Backup fehlgeschlagen",
        cap: model.failNames.slice(0, 3).join(", ") || "—",
        tone: model.backupFail ? "warn" : "ok",
      },
      {
        n: model.diskEnts.length,
        label: "Disk kritisch",
        cap: model.diskEnts.map((e) => e.name).slice(0, 3).join(", ") || "—",
        tone: model.diskEnts.length ? "warn" : "ok",
      },
      {
        n: model.certs.length,
        label: "Zertifikat",
        cap: model.certs.map((c) => c.label || c.url).slice(0, 3).join(", ") || "—",
        tone: model.certs.length ? "warn" : "ok",
      },
      {
        n: model.pendingSum,
        label: "Updates bereit",
        cap: model.updateHosts.map((t) => t.name).slice(0, 4).join(", ") || "—",
        tone: model.pendingSum ? "ochre" : "ok",
        wide: true,
      },
    ];
    grid.innerHTML = cards
      .map(
        (c) =>
          '<article class="m-stat m-stat-' +
          c.tone +
          (c.wide ? " m-stat-wide" : "") +
          '">' +
          '<p class="m-stat-n">' +
          esc(c.n) +
          "</p>" +
          '<p class="m-stat-label">' +
          esc(c.label) +
          "</p>" +
          '<p class="m-stat-cap">' +
          esc(c.cap) +
          "</p></article>"
      )
      .join("");
    const crit = model.down.length + model.backupFail + model.diskEnts.length + model.certs.length;
    if (pills) {
      pills.innerHTML =
        '<span class="m-pill m-pill-danger">' +
        crit +
        " kritisch</span>" +
        '<span class="m-pill m-pill-ok">' +
        (crit ? "Rest ok" : "Alles ruhig") +
        "</span>";
    }
  }

  function waitingWaveItems(agent) {
    const items = (agent && agent.wave && agent.wave.items) || [];
    return items.filter((it) => {
      const st = it.status || "";
      return (
        st === "waiting_confirm" ||
        st === "blocked" ||
        (it.needs_confirm && !it.confirmed && st !== "success" && st !== "failed" && st !== "skipped")
      );
    });
  }

  function renderWaveLage(agent) {
    const root = document.getElementById("m-wave");
    if (!root) return;
    if (!agent || !agent.enabled || !agent.wave) {
      root.hidden = true;
      root.innerHTML = "";
      return;
    }
    const wave = agent.wave;
    const waiting = waitingWaveItems(agent);
    root.hidden = false;
    root.innerHTML =
      '<article class="m-card">' +
      '<h2 class="m-card-title">' +
      esc(wave.banner || "Welle") +
      "</h2>" +
      '<p class="m-card-meta">' +
      esc(wave.status === "waiting" ? "Wartet auf Bestätigung" : wave.status || "") +
      "</p>" +
      (waiting.length
        ? '<button type="button" class="btn btn-primary" data-m-wave-confirm-all="1">Diese bestätigen</button>'
        : "") +
      "</article>" +
      waiting
        .map(
          (it) =>
            '<article class="m-card">' +
            '<p class="m-card-title">' +
            esc(it.target_name || it.target_id) +
            " · " +
            esc(it.bucket === "security" ? "Security" : it.bucket === "images" ? "Images" : "Bestätigung") +
            "</p>" +
            '<p class="m-card-meta">' +
            esc(it.explanation || "Wartet auf Bestätigung.") +
            "</p>" +
            '<button type="button" class="btn btn-primary" data-m-wave-confirm="' +
            esc(it.id) +
            '">Bestätigen</button></article>'
        )
        .join("");
    root.querySelector("[data-m-wave-confirm-all]")?.addEventListener("click", () => {
      confirmWaveItems({ all_waiting: true });
    });
    root.querySelectorAll("[data-m-wave-confirm]").forEach((btn) => {
      btn.addEventListener("click", () => {
        confirmWaveItems({ item_ids: [Number(btn.getAttribute("data-m-wave-confirm"))] });
      });
    });
  }

  function renderOpsLage(ops) {
    const root = document.getElementById("m-ops");
    if (!root) return;
    if (!ops) {
      root.hidden = true;
      root.innerHTML = "";
      return;
    }
    const next = (ops.next || []).slice(0, 5);
    const waiting = ops.waiting || [];
    const prompts = ops.scope_prompts || [];
    if (!next.length && !waiting.length && !prompts.length) {
      root.hidden = true;
      root.innerHTML = "";
      return;
    }
    root.hidden = false;
    const nextHtml = next
      .map(
        (w) =>
          '<article class="m-card">' +
          '<p class="m-card-title">' +
          esc((w.target_name || w.target_id || "") + (w.stack ? " · " + w.stack : "")) +
          "</p>" +
          '<p class="m-card-meta">' +
          esc((w.kind_label || w.kind || "") + " · " + (w.start_de || w.start_hm || "") + " · " + (w.duration_label || "")) +
          "</p></article>"
      )
      .join("");
    const waitHtml = waiting
      .map(
        (w) =>
          '<article class="m-card">' +
          '<p class="m-card-title">' +
          esc((w.target_name || "") + (w.stack ? " · " + w.stack : "")) +
          "</p>" +
          '<p class="m-card-meta">' +
          esc(w.reason || "Wartet auf Bestätigung.") +
          "</p>" +
          '<button type="button" class="btn btn-primary" data-m-ops-confirm="' +
          esc(w.id) +
          '">Bestätigen</button></article>'
      )
      .join("");
    const scope = ops.scope || {};
    const nPatch = (scope.patch_ids || []).length;
    const nImg = (scope.image_ids || []).length;
    const nAsk = (ops.scope_prompts || []).length;
    const scopeLine =
      "Patchen: " +
      nPatch +
      " · Images: " +
      nImg +
      (nAsk ? " · " + nAsk + " Host-Frage(n)" : "");
    root.innerHTML =
      '<article class="m-card"><h2 class="m-card-title">Nächste Fenster</h2>' +
      '<p class="m-card-meta">' + esc(scopeLine) + '</p>' +
      '<p class="m-card-meta">' +
      '<a href="/ops">Lage</a> · ' +
      '<a href="/ops/hosts">Hosts</a> · ' +
      '<a href="/ops/log">Log</a> · ' +
      '<a href="/ops/regeln">Regeln</a>' +
      "</p></article>" +
      nextHtml +
      waitHtml;
    root.querySelectorAll("[data-m-ops-confirm]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        try {
          await fetchJSON("/api/modules/ops_agent/confirm", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ window_id: Number(btn.getAttribute("data-m-ops-confirm")) }),
          });
          loadSection();
        } catch (e) {
          toast(String(e.message || e), true);
        }
      });
    });
  }

  async function confirmWaveItems(body) {
    confirmSheet({
      title: "Welle bestätigen?",
      body: "Nicht-Security und blockierte Positionen werden über die bestehende Apply-Pipeline eingespielt (Snapshot zuerst).",
      confirmLabel: "Bestätigen",
      async onConfirm() {
        await fetchJSON("/api/modules/patcher/agent/confirm", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        toast("Bestätigt.");
        await loadSection();
      },
    });
  }

  function hostSubtitle(ent) {
    const kind = kindLabel(kindOf(ent));
    const os = ((ent.meta || {}).ostype || (ent.meta || {}).os || ent.version || "").toString();
    const ip = firstIp(ent);
    return [kind, os, ip].filter(Boolean).join(" · ");
  }

  function renderHosts(topo, patcher, checks, agent) {
    const root = document.getElementById("m-hosts-list");
    const stamp = document.getElementById("m-hosts-stamp");
    const entities = listEntities(topo);
    const targets = targetMap(patcher);
    const downN = entities.filter(isDown).length;
    if (stamp) {
      stamp.textContent = entities.length
        ? entities.length + " Systeme · " + downN + " down"
        : "Keine Hosts in der Topologie.";
    }
    if (!root) return;
    if (!entities.length) {
      root.innerHTML = '<p class="m-empty">Noch keine Discovery. Am Desktop aktualisieren.</p>';
      return;
    }
    root.innerHTML = entities
      .map((ent) => {
        const t = targets.get(ent.id);
        const st = statusOf(ent);
        const down = isDown(ent);
        const pending = t && Number(t.pending) > 0 ? Number(t.pending) : 0;
        const chips =
          (down ? chip("chip-stopped", "Down") : chip("chip-running", "Läuft")) +
          (t && t.monitored === false
            ? chip("chip-unknown", "Nicht überwacht")
            : chip("chip-monitor", "Monitor")) +
          (pending ? chip("chip-pending", pending + " Updates") : "");
        return (
          '<button type="button" class="m-card m-card-btn" data-host-id="' +
          esc(ent.id) +
          '"><span class="m-card-title">' +
          esc(ent.name) +
          "</span><span class=\"m-card-meta\">" +
          esc(hostSubtitle(ent)) +
          '</span><span class="m-chip-row">' +
          chips +
          "</span></button>"
        );
      })
      .join("");

    root.querySelectorAll("[data-host-id]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const id = btn.getAttribute("data-host-id");
        const ent = entities.find((e) => e.id === id);
        if (ent) openHostSheet(ent, targets.get(id), checks, agent);
      });
    });
  }

  function lastCheckText(ent, target, checks) {
    if (target && target.last_scan && target.last_scan.created_at) {
      return "Letzte Prüfung " + target.last_scan.created_at;
    }
    const name = (ent.name || "").toLowerCase();
    const match = (checks || []).find((c) => {
      const label = String(c.label || "").toLowerCase();
      return label && (label === name || label.includes(name));
    });
    if (match && match.last_checked_at) return "Letzte Prüfung " + match.last_checked_at;
    if (ent.discovered_at) return "Discovery " + ent.discovered_at;
    return "Keine Prüfung gespeichert";
  }

  function openHostSheet(ent, target, checks, agent) {
    const kind = kindOf(ent);
    const canPower = kind === "lxc" || kind === "qemu";
    const running = statusOf(ent) === "running";
    const pending = target && Number(target.pending) > 0 ? Number(target.pending) : 0;
    const powerBtns = canPower
      ? '<div class="m-sheet-actions m-sheet-actions-row">' +
        '<button type="button" class="btn btn-primary" data-m-power="start"' +
        (running ? " disabled" : "") +
        ">Starten</button>" +
        '<button type="button" class="btn btn-ghost" data-m-power="shutdown"' +
        (running ? "" : " disabled") +
        ">Stoppen</button></div>"
      : '<p class="m-card-meta">Power nur für LXC/VM über Proxmox.</p>';
    const waveWaiting = waitingWaveItems(agent).filter((it) => it.target_id === ent.id);
    const waveBlock = waveWaiting.length
      ? '<p class="m-sheet-text">' +
        esc(waveWaiting[0].explanation || "Welle wartet auf Bestätigung.") +
        '</p><button type="button" class="btn btn-primary" data-m-wave-one="' +
        waveWaiting[0].id +
        '">Bestätigen</button>'
      : "";
    const patchBlock = pending
      ? '<p class="m-sheet-text">' +
        pending +
        " Updates bereit.</p>" +
        '<button type="button" class="btn btn-primary" data-m-patch="1">Einspielen</button>'
      : '<p class="m-card-meta">Keine ausstehenden Updates (letzter Scan).</p>';

    openSheet(
      '<h2 id="m-sheet-title" class="m-sheet-title">' +
        esc(ent.name) +
        "</h2>" +
        '<p class="m-card-meta">' +
        esc(hostSubtitle(ent)) +
        "</p>" +
        '<div class="m-chip-row">' +
        (running ? chip("chip-running", "Läuft") : chip("chip-stopped", "Down")) +
        "</div>" +
        '<p class="m-card-meta">' +
        esc(lastCheckText(ent, target, checks)) +
        "</p>" +
        powerBtns +
        waveBlock +
        patchBlock +
        '<a class="m-text-link" href="/?guest=' +
        encodeURIComponent(ent.id) +
        '">In Desktop öffnen</a>'
    );

    sheetBody.querySelectorAll("[data-m-power]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const action = btn.getAttribute("data-m-power");
        requestPower(ent, action);
      });
    });
    sheetBody.querySelector("[data-m-patch]")?.addEventListener("click", () => {
      requestPatch(ent, target);
    });
    sheetBody.querySelector("[data-m-wave-one]")?.addEventListener("click", () => {
      const id = Number(sheetBody.querySelector("[data-m-wave-one]").getAttribute("data-m-wave-one"));
      confirmWaveItems({ item_ids: [id] });
    });
  }

  function requestPower(ent, action) {
    const verb = POWER_LABELS[action] || action;
    confirmSheet({
      title: "Power",
      body: "„" + ent.name + "“ wirklich " + verb + "?",
      caption: "Wie am Desktop: Start/Stop über Proxmox.",
      confirmLabel: verb.charAt(0).toUpperCase() + verb.slice(1),
      danger: action !== "start",
      async onConfirm() {
        const data = await fetchJSON(
          "/api/guests/" + encodeURIComponent(ent.id) + "/power/" + encodeURIComponent(action),
          { method: "POST" }
        );
        toast(data.message || ent.name + " · " + verb);
        await loadSection();
      },
    });
  }

  function requestPatch(ent, target) {
    if (!target) return;
    confirmSheet({
      title: "Updates einspielen?",
      body: CONFIRM_PATCH,
      caption: ent.name + " · " + Number(target.pending || 0) + " Pakete · Snapshot zuerst",
      confirmLabel: "Einspielen",
      async onConfirm() {
        const data = await fetchJSON("/api/modules/patcher/apply", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            target_id: target.id,
            confirm: true,
            snapshot_first: true,
            reboot_after: false,
          }),
        });
        toast(data.message || "Einspielen gestartet.");
        const jobId = data.job_id || data.id;
        if (jobId) {
          pollJob("/api/modules/patcher/jobs/" + encodeURIComponent(jobId), {
            done(job) {
              toast(
                job.status === "failed"
                  ? job.error || "Einspielen fehlgeschlagen"
                  : "Updates eingespielt.",
                job.status === "failed"
              );
            },
          });
        }
      },
    });
  }

  function hinweiseItems(model, checks, runs) {
    const items = [];
    for (const ent of model.down) {
      items.push({
        tone: "danger",
        title: ent.name + " down",
        meta: kindLabel(kindOf(ent)) + " · " + (ent.discovered_at || "Topologie"),
      });
    }
    for (const c of checks || []) {
      if (c.last_status === "down") {
        items.push({
          tone: "danger",
          title: (c.label || c.url) + " down",
          meta: "Health-Check · " + (c.last_checked_at || "jetzt"),
        });
      } else if (c.cert_days_left != null && Number(c.cert_days_left) <= CERT_WARN_DAYS) {
        items.push({
          tone: "warn",
          title: "Zertifikat " + (c.label || c.url),
          meta: c.cert_days_left + " Tage · " + (c.last_checked_at || ""),
        });
      }
    }
    for (const ent of model.diskEnts) {
      items.push({
        tone: "warn",
        title: "Disk kritisch · " + ent.name,
        meta: diskPct(ent) + " %",
      });
    }
    const seenFail = new Set();
    for (const run of runs || []) {
      if (run.status !== "failed") continue;
      const key = (run.stack || "") + "::" + (run.parent_id || "");
      if (seenFail.has(key)) continue;
      seenFail.add(key);
      items.push({
        tone: "warn",
        title: "Backup fehlgeschlagen",
        meta: (run.stack || "Stack") + " · " + (run.created_at || ""),
      });
    }
    for (const t of model.updateHosts) {
      items.push({
        tone: "ochre",
        title: t.pending + " Updates bereit",
        meta: t.name + (t.last_scan && t.last_scan.created_at ? " · " + t.last_scan.created_at : ""),
      });
    }
    return items;
  }

  function renderHinweise(items) {
    const root = document.getElementById("m-hinweise-list");
    if (!root) return;
    if (!items.length) {
      root.innerHTML = '<p class="m-empty">Keine Hinweise. Alles ruhig.</p>';
      return;
    }
    root.innerHTML = items
      .map(
        (it) =>
          '<article class="m-card m-card-' +
          it.tone +
          '"><p class="m-card-title">' +
          esc(it.title) +
          '</p><p class="m-card-meta">' +
          esc(it.meta) +
          "</p></article>"
      )
      .join("");
  }

  function runStatus(run, job) {
    if (job && !job.done) return { label: "Läuft…", cls: "chip-info", running: true };
    if (!run) return { label: "Kein Lauf", cls: "chip-unknown", running: false };
    if (run.status === "success") return { label: "OK", cls: "chip-running", running: false };
    if (run.status === "partial") return { label: "Teilweise", cls: "chip-partial", running: false };
    if (run.status === "failed") return { label: "Fehlgeschlagen", cls: "chip-stopped", running: false };
    if (run.status === "running") return { label: "Läuft…", cls: "chip-info", running: true };
    return { label: run.status || "—", cls: "chip-unknown", running: false };
  }

  function renderSichern(stacks, runs, jobs) {
    const root = document.getElementById("m-sichern-list");
    if (!root) return;
    if (!(stacks || []).length) {
      root.innerHTML = '<p class="m-empty">Keine Compose-Stacks gefunden.</p>';
      return;
    }
    const last = latestRunByStack(runs);
    const active = jobByStack(jobs);
    root.innerHTML = stacks
      .map((st) => {
        const key = st.parent_id + "::" + st.stack;
        const run = last.get(key);
        const job = active.get(key);
        const rs = runStatus(run, job);
        const when = job && job.phase ? job.phase : run && run.created_at ? run.created_at : "noch nie";
        return (
          '<article class="m-card">' +
          '<p class="m-card-title">' +
          esc(st.stack) +
          "</p>" +
          '<p class="m-card-meta">' +
          esc(st.guest_name || st.parent_id) +
          " · " +
          esc(when) +
          '</p><div class="m-chip-row">' +
          chip(rs.cls, rs.label) +
          "</div>" +
          '<button type="button" class="btn btn-primary m-card-cta" data-backup-parent="' +
          esc(st.parent_id) +
          '" data-backup-stack="' +
          esc(st.stack) +
          '"' +
          (rs.running ? " disabled" : "") +
          ">Backup starten</button></article>"
        );
      })
      .join("");

    root.querySelectorAll("[data-backup-stack]").forEach((btn) => {
      btn.addEventListener("click", () => {
        const parentId = btn.getAttribute("data-backup-parent");
        const project = btn.getAttribute("data-backup-stack");
        confirmSheet({
          title: "Backup starten?",
          body: "Backup für Stack „" + project + "“ jetzt starten?",
          caption: "Läuft im Hintergrund. Kein Wipe, kein Browse.",
          confirmLabel: "Jetzt starten",
          async onConfirm() {
            const data = await fetchJSON("/api/modules/backup_verifier/run", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ parent_id: parentId, project: project }),
            });
            toast(data.message || "Backup gestartet.");
            if (data.job_id) {
              pollJob("/api/modules/backup_verifier/jobs/" + encodeURIComponent(data.job_id), {
                done(job) {
                  toast(
                    job.status === "failed"
                      ? job.error || "Backup fehlgeschlagen"
                      : "Backup " + (job.status === "partial" ? "teilweise" : "fertig") + ".",
                    job.status === "failed"
                  );
                  loadSection();
                },
              });
            }
            loadSection();
          },
        });
      });
    });
  }

  async function loadBundle() {
    const [topo, health, stacks, history, backupJobs, patcher, agent, ops] = await Promise.all([
      fetchSoft("/api/topology"),
      fetchSoft("/api/modules/health/checks"),
      fetchSoft("/api/modules/backup_verifier/stacks"),
      fetchSoft("/api/modules/backup_verifier/history?limit=80"),
      fetchSoft("/api/modules/backup_verifier/jobs?active=true"),
      fetchSoft("/api/modules/patcher/targets"),
      fetchSoft("/api/modules/patcher/agent"),
      fetchSoft("/api/modules/ops_agent/board"),
    ]);
    return {
      topo: topo || {},
      checks: (health && health.checks) || [],
      stacks: (stacks && stacks.stacks) || [],
      runs: (history && history.runs) || [],
      jobs: (backupJobs && backupJobs.jobs) || [],
      patcher: patcher || { targets: [] },
      agent: agent || { enabled: false, wave: null },
      ops: ops || null,
    };
  }

  async function loadSection() {
    try {
      const b = await loadBundle();
      const model = lageModel(b.topo, b.checks, b.stacks, b.runs, b.jobs, b.patcher);
      if (section === "lage") {
        renderLage(model);
        renderWaveLage(b.agent);
        renderOpsLage(b.ops);
      }
      else if (section === "hosts") renderHosts(b.topo, b.patcher, b.checks, b.agent);
      else if (section === "hinweise") renderHinweise(hinweiseItems(model, b.checks, b.runs));
      else if (section === "sichern") renderSichern(b.stacks, b.runs, b.jobs);
    } catch (e) {
      toast(String(e.message || e), true);
    }
  }

  document.getElementById("m-refresh")?.addEventListener("click", async () => {
    try {
      await fetch("/api/guests/live-status", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: "{}",
      });
    } catch (_) {}
    loadSection();
  });
  document.getElementById("m-sheet-backdrop")?.addEventListener("click", closeSheet);
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape") closeSheet();
  });
  document.getElementById("m-logout")?.addEventListener("click", async () => {
    try {
      await fetchJSON("/api/auth/logout", { method: "POST" });
    } catch (_) {
      /* still leave */
    }
    location.href = "/auth/login?next=" + encodeURIComponent("/mobile");
  });

  if (section !== "mehr") loadSection();
})();
