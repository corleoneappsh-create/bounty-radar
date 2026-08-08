import https from 'https';
import { ageInDays, assessIssueSafety } from './safety';
import { assessRepositoryTrust } from './trust';
import type { Bounty } from './search';

type JsonObject = Record<string, any>;

function getText(url: string): Promise<string> {
  return new Promise((resolve, reject) => {
    https.get(url, {
      headers: {
        'User-Agent': 'MoneyLab-bounty-radar/1.1',
        'Accept': 'text/html,application/json',
        ...(url.startsWith('https://api.github.com') && process.env.GH_TOKEN
          ? { Authorization: `Bearer ${process.env.GH_TOKEN}` } : {}),
      },
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => data += String(chunk));
      res.on('end', () => {
        if ((res.statusCode ?? 500) >= 400) return reject(new Error(`HTTP ${res.statusCode} for ${url}`));
        resolve(data);
      });
    }).on('error', reject);
  });
}

export function extractAssignedJson(html: string, marker = '__NEXT_DATA__ = '): JsonObject {
  const start = html.indexOf(marker);
  if (start < 0) throw new Error('IssueHunt NEXT_DATA marker not found');
  const text = html.slice(start + marker.length);
  let depth = 0;
  let inString = false;
  let escaped = false;
  let begin = -1;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (inString) {
      if (escaped) escaped = false;
      else if (ch === '\\') escaped = true;
      else if (ch === '"') inString = false;
      continue;
    }
    if (ch === '"') { inString = true; continue; }
    if (ch === '{') {
      if (begin < 0) begin = i;
      depth += 1;
    } else if (ch === '}') {
      depth -= 1;
      if (begin >= 0 && depth === 0) return JSON.parse(text.slice(begin, i + 1));
    }
  }
  throw new Error('IssueHunt NEXT_DATA JSON is incomplete');
}

async function fetchGithubIssue(repo: string, issue: number): Promise<JsonObject | null> {
  try {
    const raw = await getText(`https://api.github.com/repos/${repo}/issues/${issue}`);
    const data = JSON.parse(raw);
    if (data.pull_request) return null;
    return data;
  } catch {
    return null;
  }
}
function toBounty(raw: JsonObject, github: JsonObject): Bounty {
  const repo = `${raw.repositoryOwnerName}/${raw.repositoryName}`;
  const labels = (github.labels ?? []).map((l: JsonObject | string) => typeof l === 'string' ? l : String(l.name ?? ''));
  const body = String(github.body ?? '');
  const safety = assessIssueSafety(String(github.title ?? raw.title ?? ''), body, labels);
  const trust = assessRepositoryTrust(repo, String(github.title ?? raw.title ?? ''), labels);
  const extraTrust = Number(raw.pullRequestCount ?? 0) > 0 ? ['issuehunt-existing-pr-listed'] : [];
  return {
    repo,
    issue: Number(raw.number),
    title: String(github.title ?? raw.title ?? ''),
    amount: `$${(Number(raw.depositAmount ?? 0) / 100).toFixed(2)}`,
    labels,
    url: String(github.html_url ?? `https://github.com/${repo}/issues/${raw.number}`),
    comments: Number(github.comments ?? 0),
    createdAt: String(github.created_at ?? raw.fundedAt ?? '').substring(0, 10),
    platform: 'issuehunt',
    safe: safety.safe,
    riskFlags: safety.riskFlags,
    ageDays: ageInDays(String(github.created_at ?? raw.fundedAt ?? '')),
    trustedForAutoQueue: trust.trustedForAutoQueue && extraTrust.length === 0,
    trustFlags: [...trust.trustFlags, ...extraTrust],
    assignees: (github.assignees ?? []).map((a: JsonObject) => String(a.login ?? '')).filter(Boolean),
  };
}
export async function searchIssueHuntBounties(options: { minAmount?: number; maxPages?: number } = {}): Promise<Bounty[]> {
  const firstHtml = await getText('https://oss.issuehunt.io/issues');
  const firstData = extractAssignedJson(firstHtml);
  const firstProps = firstData.props?.pageProps ?? {};
  const totalPages = Math.min(Number(firstProps.totalPage ?? 1), options.maxPages ?? 50);
  const pages: JsonObject[][] = [Array.isArray(firstProps.issues) ? firstProps.issues : []];

  for (let page = 2; page <= totalPages; page += 1) {
    const html = await getText(`https://oss.issuehunt.io/issues?page=${page}`);
    const data = extractAssignedJson(html);
    pages.push(Array.isArray(data.props?.pageProps?.issues) ? data.props.pageProps.issues : []);
  }

  const minCents = Math.max(0, Math.round((options.minAmount ?? 0) * 100));
  const candidates = pages.flat().filter((raw) =>
    Number(raw.depositAmount ?? 0) >= minCents && String(raw.status ?? '') === 'ready');

  const output: Bounty[] = [];
  for (const raw of candidates) {
    const repo = `${raw.repositoryOwnerName}/${raw.repositoryName}`;
    const github = await fetchGithubIssue(repo, Number(raw.number));
    if (!github || github.state !== 'open') continue;
    output.push(toBounty(raw, github));
  }
  return output;
}
