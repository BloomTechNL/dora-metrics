#!/usr/bin/env node
/**
 * Compute DORA "Deployment Frequency" from git history.
 *
 * This team does trunk-based development with continuous deployment: every
 * commit to `main` is a deploy. So deployment frequency is just how often
 * commits land on main — no Trello involved, unlike trello-mttr.ts /
 * trello-cfr.ts.
 *
 * Always writes the raw per-commit dataset to data/deploy-frequency.csv — a
 * gitignored local cache the dashboard reads to recompute deploys-per-period
 * over arbitrary windows without re-walking git history.
 *
 * Usage:
 *   node --import tsx/esm deploy-frequency.ts
 *
 * Required env var:
 *   GIT_REPO_PATH — local path to the git checkout to read commit history from
 */

import { execFileSync } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const FIELD_SEP = '\x1f';
const BRANCH = 'main';
const DATA_DIR = join(import.meta.dirname, 'data');
const CSV_PATH = join(DATA_DIR, 'deploy-frequency.csv');

interface Commit {
  hash: string;
  date: Date;
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

function getCommits(branch: string, cwd: string): Commit[] {
  const raw = execFileSync('git', ['log', branch, `--format=%H${FIELD_SEP}%cI`], {
    cwd,
    encoding: 'utf-8',
    maxBuffer: 1024 * 1024 * 64,
  });
  return raw
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const [hash, date] = line.split(FIELD_SEP);
      return { hash, date: new Date(date) };
    });
}

async function main() {
  const commits = getCommits(BRANCH, requireEnv('GIT_REPO_PATH'));
  const sorted = [...commits].sort((a, b) => a.date.getTime() - b.date.getTime());
  const first = sorted[0];
  const last = sorted[sorted.length - 1];
  const spanDays = (last.date.getTime() - first.date.getTime()) / (1000 * 60 * 60 * 24);
  const perDay = spanDays > 0 ? commits.length / spanDays : commits.length;

  console.log(`Deploys (commits on ${BRANCH}): ${commits.length}`);
  console.log(`First deploy: ${first.date.toISOString().slice(0, 10)}`);
  console.log(`Last deploy:  ${last.date.toISOString().slice(0, 10)}`);
  console.log(`Span: ${spanDays.toFixed(1)} days`);
  console.log(`Deployment frequency: ${perDay.toFixed(2)} deploys/day`);

  await mkdir(DATA_DIR, { recursive: true });
  const header = 'hash,date\n';
  const rows = commits.map((c) => `${c.hash},${c.date.toISOString()}`).join('\n');
  await writeFile(CSV_PATH, header + rows + '\n', 'utf-8');
  console.log('');
  console.log(`Wrote raw data to ${CSV_PATH}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
