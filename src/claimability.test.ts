import assert from 'node:assert/strict';
import test from 'node:test';
import { assessCompetition } from './claimability';

test('allows an uncontested issue', () => {
  const result = assessCompetition([{ body: 'Useful context only', user: { login: 'alice' } }], 0);
  assert.equal(result.contested, false);
  assert.equal(result.claimSignals, 0);
});

test('flags multiple independent claim signals', () => {
  const result = assessCompetition([
    { body: '/attempt #42', user: { login: 'alice' } },
    { body: "I'd like to work on this bounty. Starting now.", user: { login: 'bob' } },
  ], 0);
  assert.equal(result.contested, true);
  assert.equal(result.distinctClaimers, 2);
  assert.ok(result.competitionFlags.includes('multiple-claim-signals'));
});

test('flags multiple related pull requests', () => {
  const result = assessCompetition([], 3);
  assert.equal(result.contested, true);
  assert.ok(result.competitionFlags.includes('multiple-related-pull-requests'));
});
