const refreshSeconds = Number(window.TDARR_DASHBOARD_REFRESH || 20);
const themeStorageKey = "tdarr-dashboard-theme";

let previousSummary = null;

function getCurrentTheme() {
  return document.documentElement.dataset.theme === "light" ? "light" : "dark";
}

function getUrlTheme() {
  const urlTheme = new URLSearchParams(window.location.search).get("theme");
  return urlTheme === "light" || urlTheme === "dark" ? urlTheme : null;
}

function applyTheme(theme) {
  const nextTheme = theme === "light" ? "light" : "dark";
  document.documentElement.dataset.theme = nextTheme;

  const themeToggle = document.getElementById("theme-toggle");
  const themeLabel = document.getElementById("theme-toggle-label");
  if (!themeToggle || !themeLabel) return;

  const nextLabel = nextTheme === "light" ? "Dark mode" : "Light mode";
  themeToggle.setAttribute("aria-pressed", String(nextTheme === "light"));
  themeToggle.setAttribute("aria-label", nextLabel);
  themeLabel.textContent = nextLabel;
}

function initializeThemeToggle() {
  const urlTheme = getUrlTheme();
  let savedTheme = null;
  try {
    savedTheme = localStorage.getItem(themeStorageKey);
  } catch (error) {
    savedTheme = null;
  }

  if (urlTheme) {
    applyTheme(urlTheme);
  } else if (savedTheme === "light" || savedTheme === "dark") {
    applyTheme(savedTheme);
  } else {
    applyTheme(getCurrentTheme());
  }

  document.getElementById("theme-toggle")?.addEventListener("click", () => {
    const nextTheme = getCurrentTheme() === "light" ? "dark" : "light";
    applyTheme(nextTheme);
    try {
      localStorage.setItem(themeStorageKey, nextTheme);
    } catch (error) {
      console.warn("Unable to persist theme preference.", error);
    }
  });
}

function clamp(value, min, max) {
  return Math.min(Math.max(value, min), max);
}

function formatNumber(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return new Intl.NumberFormat().format(Number(value));
}

function formatDecimal(value, digits = 1) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return Number(value).toFixed(digits);
}

function formatStorageParts(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) {
    return { primary: "--", unit: "GiB" };
  }

  const numeric = Number(value);
  if (Math.abs(numeric) >= 1024) {
    return { primary: formatDecimal(numeric / 1024, 2), unit: "TB" };
  }

  if (Math.abs(numeric) >= 100) {
    return { primary: formatDecimal(numeric, 0), unit: "GiB" };
  }

  return { primary: formatDecimal(numeric, 2), unit: "GiB" };
}

function formatStorageInline(value) {
  const parts = formatStorageParts(value);
  return `${parts.primary} ${parts.unit}`;
}

function formatPercentInline(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return `${Math.round(Number(value))}%`;
}

function formatScoreValue(value) {
  if (value === null || value === undefined || Number.isNaN(Number(value))) return "--";
  return String(Math.round(Number(value)));
}

function formatRelativeTime(iso) {
  if (!iso) return "--";
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return "--";

  const diffMinutes = Math.max(0, Math.round((Date.now() - then) / 60000));
  if (diffMinutes < 1) return "now";
  if (diffMinutes < 60) return `${diffMinutes}m ago`;

  const diffHours = Math.round(diffMinutes / 60);
  if (diffHours < 24) return `${diffHours}h ago`;

  const diffDays = Math.round(diffHours / 24);
  return `${diffDays}d ago`;
}

function formatAbsoluteTime(iso) {
  if (!iso) return "--";
  const date = new Date(iso);
  const dateLabel = new Intl.DateTimeFormat(undefined, {
    month: "long",
    day: "numeric",
    year: "numeric",
  }).format(date);
  const timeLabel = new Intl.DateTimeFormat(undefined, {
    hour: "numeric",
    minute: "2-digit",
  }).format(date);
  return `${dateLabel} ${timeLabel}`;
}

function setText(id, value) {
  const element = document.getElementById(id);
  if (element) element.textContent = value;
}

function setSplitMetric(primaryId, unitId, value) {
  const parts = formatStorageParts(value);
  setText(primaryId, parts.primary);
  setText(unitId, parts.unit);
}

function setScoreRing(id, rawValue) {
  const element = document.getElementById(id);
  if (!element) return;

  const numeric = Number(rawValue);
  const clamped = Number.isNaN(numeric) ? 0 : clamp(numeric, 0, 100);
  element.style.setProperty("--ring-angle", `${clamped * 3.6}deg`);
}

function setTelemetryWidth(id, value, maxValue) {
  const element = document.getElementById(id);
  if (!element) return;

  if (Number(value) <= 0) {
    element.style.width = "14%";
    return;
  }

  const max = Math.max(Number(maxValue) || 0, 1);
  const ratio = clamp((Number(value) || 0) / max, 0, 1);
  const width = 82 + Math.round(ratio * 10);
  element.style.width = `${width}%`;
}

function buildTrend(current, previous, formatter) {
  if (current === null || current === undefined || Number.isNaN(Number(current))) {
    return { text: "Awaiting live sync", className: "trend-neutral" };
  }

  if (previous === null || previous === undefined || Number.isNaN(Number(previous))) {
    return { text: "↑ 0 from last sync", className: "trend-neutral" };
  }

  const diff = Number(current) - Number(previous);
  if (diff > 0) {
    return {
      text: `↑ ${formatter(Math.abs(diff))} from last sync`,
      className: "trend-up",
    };
  }

  if (diff < 0) {
    return {
      text: `↓ ${formatter(Math.abs(diff))} from last sync`,
      className: "trend-down",
    };
  }

  return {
    text: "→ 0 from last sync",
    className: "trend-flat",
  };
}

function applyTrend(id, trend) {
  const element = document.getElementById(id);
  if (!element) return;

  element.textContent = trend.text;
  element.className = element.className
    .split(" ")
    .filter((name) => !name.startsWith("trend-"))
    .concat(trend.className)
    .join(" ");
}

function getStatusState(summary) {
  const queueCount = Number(summary.queue.transcodes.count || 0);
  const copyFailed = Number(summary.staging.copyFailed || 0);
  const activeWorkers =
    Number(summary.nodes.activeTranscodeWorkers || 0) +
    Number(summary.nodes.activeHealthcheckWorkers || 0);
  const dbStatus = String(summary.stats.dbLoadStatus || "Stable");

  if (copyFailed > 0) {
    return { label: "Attention", className: "status-pill status-pill-danger" };
  }

  if (queueCount > 0 && activeWorkers === 0) {
    return { label: "Idle", className: "status-pill status-pill-warn" };
  }

  if (dbStatus.toLowerCase() !== "stable") {
    return { label: dbStatus, className: "status-pill status-pill-warn" };
  }

  return { label: "Stable", className: "status-pill status-pill-ok" };
}

function getLoadPercent(summary) {
  const configured =
    Number(summary.nodes.configuredTranscodeWorkers || 0) +
    Number(summary.nodes.configuredHealthcheckWorkers || 0);
  const active =
    Number(summary.nodes.activeTranscodeWorkers || 0) +
    Number(summary.nodes.activeHealthcheckWorkers || 0);

  if (!configured) return 0;
  return clamp((active / configured) * 100, 0, 100);
}

function getScorePresentation(kind, rawValue) {
  const value = Number(rawValue);
  if (Number.isNaN(value)) {
    return {
      headline: "Waiting",
      copy: kind === "tdarr" ? "Awaiting live transcode score." : "Awaiting live health score.",
    };
  }

  if (kind === "tdarr") {
    if (value >= 90) return { headline: "Excellent", copy: "Strong throughput" };
    if (value >= 80) return { headline: "Very Good", copy: "Healthy conversion pace" };
    if (value >= 65) return { headline: "Good", copy: "Steady queue progress" };
    return { headline: "Watchlist", copy: "Throughput needs attention" };
  }

  if (value >= 88) return { headline: "Very Good", copy: "Minimal issues detected" };
  if (value >= 75) return { headline: "Good", copy: "A few signals to watch" };
  return { headline: "Watchlist", copy: "Health checks need attention" };
}

function showBanner(message) {
  const banner = document.getElementById("error-banner");
  if (!banner) return;
  banner.hidden = !message;
  banner.textContent = message || "";
}

function renderSummary(summary) {
  const previous = previousSummary;
  const queueCount = Number(summary.queue.transcodes.count || 0);
  const processedCount = Number(summary.stats.totalTranscodeCount || 0);
  const erroredCount = Number(summary.staging.copyFailed || 0);
  const savedGiB = Number(summary.stats.savedGiB || 0);
  const totalFiles = Number(summary.stats.totalFileCount || 0);
  const totalHealthChecks = Number(summary.stats.totalHealthCheckCount || 0);
  const dbQueue = Number(summary.stats.dbQueue || 0);
  const dbLoadPercent = getLoadPercent(summary);
  const status = getStatusState(summary);

  const statusPill = document.getElementById("status-pill");
  if (statusPill) {
    statusPill.className = status.className;
    statusPill.textContent = status.label;
  }

  showBanner("");

  setText("queue-count", formatNumber(queueCount));
  setText("processed-count", formatNumber(processedCount));
  setText("errored-count", formatNumber(erroredCount));
  setSplitMetric("saved-primary", "saved-unit", savedGiB);

  applyTrend(
    "queue-delta",
    buildTrend(queueCount, previous?.queue?.transcodes?.count, formatNumber)
  );
  applyTrend(
    "processed-delta",
    buildTrend(processedCount, previous?.stats?.totalTranscodeCount, formatNumber)
  );
  applyTrend(
    "errored-delta",
    buildTrend(erroredCount, previous?.staging?.copyFailed, formatNumber)
  );
  applyTrend(
    "saved-delta",
    buildTrend(savedGiB, previous?.stats?.savedGiB, formatStorageInline)
  );

  setText("telemetry-queue-value", formatNumber(queueCount));
  setText("telemetry-processed-value", formatNumber(processedCount));
  setText("telemetry-errored-value", formatNumber(erroredCount));

  applyTrend(
    "telemetry-queue-delta",
    buildTrend(queueCount, previous?.queue?.transcodes?.count, formatNumber)
  );
  applyTrend(
    "telemetry-processed-delta",
    buildTrend(processedCount, previous?.stats?.totalTranscodeCount, formatNumber)
  );
  applyTrend(
    "telemetry-errored-delta",
    buildTrend(erroredCount, previous?.staging?.copyFailed, formatNumber)
  );

  const telemetryMax = Math.max(queueCount, processedCount, erroredCount, 1);
  setTelemetryWidth("telemetry-queue-bar", queueCount, telemetryMax);
  setTelemetryWidth("telemetry-processed-bar", processedCount, telemetryMax);
  setTelemetryWidth("telemetry-errored-bar", erroredCount, telemetryMax);

  setText("score-tdarr-value", formatScoreValue(summary.stats.tdarrScore));
  setText("score-health-value", formatScoreValue(summary.stats.healthCheckScore));
  setScoreRing("score-tdarr-ring", summary.stats.tdarrScore);
  setScoreRing("score-health-ring", summary.stats.healthCheckScore);

  const tdarrScore = getScorePresentation("tdarr", summary.stats.tdarrScore);
  const healthScore = getScorePresentation("health", summary.stats.healthCheckScore);
  setText("score-tdarr-rating", tdarrScore.headline);
  setText("score-tdarr-copy", tdarrScore.copy);
  setText("score-health-rating", healthScore.headline);
  setText("score-health-copy", healthScore.copy);

  setText("files-value", formatNumber(totalFiles));
  setText("transcodes-value", formatNumber(processedCount));
  setText("health-value", formatNumber(totalHealthChecks));
  setText("db-queue-value", formatNumber(dbQueue));
  setText("db-load-value", formatPercentInline(dbLoadPercent));
  setText("last-sync-value", formatRelativeTime(summary.generatedAt));
  setText("last-sync-detail", formatAbsoluteTime(summary.generatedAt));
  setText("last-sync-delta", "");

  applyTrend(
    "files-delta",
    buildTrend(totalFiles, previous?.stats?.totalFileCount, formatNumber)
  );
  applyTrend(
    "transcodes-delta",
    buildTrend(processedCount, previous?.stats?.totalTranscodeCount, formatNumber)
  );
  applyTrend(
    "health-delta",
    buildTrend(totalHealthChecks, previous?.stats?.totalHealthCheckCount, formatNumber)
  );
  applyTrend(
    "db-queue-delta",
    buildTrend(dbQueue, previous?.stats?.dbQueue, formatNumber)
  );
  applyTrend(
    "db-load-delta",
    buildTrend(dbLoadPercent, previous ? getLoadPercent(previous) : null, formatPercentInline)
  );

  const warningNote = (summary.notes || []).find((note) => note.level === "warning" || note.level === "danger");
  if (warningNote) {
    showBanner(warningNote.message);
  }

  previousSummary = summary;
}

async function loadSummary({ manual = false } = {}) {
  const refreshButton = document.getElementById("refresh-button");

  try {
    if (manual && refreshButton) {
      refreshButton.classList.add("is-loading");
      refreshButton.querySelector("span").textContent = "Refreshing";
    }

    const response = await fetch("/api/dashboard", { cache: "no-store" });
    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const summary = await response.json();
    renderSummary(summary);
  } catch (error) {
    console.error(error);

    const statusPill = document.getElementById("status-pill");
    if (statusPill) {
      statusPill.className = "status-pill status-pill-danger";
      statusPill.textContent = "Error";
    }

    showBanner(`Dashboard refresh failed: ${error.message}`);
  } finally {
    if (refreshButton) {
      refreshButton.classList.remove("is-loading");
      refreshButton.querySelector("span").textContent = "Refresh Data";
    }
  }
}

initializeThemeToggle();

document.getElementById("refresh-button")?.addEventListener("click", () => {
  loadSummary({ manual: true });
});

loadSummary();
setInterval(() => {
  loadSummary();
}, refreshSeconds * 1000);
