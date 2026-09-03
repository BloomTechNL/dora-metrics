#!/usr/bin/env node
/**
 * Compute DORA "Change Failure Rate" from git history + the Trello board.
 *
 * This team does trunk-based development with continuous deployment: every
 * commit to `main` is a deploy. So, over the entire git history:
 *
 *   deploys  = total commits on main
 *   failures = (bug cards created) + (git revert commits)
 *   CFR      = failures / deploys
 *
 * Bug cards approximate "a deploy caused a production issue", even though we
 * can't trace a bug card back to the exact commit that caused it — this is an
 * aggregate-rate approximation, not a per-commit causal link. It also means a
 * single real incident could be double-counted if it produced both a bug card
 * AND a revert commit fixing it; there's no reliable way to de-duplicate that
 * here without an explicit link between Trello cards and commits.
 *
 * Revert commits are detected by a case-insensitive `^revert` subject-line
 * prefix. This repo's history mixes git's auto-generated `Revert "..."`
 * messages with hand-written ones ("Reverted logic for...", "revert infra for
 * now"), so a loose prefix match is used rather than only the strict
 * `git revert` format — this can occasionally catch a non-product revert
 * (e.g. "revert to green" for a CI state).
 *
 * Bug card creation time is derived from the Trello card id itself (same
 * trick as trello-mttr.ts: the first 8 hex chars of a Trello id are a Mongo
 * ObjectId timestamp). Only non-archived (open) cards are counted, for
 * consistency with trello-mttr.ts.
 *
 * Usage:
 *   node --import tsx/esm trello-cfr.ts
 *
 * Always writes the raw (per-commit, per-card) datasets to
 * data/cfr-commits.csv and data/cfr-bug-cards.csv — gitignored local caches
 * intended for a dashboard to recompute CFR over arbitrary windows without
 * re-hitting Trello or re-walking git history.
 *
 * Required env vars:
 *   TRELLO_API_KEY
 *   TRELLO_TOKEN
 *   TRELLO_BOARD_ID  — the Trello board to read bug cards from
 *   GIT_REPO_PATH    — local path to the git checkout to read commit history from
 */

import { execFileSync } from 'node:child_process';
import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const TRELLO_API_BASE = 'https://api.trello.com/1';
const FIELD_SEP = '\x1f';
const LABEL_NAME = 'bug';
const BRANCH = 'main';
const REVERT_REGEX = /^revert/i;
const DATA_DIR = join(import.meta.dirname, 'data');
const COMMITS_CSV_PATH = join(DATA_DIR, 'cfr-commits.csv');
const BUG_CARDS_CSV_PATH = join(DATA_DIR, 'cfr-bug-cards.csv');

interface TrelloLabel {
  id: string;
  name: string;
}

interface TrelloCard {
  id: string;
  name: string;
  shortUrl: string;
  labels: TrelloLabel[];
}

interface Commit {
  hash: string;
  date: Date;
  subject: string;
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

async function trelloGet<T>(
  path: string,
  params: Record<string, string>,
  auth: { key: string; token: string },
): Promise<T> {
  const url = new URL(`${TRELLO_API_BASE}${path}`);
  for (const [k, v] of Object.entries(params)) url.searchParams.set(k, v);
  url.searchParams.set('key', auth.key);
  url.searchParams.set('token', auth.token);

  const response = await fetch(url);
  if (!response.ok) {
    const body = await response.text();
    throw new Error(`Trello API error ${response.status} for ${path}: ${body}`);
  }
  return (await response.json()) as T;
}

function createdAtFromCardId(cardId: string): Date {
  const timestampHex = cardId.substring(0, 8);
  return new Date(parseInt(timestampHex, 16) * 1000);
}

function getCommits(branch: string, cwd: string): Commit[] {
  const raw = execFileSync('git', ['log', branch, `--format=%H${FIELD_SEP}%cI${FIELD_SEP}%s`], {
    cwd,
    encoding: 'utf-8',
    maxBuffer: 1024 * 1024 * 64,
  });
  return raw
    .split('\n')
    .filter(Boolean)
    .map((line) => {
      const [hash, date, subject] = line.split(FIELD_SEP);
      return { hash, date: new Date(date), subject };
    });
}

function formatPercent(fraction: number): string {
  return (fraction * 100).toFixed(2) + '%';
}

async function main() {
  const auth = {
    key: requireEnv('TRELLO_API_KEY'),
    token: requireEnv('TRELLO_TOKEN'),
  };
  const boardId = requireEnv('TRELLO_BOARD_ID');
  const repoPath = requireEnv('GIT_REPO_PATH');

  const commits = getCommits(BRANCH, repoPath);
  const reverts = commits.filter((c) => REVERT_REGEX.test(c.subject));

  const labels = await trelloGet<TrelloLabel[]>(`/boards/${boardId}/labels`, { fields: 'id,name' }, auth);
  const bugLabel = labels.find((l) => l.name.trim().toLowerCase() === LABEL_NAME);
  if (!bugLabel) {
    throw new Error(
      `No label named "${LABEL_NAME}" found on board ${boardId}. Available labels: ${labels.map((l) => l.name || '(unnamed)').join(', ')}`,
    );
  }

  const allCards = await trelloGet<TrelloCard[]>(
    `/boards/${boardId}/cards/open`,
    { fields: 'id,name,shortUrl,labels' },
    auth,
  );
  const bugCards = allCards
    .filter((c) => c.labels.some((l) => l.id === bugLabel.id))
    .map((c) => ({ card: c, createdAt: createdAtFromCardId(c.id) }));

  const deploys = commits.length;
  const failures = bugCards.length + reverts.length;
  const cfr = deploys > 0 ? failures / deploys : 0;

  console.log(`Deploys (commits on ${BRANCH}): ${deploys}`);
  console.log(`Bug cards created:              ${bugCards.length}`);
  console.log(`Revert commits:                 ${reverts.length}`);
  console.log(`Failures (bug cards + reverts): ${failures}`);
  console.log('');
  console.log(`Change failure rate: ${formatPercent(cfr)}`);

  console.log('');
  console.log(`Revert commits (${reverts.length}):`);
  for (const r of reverts) console.log(`  - ${r.date.toISOString().slice(0, 10)}  ${r.hash.slice(0, 8)}  ${r.subject}`);

  console.log('');
  console.log(`Bug cards (${bugCards.length}):`);
  for (const { card, createdAt } of bugCards) {
    console.log(`  - ${createdAt.toISOString().slice(0, 10)}  ${card.name} (${card.shortUrl})`);
  }

  await mkdir(DATA_DIR, { recursive: true });

  const commitsHeader = 'hash,date,subject,is_revert\n';
  const commitsRows = commits
    .map((c) => `${c.hash},${c.date.toISOString()},"${c.subject.replace(/"/g, '""')}",${REVERT_REGEX.test(c.subject)}`)
    .join('\n');
  await writeFile(COMMITS_CSV_PATH, commitsHeader + commitsRows + '\n', 'utf-8');

  const bugCardsHeader = 'card_name,card_url,created_at\n';
  const bugCardsRows = bugCards
    .map(({ card, createdAt }) => `"${card.name.replace(/"/g, '""')}",${card.shortUrl},${createdAt.toISOString()}`)
    .join('\n');
  await writeFile(BUG_CARDS_CSV_PATH, bugCardsHeader + bugCardsRows + '\n', 'utf-8');

  console.log('');
  console.log(`Wrote raw data to ${COMMITS_CSV_PATH} and ${BUG_CARDS_CSV_PATH}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
