import https from 'https';

export interface CompetitionAssessment {
  contested: boolean;
  claimSignals: number;
  distinctClaimers: number;
  relatedPullRequests: number;
  competitionFlags: string[];
}

const CLAIM_PATTERNS = [
  /(^|\s)\/(claim|attempt)\b/i,
  /i(?:'|’)d like to work on this/i,
  /i am (?:starting|working on) (?:this|it)/i,
  /starting now/i,
  /working on this bounty/i,
];

export function assessCompetition(
  comments: Array<{ body?: string; user?: { login?: string } }> = [],
  relatedPullRequests = 0,
): CompetitionAssessment {
  let claimSignals = 0;
  const claimers = new Set<string>();

  for (const comment of comments) {
    const body = String(comment.body ?? '');
    if (!CLAIM_PATTERNS.some((pattern) => pattern.test(body))) continue;
    claimSignals += 1;
    const login = String(comment.user?.login ?? '').trim();
    if (login) claimers.add(login);
  }
  const competitionFlags: string[] = [];
  if (claimSignals >= 2 || claimers.size >= 2) competitionFlags.push('multiple-claim-signals');
  if (relatedPullRequests >= 2) competitionFlags.push('multiple-related-pull-requests');
  if (relatedPullRequests >= 1 && claimSignals >= 1) competitionFlags.push('claimed-and-pr-present');

  return {
    contested: competitionFlags.length > 0,
    claimSignals,
    distinctClaimers: claimers.size,
    relatedPullRequests,
    competitionFlags,
  };
}

function githubJson(path: string): Promise<any> {
  return new Promise((resolve, reject) => {
    https.get(`https://api.github.com${path}`, {
      headers: {
        'User-Agent': 'MoneyLab-bounty-radar/1.1',
        'Accept': 'application/vnd.github+json',
        ...(process.env.GH_TOKEN ? { Authorization: `Bearer ${process.env.GH_TOKEN}` } : {}),
      },
    }, (res) => {
      let data = '';
      res.on('data', (chunk) => data += String(chunk));
      res.on('end', () => {
        if ((res.statusCode ?? 500) >= 400) return reject(new Error(`GitHub HTTP ${res.statusCode}`));
        try { resolve(JSON.parse(data)); } catch { reject(new Error('Invalid GitHub JSON')); }
      });
    }).on('error', reject);
  });
}
export async function checkGithubCompetition(repo: string, issue: number): Promise<CompetitionAssessment> {
  try {
    const comments = await githubJson(`/repos/${repo}/issues/${issue}/comments?per_page=100`);
    const q = encodeURIComponent(`repo:${repo} is:pr \"#${issue}\"`);
    const search = await githubJson(`/search/issues?q=${q}&per_page=20&sort=updated&order=desc`);
    const prs = Array.isArray(search.items) ? search.items : [];
    return assessCompetition(Array.isArray(comments) ? comments : [], prs.length);
  } catch {
    return {
      contested: true,
      claimSignals: 0,
      distinctClaimers: 0,
      relatedPullRequests: 0,
      competitionFlags: ['competition-check-failed'],
    };
  }
}
