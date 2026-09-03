#!/usr/bin/env node
/**
 * Compute DORA "Mean Time to Recovery" from a Trello board, reported as the
 * median (the standard way to summarize MTTR, since duration distributions
 * are typically right-skewed by a handful of slow outliers) with mean/min/max
 * shown alongside for reference.
 *
 * MTTR per card = (last time the card entered "Done") - (first time it ever
 * entered "In Progress"). Both timestamps come from walking the card's
 * updateCard:idList action history: "first entered In Progress" is the
 * earliest move whose listAfter is the In Progress list (counting every
 * stall/restart, not just the final push), and "last entered Done" is the
 * most recent move whose listAfter is the Done list.
 *
 * Only non-archived (open) cards are considered. Cards carrying the target
 * label that never reached Done, or that reached Done but were never recorded
 * entering In Progress, are excluded from the MTTR sample and reported
 * separately. Cards whose In Progress -> Done gap is under 15 minutes (by
 * default) are also excluded and reported separately: a gap that short is
 * usually a card being dragged straight through both lists at once (e.g. a
 * Butler automation, or bulk cleanup) rather than real recovery time.
 *
 * Usage:
 *   node --import tsx/esm trello-mttr.ts [boardId] [--label=bug] [--done=Done] [--in-progress="In Progress"] [--min-duration-minutes=15]
 *
 * boardId defaults to the TRELLO_BOARD_ID env var if not passed positionally.
 *
 * Always writes the resolved (raw, per-card) dataset to data/mttr.csv — a
 * gitignored local cache intended for a dashboard to recompute MTTR over
 * arbitrary windows without re-hitting Trello.
 *
 * Required env vars:
 *   TRELLO_API_KEY
 *   TRELLO_TOKEN
 *   TRELLO_BOARD_ID  — the Trello board to read bug cards from (unless passed as an arg)
 */

import { mkdir, writeFile } from 'node:fs/promises';
import { join } from 'node:path';

const TRELLO_API_BASE = 'https://api.trello.com/1';
const DATA_DIR = join(import.meta.dirname, 'data');
const MTTR_CSV_PATH = join(DATA_DIR, 'mttr.csv');

interface TrelloList {
  id: string;
  name: string;
}

interface TrelloLabel {
  id: string;
  name: string;
}

interface TrelloCardAction {
  type: string;
  date: string;
  data: {
    listBefore?: { id: string; name: string };
    listAfter?: { id: string; name: string };
  };
}

interface TrelloCard {
  id: string;
  name: string;
  idList: string;
  closed: boolean;
  shortUrl: string;
  labels: TrelloLabel[];
  actions: TrelloCardAction[];
}

function parseArgs(argv: string[]) {
  const positional = argv.filter((a) => !a.startsWith('--'));
  const flags = new Map<string, string>();
  for (const a of argv) {
    if (a.startsWith('--')) {
      const [key, ...rest] = a.slice(2).split('=');
      flags.set(key, rest.join('='));
    }
  }
  return {
    boardId: positional[0] ?? requireEnv('TRELLO_BOARD_ID'),
    labelName: flags.get('label') ?? 'bug',
    doneListName: flags.get('done') ?? 'Done',
    inProgressListName: flags.get('in-progress') ?? 'In Progress',
    minDurationMinutes: Number(flags.get('min-duration-minutes') ?? '15'),
  };
}

function findUniqueList(lists: TrelloList[], name: string, boardId: string): TrelloList {
  const matches = lists.filter((l) => l.name.trim().toLowerCase() === name.trim().toLowerCase());
  if (matches.length === 0) {
    throw new Error(
      `No list named "${name}" found on board ${boardId}. Available lists: ${lists.map((l) => l.name).join(', ')}`,
    );
  }
  if (matches.length > 1) {
    throw new Error(
      `Multiple lists named "${name}" found; pass an exact name to disambiguate, or rename lists. Matches: ${matches
        .map((l) => `${l.name} (${l.id})`)
        .join(', ')}`,
    );
  }
  return matches[0];
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

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
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

function mean(values: number[]): number {
  return values.reduce((sum, v) => sum + v, 0) / values.length;
}

function median(values: number[]): number {
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 === 0 ? (sorted[mid - 1] + sorted[mid]) / 2 : sorted[mid];
}

function formatHours(ms: number): string {
  return (ms / (1000 * 60 * 60)).toFixed(2);
}

async function main() {
  const { boardId, labelName, doneListName, inProgressListName, minDurationMinutes } = parseArgs(
    process.argv.slice(2),
  );
  const minDurationMs = minDurationMinutes * 60 * 1000;
  const auth = {
    key: requireEnv('TRELLO_API_KEY'),
    token: requireEnv('TRELLO_TOKEN'),
  };

  const lists = await trelloGet<TrelloList[]>(`/boards/${boardId}/lists`, { fields: 'id,name' }, auth);
  const doneListId = findUniqueList(lists, doneListName, boardId).id;
  const inProgressListId = findUniqueList(lists, inProgressListName, boardId).id;

  const labels = await trelloGet<TrelloLabel[]>(`/boards/${boardId}/labels`, { fields: 'id,name' }, auth);
  const bugLabels = labels.filter((l) => l.name.trim().toLowerCase() === labelName.trim().toLowerCase());
  if (bugLabels.length === 0) {
    throw new Error(
      `No label named "${labelName}" found on board ${boardId}. Available labels: ${labels.map((l) => l.name || '(unnamed)').join(', ')}`,
    );
  }
  if (bugLabels.length > 1) {
    throw new Error(`Multiple labels named "${labelName}" found; this script can't disambiguate labels by name.`);
  }
  const bugLabelId = bugLabels[0].id;

  const allCards = await trelloGet<Array<Omit<TrelloCard, 'actions'>>>(
    `/boards/${boardId}/cards/open`,
    { fields: 'id,name,idList,closed,shortUrl,labels' },
    auth,
  );

  const bugCards = allCards.filter((c) => c.labels.some((l) => l.id === bugLabelId));
  console.log(`Found ${bugCards.length} card(s) labeled "${labelName}" on board ${boardId}. Fetching history...`);

  type Resolved = { card: TrelloCard; startedAt: Date; doneAt: Date; durationMs: number };
  const resolved: Resolved[] = [];
  const unresolved: TrelloCard[] = [];
  const ambiguous: TrelloCard[] = [];
  const noInProgress: TrelloCard[] = [];
  const tooFast: Resolved[] = [];

  for (const [index, card] of bugCards.entries()) {
    const actions = await trelloGet<TrelloCardAction[]>(
      `/cards/${card.id}/actions`,
      { filter: 'updateCard:idList', limit: '1000', fields: 'type,date,data' },
      auth,
    );
    const full: TrelloCard = { ...card, actions };

    const doneEntries = actions
      .filter((a) => a.data.listAfter?.id === doneListId)
      .map((a) => new Date(a.date));
    const inProgressEntries = actions
      .filter((a) => a.data.listAfter?.id === inProgressListId)
      .map((a) => new Date(a.date));

    if (doneEntries.length > 0) {
      const doneAt = new Date(Math.max(...doneEntries.map((d) => d.getTime())));
      if (inProgressEntries.length > 0) {
        const startedAt = new Date(Math.min(...inProgressEntries.map((d) => d.getTime())));
        const durationMs = doneAt.getTime() - startedAt.getTime();
        const entry = { card: full, startedAt, doneAt, durationMs };
        if (durationMs < minDurationMs) {
          // In Progress -> Done happened almost instantly; likely a card dragged
          // straight through both lists (automation/bulk cleanup), not real work time.
          tooFast.push(entry);
        } else {
          resolved.push(entry);
        }
      } else {
        // Reached Done but never recorded entering In Progress (e.g. moved directly
        // from another list, or action history has aged out). Can't compute MTTR for it.
        noInProgress.push(full);
      }
    } else if (card.idList === doneListId) {
      // Currently in Done but no recorded move into it (e.g. created directly there,
      // or action history has aged out of Trello's retention). Can't compute MTTR for it.
      ambiguous.push(full);
    } else {
      unresolved.push(full);
    }

    if ((index + 1) % 25 === 0) console.log(`  ...${index + 1}/${bugCards.length} processed`);
    await sleep(110); // stay comfortably under Trello's 100 req / 10s rate limit
  }

  console.log('');
  console.log('Card                                                            In Progress          Reached Done         Hours');
  for (const r of resolved.sort((a, b) => a.durationMs - b.durationMs)) {
    const name = r.card.name.length > 60 ? r.card.name.slice(0, 57) + '...' : r.card.name.padEnd(60);
    console.log(
      `${name}  ${r.startedAt.toISOString().slice(0, 16)}  ${r.doneAt.toISOString().slice(0, 16)}  ${formatHours(r.durationMs)}`,
    );
  }

  if (resolved.length > 0) {
    const durationsMs = resolved.map((r) => r.durationMs);
    console.log('');
    console.log(`Resolved bug cards: ${resolved.length}`);
    console.log(`Median time to recovery: ${formatHours(median(durationsMs))} hours`);
    console.log(`(mean: ${formatHours(mean(durationsMs))} hours, min: ${formatHours(Math.min(...durationsMs))} hours, max: ${formatHours(Math.max(...durationsMs))} hours)`);
  } else {
    console.log('No resolved bug cards found — nothing to compute MTTR from.');
  }

  if (unresolved.length > 0) {
    console.log('');
    console.log(`${unresolved.length} bug card(s) never reached "${doneListName}" — excluded from MTTR:`);
    for (const c of unresolved) console.log(`  - ${c.name} (${c.shortUrl})`);
  }
  if (ambiguous.length > 0) {
    console.log('');
    console.log(
      `${ambiguous.length} bug card(s) are currently in "${doneListName}" but have no recorded move into it (excluded, can't compute):`,
    );
    for (const c of ambiguous) console.log(`  - ${c.name} (${c.shortUrl})`);
  }
  if (noInProgress.length > 0) {
    console.log('');
    console.log(
      `${noInProgress.length} bug card(s) reached "${doneListName}" but never recorded entering "${inProgressListName}" (excluded, can't compute):`,
    );
    for (const c of noInProgress) console.log(`  - ${c.name} (${c.shortUrl})`);
  }
  if (tooFast.length > 0) {
    console.log('');
    console.log(
      `${tooFast.length} bug card(s) went "${inProgressListName}" -> "${doneListName}" in under ${minDurationMinutes} minutes (excluded as likely automation/bulk-move noise):`,
    );
    for (const r of tooFast) {
      const minutes = (r.durationMs / (1000 * 60)).toFixed(1);
      console.log(`  - ${r.card.name} (${minutes} min) (${r.card.shortUrl})`);
    }
  }

  await mkdir(DATA_DIR, { recursive: true });
  const header = 'card_name,card_url,in_progress_at,done_at,duration_hours\n';
  const rows = resolved
    .map(
      (r) =>
        `"${r.card.name.replace(/"/g, '""')}",${r.card.shortUrl},${r.startedAt.toISOString()},${r.doneAt.toISOString()},${formatHours(r.durationMs)}`,
    )
    .join('\n');
  await writeFile(MTTR_CSV_PATH, header + rows + '\n', 'utf-8');
  console.log('');
  console.log(`Wrote raw data to ${MTTR_CSV_PATH}`);
}

main().catch((error) => {
  console.error(error instanceof Error ? error.message : error);
  process.exit(1);
});
