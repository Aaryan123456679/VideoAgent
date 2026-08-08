import { useCallback, useEffect, useState } from "react";
import { cancelJob, createJob, getArtifacts, getJob, listJobs } from "./api.js";

const POLL_MS = 3000;

function short(id) {
  return id ? id.slice(0, 8) : "";
}

export default function App() {
  const [prompt, setPrompt] = useState(
    "a violinist plays on a rooftop at dawn as the city wakes below"
  );
  const [creating, setCreating] = useState(false);
  const [createError, setCreateError] = useState(null);

  const [jobs, setJobs] = useState([]);
  const [listError, setListError] = useState(null);

  const [selectedId, setSelectedId] = useState(null);
  const [detail, setDetail] = useState(null);
  const [artifacts, setArtifacts] = useState(null);
  const [detailError, setDetailError] = useState(null);

  const refreshList = useCallback(async () => {
    try {
      const page = await listJobs();
      setJobs(page.jobs);
      setListError(null);
    } catch (err) {
      setListError(err.message);
    }
  }, []);

  const refreshDetail = useCallback(async (jobId) => {
    if (!jobId) return null;
    try {
      const job = await getJob(jobId);
      setDetail(job);
      setDetailError(null);
      return job;
    } catch (err) {
      setDetailError(err.message);
      return null;
    }
  }, []);

  useEffect(() => {
    refreshList();
    const id = setInterval(refreshList, POLL_MS);
    return () => clearInterval(id);
  }, [refreshList]);

  // Polls job status/current_node/budget while the job is still moving, and stops once it
  // reaches a terminal state — a terminal job never changes again, so there is nothing to
  // poll for, and polling anyway would only exist to cause the artifact refetch below.
  useEffect(() => {
    setDetail(null);
    setArtifacts(null);
    if (!selectedId) return undefined;

    let stopped = false;
    let intervalId = null;

    async function tick() {
      const job = await refreshDetail(selectedId);
      if (stopped) return;
      if (job && job.status === "terminal" && intervalId !== null) {
        clearInterval(intervalId);
        intervalId = null;
      }
    }

    tick();
    intervalId = setInterval(tick, POLL_MS);
    return () => {
      stopped = true;
      if (intervalId !== null) clearInterval(intervalId);
    };
  }, [selectedId, refreshDetail]);

  // Artifacts carry a freshly-minted presigned URL every time they're fetched (`[D-52]`: no
  // URL is ever stored, so there is no "the same" URL to re-request). Fetching them once per
  // terminal job — never on an interval — is what keeps a <video>'s `src` stable; refetching
  // on a timer would hand the element a new URL every tick and restart playback from zero.
  useEffect(() => {
    if (!selectedId || !detail || detail.status !== "terminal" || artifacts) return undefined;
    let cancelled = false;
    getArtifacts(selectedId)
      .then((list) => {
        if (!cancelled) setArtifacts(list.artifacts);
      })
      .catch((err) => {
        if (!cancelled) setDetailError(err.message);
      });
    return () => {
      cancelled = true;
    };
  }, [selectedId, detail, artifacts]);

  async function handleCreate(event) {
    event.preventDefault();
    setCreating(true);
    setCreateError(null);
    try {
      const accepted = await createJob(prompt);
      await refreshList();
      setSelectedId(accepted.job_id);
    } catch (err) {
      setCreateError(err.message);
    } finally {
      setCreating(false);
    }
  }

  async function handleCancel(jobId) {
    try {
      await cancelJob(jobId);
      await refreshDetail(jobId);
    } catch (err) {
      setDetailError(err.message);
    }
  }

  const finalVideo = artifacts?.find((a) => a.kind === "final_video");
  const thumbnail = artifacts?.find((a) => a.kind === "thumbnail");
  const shotClips = artifacts?.filter((a) => a.kind === "shot_clip") ?? [];

  return (
    <div className="page">
      <h1>Video Agent — trial UI</h1>
      <p className="subtitle">
        No auth, no polish — talks straight to the real API and graph, shots rendered by the
        mock provider.
      </p>

      <section className="panel">
        <h2>Create a video</h2>
        <form onSubmit={handleCreate}>
          <textarea
            rows={3}
            value={prompt}
            onChange={(e) => setPrompt(e.target.value)}
            minLength={8}
            maxLength={2000}
            required
          />
          <button type="submit" disabled={creating}>
            {creating ? "Submitting…" : "Create video"}
          </button>
        </form>
        {createError && <p className="error">{createError}</p>}
      </section>

      <div className="columns">
        <section className="panel jobs-list">
          <h2>Jobs</h2>
          {listError && <p className="error">{listError}</p>}
          <ul>
            {jobs.map((job) => (
              <li
                key={job.job_id}
                className={job.job_id === selectedId ? "selected" : ""}
                onClick={() => setSelectedId(job.job_id)}
              >
                <code>{short(job.job_id)}</code>
                <span className={`badge status-${job.status}`}>{job.status}</span>
                {job.outcome && <span className={`badge outcome-${job.outcome}`}>{job.outcome}</span>}
              </li>
            ))}
            {jobs.length === 0 && <li className="empty">No jobs yet.</li>}
          </ul>
        </section>

        <section className="panel job-detail">
          <h2>Job state</h2>
          {!selectedId && <p className="empty">Select a job to see its live state.</p>}
          {detailError && <p className="error">{detailError}</p>}
          {detail && (
            <>
              <dl>
                <dt>job_id</dt>
                <dd>
                  <code>{detail.job_id}</code>
                </dd>
                <dt>status</dt>
                <dd>{detail.status}</dd>
                <dt>current_node</dt>
                <dd>{detail.current_node}</dd>
                <dt>outcome</dt>
                <dd>{detail.outcome ?? "—"}</dd>
                <dt>degraded</dt>
                <dd>{String(detail.degraded)}{detail.degraded_reason ? ` (${detail.degraded_reason})` : ""}</dd>
                <dt>budget</dt>
                <dd>
                  {detail.budget.iterations_used}/{detail.budget.iterations_cap} iterations ·{" "}
                  {detail.budget.tokens_used}/{detail.budget.tokens_cap} tokens ·{" "}
                  ${detail.budget.usd_spent}/${detail.budget.usd_cap} ·{" "}
                  {detail.budget.wall_clock_s.toFixed(1)}s/{detail.budget.wall_clock_cap_s}s
                </dd>
                <dt>updated_at</dt>
                <dd>{detail.updated_at}</dd>
              </dl>
              {detail.status !== "terminal" && (
                <button onClick={() => handleCancel(detail.job_id)}>Cancel job</button>
              )}

              {finalVideo && (
                <div className="video-box">
                  <h3>Delivered video</h3>
                  <video controls src={finalVideo.url} poster={thumbnail?.url} width={480} />
                </div>
              )}

              {shotClips.length > 0 && (
                <div className="video-box">
                  <h3>Shot clips</h3>
                  <div className="clip-row">
                    {shotClips
                      .sort((a, b) => a.shot_index - b.shot_index)
                      .map((clip) => (
                        <video
                          key={clip.artifact_id}
                          controls
                          src={clip.url}
                          width={200}
                          title={`shot ${clip.shot_index}`}
                        />
                      ))}
                  </div>
                </div>
              )}
            </>
          )}
        </section>
      </div>
    </div>
  );
}
