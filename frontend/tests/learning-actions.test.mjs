// Offline contract tests: synthetic payloads only, no API requests or storage.
// Run: node --experimental-strip-types --test frontend/tests/learning-actions.test.mjs
import assert from 'node:assert/strict';
import { test } from 'node:test';
import { normaliseLearningAction, normaliseToday, manualRatedAttempt } from '../src/types/learning.ts';

const attempt = {
  id: 'attempt-1', day_unit_id: 'unit-1', card_id: 'card-1',
  content_revision_no: 4, status: 'revealed', revealed_at: '2026-10-16T09:04:50Z',
};
const revealedUnit = {
  id: 'unit-1', day_unit_id: 'unit-1', attempt_id: 'attempt-1', card_id: 'card-1',
  question: '会计核算与会计监督有什么关系？', answer: '两者相辅相成。',
  revealed: true, content_revision_no: 4, attempt,
};

test('actual flat reveal response retains question, answer and attempt identity', () => {
  const action = normaliseLearningAction(revealedUnit);
  assert.equal(action.unit?.prompt, revealedUnit.question);
  assert.equal(action.unit?.answer, revealedUnit.answer);
  assert.equal(action.unit?.revealed, true);
  assert.equal(action.unit?.id, attempt.id);
  assert.notEqual(action.unit?.id, revealedUnit.id);
  assert.equal(action.unit?.day_unit_id, 'unit-1');
  assert.equal(action.unit?.content_revision_no, 4);
});

test('wrapped flat responses retain the revealed content', () => {
  for (const key of ['data', 'result']) {
    const action = normaliseLearningAction({ [key]: revealedUnit });
    assert.equal(action.unit?.id, attempt.id);
    assert.equal(action.unit?.answer, revealedUnit.answer);
  }
});

test('existing rating envelope preserves idempotency and attempt identity', () => {
  const rated = { ...attempt, status: 'rated', rating: 'mastered' };
  const action = normaliseLearningAction({
    unit: revealedUnit, attempt: rated, request_id: 'rate-request-1',
    idempotent_replay: true, day_version: 6, rating: 'mastered',
  });
  assert.equal(action.unit?.id, attempt.id);
  assert.equal(action.unit?.answer, revealedUnit.answer);
  assert.equal(action.unit?.rating, 'mastered');
  assert.equal(action.unit?.completed, true);
  assert.equal(action.attempt?.id, attempt.id);
  assert.equal(action.request_id, 'rate-request-1');
  assert.equal(action.idempotent_replay, true);
  assert.equal(action.day_version, 6);
});

test('unit envelope keeps its embedded attempt when no outer attempt is provided', () => {
  const action = normaliseLearningAction({ unit: revealedUnit });
  assert.equal(action.unit?.id, attempt.id);
  assert.equal(action.unit?.status, 'revealed');
  assert.equal(action.unit?.revealed, true);
  assert.equal(action.unit?.answer, revealedUnit.answer);
});

test('pending today payload never manufactures an answer or promotes unit id to attempt id', () => {
  const pending = {
    id: 'unit-2', day_unit_id: 'unit-2', question: '尚未揭示的问题', revealed: false,
    attempt: { id: 'attempt-2', day_unit_id: 'unit-2', status: 'pending' },
  };
  const today = normaliseToday({ status: 'active', current_attempt: pending, total: 1 });
  assert.equal(today.current_attempt?.answer, null);
  assert.equal(today.current_attempt?.revealed, false);
  assert.equal(today.current_attempt?.id, 'attempt-2');
  const unitWithoutAttempt = normaliseLearningAction({ day_unit_id: 'unit-3', id: 'unit-3', question: '问题', revealed: false });
  assert.equal(unitWithoutAttempt.unit?.id || '', '');
  assert.equal(unitWithoutAttempt.unit?.answer ?? null, null);
});

function ratingFlow() {
  const previous = normaliseToday({ study_date: '2026-10-16', status: 'active', current_round: 1, state_version: 5, current_attempt: { ...revealedUnit, study_date: '2026-10-16' } });
  const action = normaliseLearningAction({ study_date: '2026-10-16', day_version: 6, round: 1, request_id: 'manual-1', unit: revealedUnit, attempt: { ...attempt, status: 'rated', rating: 'mastered' } });
  const summary = normaliseToday({ study_date: '2026-10-16', status: 'active', current_round: 1, state_version: 6, total: 2, completed: 1, remaining: 1, units: [revealedUnit, { id: 'unit-2', card_id: 'card-2', question: '下一题' }], current_attempt: { id: 'unit-2', day_unit_id: 'unit-2', question: '下一题', revealed: false, attempt: { id: 'attempt-2', card_id: 'card-2', round: 1, status: 'pending' } } });
  return { previous, action, summary };
}

test('manual rating keeps the rated answer while fresh counts/version and next question remain authoritative', () => {
  const { previous, action, summary } = ratingFlow();
  const retained = manualRatedAttempt(summary, action, previous);
  assert.equal(retained?.id, 'attempt-1');
  assert.equal(retained?.rating, 'mastered');
  assert.equal(retained?.completed, true);
  assert.equal(retained?.answer, revealedUnit.answer);
  assert.equal(summary.current_attempt?.id, 'attempt-2');
  assert.equal(summary.current_attempt?.revealed, false);
  assert.equal(summary.current_attempt?.answer, null);
  assert.equal(summary.state_version, 6);
  assert.equal(summary.completed, 1);
  assert.equal(summary.remaining, 1);
});

test('manual retention rejects cross-day, changed round/version, missing source and mismatched attempts', () => {
  const { previous, action, summary } = ratingFlow();
  for (const changed of [
    { ...summary, date: '2026-10-17' },
    { ...summary, round: 2 },
    { ...summary, state_version: 7 },
    { ...summary, status: 'paused' },
    { ...summary, queue: summary.queue.filter(card => card.id !== 'card-1') },
  ]) assert.equal(manualRatedAttempt(changed, action, previous), null);
  assert.equal(manualRatedAttempt(summary, { ...action, unit: { ...action.unit, id: 'other-attempt' } }, previous), null);
  assert.equal(manualRatedAttempt(summary, { ...action, unit: null }, previous), null);
  assert.equal(manualRatedAttempt(summary, { ...action, study_date: '2026-10-17' }, previous), null);
  assert.equal(manualRatedAttempt(summary, { ...action, unit: { ...action.unit, content_revision_no: 5 } }, previous), null);
  assert.equal(manualRatedAttempt(summary, { ...action, unit: { ...action.unit, day_unit_id: 'other-unit' } }, previous), null);
});

test('round and day completion flags survive API normalization, including last-card manual retention', () => {
  const { previous, action, summary } = ratingFlow();
  const completedRound = normaliseToday({ ...summary, current_attempt: null, round_complete: true, can_advance: true, day_completed: false, units: [revealedUnit] });
  assert.equal(completedRound.round_complete, true);
  assert.equal(completedRound.can_advance, true);
  assert.equal(completedRound.day_completed, false);
  assert.equal(manualRatedAttempt(completedRound, action, previous)?.id, 'attempt-1');
  const completedDay = normaliseToday({ status: 'completed', day_completed: true, round_complete: true, can_advance: false });
  assert.equal(completedDay.day_completed, true);
  assert.equal(completedDay.can_advance, false);
});
