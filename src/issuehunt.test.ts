import assert from 'node:assert/strict';
import test from 'node:test';
import { extractAssignedJson } from './issuehunt';

test('extracts NEXT_DATA object and ignores trailing script code', () => {
  const html = '<script>__NEXT_DATA__ = {"props":{"pageProps":{"issues":[{"title":"a } brace"}]}}};window.x=1;</script>';
  const data = extractAssignedJson(html);
  assert.equal(data.props.pageProps.issues[0].title, 'a } brace');
});

test('handles escaped quotes and nested objects', () => {
  const html = '<script>__NEXT_DATA__ = {"a":{"b":"quoted \\"value\\"","c":{"d":2}}};</script>';
  const data = extractAssignedJson(html);
  assert.equal(data.a.b, 'quoted "value"');
  assert.equal(data.a.c.d, 2);
});

test('rejects pages without NEXT_DATA marker', () => {
  assert.throws(() => extractAssignedJson('<html></html>'), /marker not found/);
});
