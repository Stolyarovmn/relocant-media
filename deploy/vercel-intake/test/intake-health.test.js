import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture, message } from './helpers.js';

test('проверка инцидентов только читает состояние, не добавляя метрик реакции', async () => {
  const f = await fixture();
  await f.receive(message(1, 'Частный текст'));
  const before = await f.state();
  const status = await f.ingress.transfer({ operation: 'status' });
  assert.deepEqual(status.intake_health, { version: 1, queue_waiting: false,
    uncertain_delivery: false, storage_near_capacity: false, attempt_stuck: false });
  assert.ok(!JSON.stringify(status).includes('Частный текст'));
  assert.deepEqual(await f.state(), before);
  f.advance(20 * 60 * 1000);
  assert.equal((await f.ingress.transfer({ operation: 'status' })).intake_health.queue_waiting, true);
  const batch = await f.ingress.transfer({ operation: 'peek' });
  await f.ingress.transfer({ operation: 'ack', through: batch.events.at(-1).sequence,
    snapshot_sha256: 'a'.repeat(64), open_drafts: [] });
  assert.equal((await f.ingress.transfer({ operation: 'status' })).intake_health.queue_waiting, false);
});

test('неизвестная доставка и истёкшая попытка видны монитору без повторной отправки', async () => {
  const f = await fixture({ hooks: { afterSending: () => { throw new Error('interrupted'); } } });
  await assert.rejects(f.receive(message(1, 'Описание')), /interrupted/);
  f.advance(46_000);
  const check = await f.ingress.transfer({ operation: 'status' });
  assert.equal(check.intake_health.uncertain_delivery, true);
  assert.equal(check.intake_health.attempt_stuck, true);
  assert.equal(f.calls.length, 0);
  assert.equal((await f.state()).busy.phase, 'sending');
});
