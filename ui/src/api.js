// Talks to scripts/dev_server.py — real API, real graph, mock shot rendering, no real auth.
// The bearer token below isn't a credential: dev_server.py's AllowAllApiKeyVerifier accepts
// anything, but the route still requires *some* token at least 28 characters long to bother
// parsing it as one, so this exists to satisfy that shape.
const API_BASE = import.meta.env.VITE_API_BASE || "http://127.0.0.1:8000";
const DEV_TOKEN = "dev-no-auth-placeholder-token-000000";

function newIdempotencyKey() {
  return crypto.randomUUID();
}

async function request(path, options = {}) {
  const response = await fetch(`${API_BASE}${path}`, {
    ...options,
    headers: {
      Authorization: `Bearer ${DEV_TOKEN}`,
      "Content-Type": "application/json",
      ...options.headers,
    },
  });
  if (!response.ok) {
    let detail = `${response.status} ${response.statusText}`;
    try {
      const body = await response.json();
      detail = body?.error?.message || detail;
    } catch {
      // body wasn't JSON; the status line is all we have
    }
    throw new Error(detail);
  }
  if (response.status === 204) return null;
  return response.json();
}

export function createJob(prompt) {
  return request("/v1/jobs", {
    method: "POST",
    headers: { "Idempotency-Key": newIdempotencyKey() },
    body: JSON.stringify({ prompt }),
  });
}

export function listJobs() {
  return request("/v1/jobs");
}

export function getJob(jobId) {
  return request(`/v1/jobs/${jobId}`);
}

export function getArtifacts(jobId) {
  return request(`/v1/jobs/${jobId}/artifacts`);
}

export function cancelJob(jobId) {
  return request(`/v1/jobs/${jobId}/cancel`, {
    method: "POST",
    headers: { "Idempotency-Key": newIdempotencyKey() },
  });
}
