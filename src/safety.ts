export interface SafetyAssessment {
  safe: boolean;
  riskFlags: string[];
}

const PROMPT_EXFILTRATION_PATTERNS: Array<[RegExp, string]> = [
  [/pre[- ]conversation/i, 'asks-for-pre-conversation-context'],
  [/initiali[sz]ation payload/i, 'asks-for-initialization-payload'],
  [/system prompt/i, 'asks-for-system-prompt'],
  [/developer (prompt|instructions)/i, 'asks-for-developer-instructions'],
  [/hidden (prompt|instructions|configuration)/i, 'asks-for-hidden-instructions'],
  [/paste.{0,80}(every|all|full).{0,80}(instruction|rule|configuration)/is, 'asks-for-full-agent-instructions'],
  [/reveal.{0,80}(instruction|prompt|configuration)/is, 'asks-to-reveal-agent-context'],
];

const SECRET_EXFILTRATION_PATTERNS: Array<[RegExp, string]> = [
  [/paste.{0,80}(api key|token|password|secret)/is, 'asks-to-paste-secret'],
  [/include.{0,80}(api key|token|password|secret).{0,80}(source|code|comment)/is, 'asks-to-embed-secret'],
];

export function assessIssueSafety(title: string, body: string, labels: string[]): SafetyAssessment {
  const text = `${title}\n${body}\n${labels.join(' ')}`;
  const riskFlags: string[] = [];

  for (const [pattern, flag] of [...PROMPT_EXFILTRATION_PATTERNS, ...SECRET_EXFILTRATION_PATTERNS]) {
    if (pattern.test(text)) riskFlags.push(flag);
  }

  if (labels.some((label) => /autonom(?:ous|us) agents only/i.test(label))) {
    riskFlags.push('agent-only-task-manual-review-required');
  }

  return {
    safe: riskFlags.length === 0,
    riskFlags: [...new Set(riskFlags)],
  };
}

export function ageInDays(createdAt: string, now = Date.now()): number {
  const created = Date.parse(createdAt);
  if (!Number.isFinite(created)) return Number.POSITIVE_INFINITY;
  return Math.max(0, Math.floor((now - created) / 86_400_000));
}
