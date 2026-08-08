export interface TrustAssessment {
  trustedForAutoQueue: boolean;
  trustFlags: string[];
}

const QUARANTINED_REPOSITORIES = new Map<string, string>([
  ['SecureBananaLabs/bug-bounty', 'observed-pending-settlement-no-confirmed-payout'],
  ['ClankerNation/OpenAgents', 'observed-agent-context-exfiltration-requirement'],
]);

export function assessRepositoryTrust(repo: string, title: string, labels: string[]): TrustAssessment {
  const trustFlags: string[] = [];
  const quarantineReason = QUARANTINED_REPOSITORIES.get(repo);
  if (quarantineReason) trustFlags.push(quarantineReason);

  if (/bounty[-_ ]?plaza/i.test(repo)) {
    trustFlags.push('bounty-mirror-not-source-repository');
  }

  const text = `${title}\n${labels.join(' ')}`;
  if (/security vulnerab|unauthenticated|access control|auth bypass|token forg|role self-assignment/i.test(text)) {
    trustFlags.push('security-sensitive-manual-review');
  }

  return {
    trustedForAutoQueue: trustFlags.length === 0,
    trustFlags: [...new Set(trustFlags)],
  };
}
