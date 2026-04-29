const codeInput = document.getElementById("codeInput");
const outputBox = document.getElementById("outputBox");
const fixedCodeBox = document.getElementById("fixedCodeBox");
const statusBadge = document.getElementById("statusBadge");
const jobInfo = document.getElementById("jobInfo");
const runBtn = document.getElementById("runBtn");
const fixBtn = document.getElementById("fixBtn");
const clearBtn = document.getElementById("clearBtn");
const sampleBtn = document.getElementById("sampleBtn");
const stopViewBtn = document.getElementById("stopViewBtn");

let currentJobId = null;
let pollTimer = null;
let pollingEnabled = true;

function setStatus(status, phase, returnCode) {
  statusBadge.className = `status-badge ${status || "idle"}`;
  const pretty = (status || "idle").toUpperCase();
  statusBadge.textContent = pretty;

  const extra = phase ? ` · ${phase}` : "";
  const rc = returnCode === null || returnCode === undefined ? "" : ` · exit code ${returnCode}`;
  jobInfo.textContent = `Job: ${currentJobId || "none"}${extra}${rc}`;
}

function setButtonsDisabled(disabled) {
  runBtn.disabled = disabled;
  fixBtn.disabled = disabled;
  clearBtn.disabled = disabled;
  sampleBtn.disabled = disabled;
}

function sampleCode() {
  return `print("Hello from the web runner!")
for i in range(5):
    print("tick", i)
`;
}

function scrollConsoleToBottom(el) {
  el.scrollTop = el.scrollHeight;
}

async function createJob(mode) {
  const code = codeInput.value.trim();

  if (!code) {
    outputBox.textContent = "Please paste some Python code first.";
    return;
  }

  setButtonsDisabled(true);
  outputBox.textContent = "Starting job...";
  fixedCodeBox.textContent = "Fixed code will appear here after auto-fix.";
  setStatus("running", "queued", null);

  try {
    const res = await fetch("/api/jobs", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ code, mode }),
    });

    const data = await res.json();

    if (!data.ok) {
      throw new Error(data.error || "Failed to create job.");
    }

    currentJobId = data.job_id;
    setStatus(mode === "fix" ? "fixing" : "running", "job created", null);
    outputBox.textContent = `Job created: ${currentJobId}\nWaiting for output...\n`;
    startPolling();
  } catch (err) {
    outputBox.textContent = `Error: ${err.message}`;
    setButtonsDisabled(false);
    setStatus("failed", "job creation failed", 1);
  }
}

function startPolling() {
  stopPolling();
  pollingEnabled = true;
  pollTimer = setInterval(pollJob, 700);
  pollJob();
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

async function pollJob() {
  if (!pollingEnabled || !currentJobId) return;

  try {
    const res = await fetch(`/api/jobs/${currentJobId}`);
    const data = await res.json();

    if (!data.ok) {
      throw new Error(data.error || "Job not found.");
    }

    outputBox.textContent = data.output || "No output yet...";
    scrollConsoleToBottom(outputBox);

    if (data.fixed_code) {
      fixedCodeBox.textContent = data.fixed_code;
    }

    setStatus(data.status, data.phase, data.return_code);

    if (data.finished) {
      stopPolling();
      setButtonsDisabled(false);
    }
  } catch (err) {
    outputBox.textContent = `Polling error: ${err.message}`;
    setButtonsDisabled(false);
    stopPolling();
    setStatus("failed", "polling error", 1);
  }
}

runBtn.addEventListener("click", () => createJob("run"));
fixBtn.addEventListener("click", () => createJob("fix"));

clearBtn.addEventListener("click", () => {
  codeInput.value = "";
  outputBox.textContent = "Your output will appear here.";
  fixedCodeBox.textContent = "Fixed code will appear here after auto-fix.";
  currentJobId = null;
  stopPolling();
  setButtonsDisabled(false);
  setStatus("idle", "ready", null);
});

sampleBtn.addEventListener("click", () => {
  codeInput.value = sampleCode();
});

stopViewBtn.addEventListener("click", () => {
  pollingEnabled = !pollingEnabled;
  if (pollingEnabled && currentJobId) {
    stopViewBtn.textContent = "Stop Polling";
    startPolling();
  } else {
    stopViewBtn.textContent = "Resume Polling";
    stopPolling();
  }
});

setStatus("idle", "ready", null);