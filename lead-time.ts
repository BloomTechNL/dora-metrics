#!/usr/bin/env node
/**
 * Compute DORA "Lead Time for Changes" from GitHub Actions run history,
 * operationalized here as pipeline duration: the time from push (workflow
 * run created_at) to pipeline completion (run updated_at) for the "Meedoen"
 * workflow (.github/workflows/meedoen.yml) — the pipeline that builds,
 * tests, and deploys the app. This roughly corresponds to time between push
 * and deploy, given trunk-based CD. The "Proxy" workflow is ignored.
 *
 * Only successful runs are counted — a failed/cancelled run doesn't result
 * in a deploy, so it isn't a push-to-deploy duration.
 *
 * Uses the batch "list workflow runs" endpoint (up to 100 runs per request,
 * filtered server-side to status=success and branch=main) rather than
 * fetching runs one at a time. That endpoint silently caps pagination at
 * 1000 results per query — page 11 comes back empty even when more data
 * exists — so this recursively splits the created-date range in half
 * whenever a range's page count saturates the cap, until every leaf range
 * fits under it. At current volume (~3,300 successful runs) that's under 50
 * requests total across a handful of ranges, comfortably inside GitHub's
 * 5,000 req/hour authenticated rate limit — no sampling or throttling
 * needed.
 *
 * Always writes the raw per-run dataset to data/lead-time.csv — a gitignored
 * local cache the dashboard reads to recompute duration-per-period over
 * arbitrary windows without re-hitting the GitHub API.
 *
 * The GitHub owner/repo is derived from the `origin` remote of the git
 * checkout at GIT_REPO_PATH, rather than hardcoded — this script targets
 * whatever repo GIT_REPO_PATH points at.
 *
 * Usage:
 *   node --import tsx/esm lead-time.ts
 *
 * Required env vars:
 *   GITHUB_TOKEN
 *   GIT_REPO_PATH — local path to a git checkout whose "origin" remote
 *                   points at the GitHub repo to read Actions runs from
 */

import { execFileSync } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const GITHUB_API_BASE = 'https://api.github.com';
const WORKFLOW_FILE = 'meedoen.yml';
const BRANCH = 'main';
const DATA_DIR = join(import.meta.dirname, 'data');
const CSV_PATH = join(DATA_DIR, 'lead-time.csv');

interface WorkflowRun {
  id: number;
  created_at: string;
  updated_at: string;
}

interface Run {
  id: number;
  createdAt: Date;
  completedAt: Date;
  durationMinutes: number;
}

function requireEnv(name: string): string {
  const value = process.env[name];
  if (!value) {
    throw new Error(
      `Missing required environment variable ${name}. Set it in your environment ` +
        `(shell profile or .env file) before running this script.`,
    );
  }
  return value;
}

async function githubGet<T>(path: string, params: Record<string, string>, token: string): Promise<T> {
  const url = new URL(`${GITHUB_API_BASE}${path}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);

  const response = await fetch(url, {
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'X-GitHub-Api-Version': '2022-11-28',
    },
  });
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`GitHub API error ${response.status} for ${path}: ${body}`);
  }
  return (await response.json()) as T;
}

function getGitHubRepoSlug(repoPath: string): { owner: string; repo: string } {
  const remoteUrl = execFileSync('git', ['remote', 'get-url', 'origin'], { cwd: repoPath, encoding: 'utf-8' }).trim();
  const match = remoteUrl.match(/github\.com[:/]([^/]+)\/(.+?)(\.git)?$/);
  if (!match) {
    throw new Error(`Could not parse a GitHub owner/repo out of origin remote URL: ${remoteUrl}`);
  }
  return { owner: match[1], repo: match[2] };
}

async function findWorkflow(owner: string, repo: string, token: string): Promise<{ id: number; createdAt: Date }> {
  const { workflows } = await githubGet<{ workflows: Array<{ id: number; path: string; created_at: string }> }>(
    `/repos/${owner}/${repo}/actions/workflows`,
    {},
    token,
  );
  const workflow = workflows.find((w) => w.path === `.github/workflows/${WORKFLOW_FILE}`);
  if (!workflow) {
    throw new Error(
      `No workflow found with path .github/workflows/${WORKFLOW_FILE}. Available: ${workflows.map((w) => w.path).join(', ')}`,
    );
  }
  return { id: workflow.id, createdAt: new Date(workflow.created_at) };
}

const PAGE_CAP = 10; // GitHub silently stops paginating this endpoint after 1000 results (10 * 100/page)

async function fetchRunsInRange(
  since: Date,
  until: Date,
  owner: string,
  repo: string,
  workflowId: number,
  token: string,
): Promise<WorkflowRun[]> {
  const created = `${since.toISOString()}..${until.toISOString()}`;
  const runs: WorkflowRun[] = [];

  for (let page = 1; page <= PAGE_CAP; page++) {
    const { workflow_runs } = await githubGet<{ workflow_runs: WorkflowRun[] }>(
      `/repos/${owner}/${repo}/actions/workflows/${workflowId}/runs`,
      { status: 'success', branch: BRANCH, created, per_page: '100', page: String(page) },
      token,
    );
    runs.push(...workflow_runs);
    if (workflow_runs.length < 100) return runs; // exhausted this range, well under the cap
  }

  // Hit the 1000-result cap for this range: split it in half and recurse, since
  // there may be more runs in here than we were able to see.
  const mid = new Date(since.getTime() + (until.getTime() - since.getTime()) / 2);
  const [left, right] = await Promise.all([
    fetchRunsInRange(since, mid, owner, repo, workflowId, token),
    fetchRunsInRange(new Date(mid.getTime() + 1), until, owner, repo, workflowId, token),
  ]);
  return [...left, ...right];
}

async function getSuccessfulRuns(
  owner: string,
  repo: string,
  workflowId: number,
  workflowCreatedAt: Date,
  token: string,
): Promise<Run[]> {
  const rawRuns = await fetchRunsInRange(workflowCreatedAt, new Date(), owner, repo, workflowId, token);

  const byId = new Map<number, WorkflowRun>();
  for (const run of rawRuns) byId.set(run.id, run);

  return [...byId.values()].map((run) => {
    const createdAt = new Date(run.created_at);
    const completedAt = new Date(run.updated_at);
    return {
      id: run.id,
      createdAt,
      completedAt,
      durationMinutes: (completedAt.getTime() - createdAt.getTime()) / (1000 * 60),
    };
  });
}

function mean(values: number[]): number {
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

async function main() {
  const token = requireEnv('GITHUB_TOKEN');
  const { owner, repo } = getGitHubRepoSlug(requireEnv('GIT_REPO_PATH'));
  const workflow = await findWorkflow(owner, repo, token);
  const runs = await getSuccessfulRuns(owner, repo, workflow.id, workflow.createdAt, token);

  if (runs.length === 0) {
    console.log('No successful pipeline runs found.');
    return;
  }

  const durations = runs.map((r) => r.durationMinutes);
  console.log(`Successful pipeline runs: ${runs.length}`);
  console.log(`Median pipeline duration: ${median(durations).toFixed(1)} minutes`);
  console.log(
    `(mean: ${mean(durations).toFixed(1)} min, min: ${Math.min(...durations).toFixed(1)} min, max: ${Math.max(...durations).toFixed(1)} min)`,
  );

  await mkdir(DATA_DIR, { recursive: true });
  const header = 'run_id,created_at,completed_at,duration_minutes\n';
  const rows = runs
    .map((r) => `${r.id},${r.createdAt.toISOString()},${r.completedAt.toISOString()},${r.durationMinutes.toFixed(2)}`)
    .join('\n');
  await writeFile(CSV_PATH, header + rows + '\n', 'utf-8');
  console.log('');
  console.log(`Wrote raw data to ${CSV_PATH}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
