import test from 'node:test';
import assert from 'node:assert/strict';
import { fixture, message, seed } from './helpers.js';
import { READER_INTRO } from '../lib/intake.js';

test('новая инструкция применяется к существующему seed без потери черновика', async () => {
  const settings = seed();
  settings.form.menu = READER_INTRO.replace('о чем хотите', 'что хотите');
  const f = await fixture({ seed: settings });
  await f.receive(message(1, 'Материалы до обновления'));
  const reference = (await f.state()).cases[0].reference;
  await f.receive(message(2, '/start'));
  const state = await f.state();
  assert.equal(state.form.menu, READER_INTRO);
  assert.equal(state.cases[0].reference, reference);
  assert.equal(state.cases[0].payload.text, 'Материалы до обновления');
  assert.equal(f.calls[0].params.text, READER_INTRO);
});
