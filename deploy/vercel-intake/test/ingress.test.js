import test from "node:test";
import assert from "node:assert/strict";
import { fixture, message, seed, bootstrap, FakeStore } from "./helpers.js";
import { Ingress, LIMITS } from "../lib/engine.js";

test("пустой и повторный peek не пишут Blob; новый экспорт сохраняет границу ACK", async () => {
  const f = await fixture();
  assert.equal(f.store.revision, 2); // bootstrap: config и state.
  for (let repeat = 0; repeat < 3; repeat++) {
    assert.deepEqual(await f.ingress.transfer({ operation: "peek" }), { status: "ready", events: [] });
  }
  assert.equal(f.store.revision, 2);

  await f.receive(message(1, "Привет"));
  assert.equal(f.store.revision, 5); // prepared, sending, затем durable receipt.
  assert.equal(f.calls.length, 1);
  assert.equal((await f.state()).exported_through, 0);
  for (let repeat = 0; repeat < 3; repeat++) {
    await assert.rejects(f.ingress.transfer({ operation: "peek", after: 99 }),
      { status: 409, code: "peek_conflict" });
  }
  assert.equal(f.store.revision, 5);
  await assert.rejects(f.ingress.transfer({ operation: "ack", through: 1,
    snapshot_sha256: "a".repeat(64) }), { status: 409, code: "ack_conflict" });
  assert.equal(f.store.revision, 5);

  const first = await f.ingress.transfer({ operation: "peek" });
  assert.deepEqual(first.events.map((event) => event.sequence), [1]);
  assert.equal(f.store.revision, 6);
  for (let repeat = 0; repeat < 3; repeat++) {
    assert.deepEqual(await f.ingress.transfer({ operation: "peek" }), first);
  }
  assert.equal(f.store.revision, 6);

  await f.receive(message(2, "Текст обращения"));
  assert.equal(f.store.revision, 7);
  assert.deepEqual(await f.ingress.transfer({ operation: "peek", limit: 1 }), first);
  assert.equal(f.store.revision, 7);
  const next = await f.ingress.transfer({ operation: "peek", after: 1 });
  assert.deepEqual(next.events.map((event) => event.sequence), [2]);
  assert.equal((await f.state()).exported_through, 2);
  assert.equal(f.store.revision, 8);
  await assert.rejects(f.ingress.transfer({ operation: "ack", through: 3,
    snapshot_sha256: "b".repeat(64) }), { status: 409, code: "ack_conflict" });
  assert.equal(f.store.revision, 8);

  await f.ingress.transfer({ operation: "ack", through: 2, snapshot_sha256: "b".repeat(64) });
  assert.equal(f.store.revision, 9);
  for (let repeat = 0; repeat < 3; repeat++) {
    assert.deepEqual((await f.ingress.transfer({ operation: "peek" })).events, []);
  }
  assert.equal(f.store.revision, 9);
  assert.equal((await f.state()).ack_sequence, 2);
});

test("текст, фото, правка и выбор темы формируют один черновик", async () => {
  const f = await fixture();
  await f.receive(message(1, "Привет"));
  await f.receive(message(2, "Есть вопрос о мосте"));
  await f.receive(message(3, undefined, { text: undefined, caption: "Подпись", photo: [{ file_id: "file", file_unique_id: "unique" }] }));
  const edited = message(4, "Исправленный вопрос", { message_id: 102 });
  edited.edited_message = edited.message;
  delete edited.message;
  await f.receive(edited);
  await f.receive(message(5, "Предложить новость"));
  const state = await f.state();
  assert.equal(state.cases.length, 1);
  assert.equal(state.cases[0].status, "new");
  assert.equal(state.cases[0].payload.text, "Исправленный вопрос\nПодпись");
  assert.equal(state.cases[0].payload.attachments.length, 1);
  assert.equal(state.cases[0].id, undefined);
  assert.equal(state.events.at(-1).notice.status, "sent");
  assert.equal(f.calls.filter((call) => call.method === "sendMessage").length, 2);
  assert.equal(state.events.at(-1).draft.payload.reader_notice_pending, false);
});

test("приветствия и проверки присутствия до ответа объединяются без контента", async () => {
  const f = await fixture();
  for (const [index, text] of ["Привет", "Вы тут?", "есть кто-нибудь?", "ау!"].entries()) {
    await f.receive(message(index + 1, text));
  }
  assert.equal(f.calls.length, 1);
  assert.equal((await f.state()).cases[0].payload.text, "");
  await f.receive(message(8, "Предложить новость"));
  assert.equal((await f.state()).cases[0].status, "choosing");
  assert.match(f.calls.at(-1).params.text, /Что произошло/u);
  f.advance(5000);
  await f.receive(message(9, "Привет", { date: 1_800_000_005 }));
  assert.equal(f.calls.length, 3);
});

test("правка контента на приветствие убирает его из текста и не отправляет меню", async () => {
  const f = await fixture();
  await f.receive(message(1, "Было описание"));
  const edited = message(2, "Вы тут?", { message_id: 101 });
  edited.edited_message = edited.message;
  delete edited.message;
  await f.receive(edited);
  assert.equal((await f.state()).cases[0].payload.text, "");
  assert.equal(f.calls.length, 1);
  await f.receive(message(3, "Предложить новость"));
  assert.equal((await f.state()).cases[0].status, "choosing");
});

test("ранний выбор клавиатуры принимает содержимое при pending меню из снимка", async () => {
  const draft = {
    id: 9, reference: "RM-ABCD-1234", kind: "reader_unclassified", status: "choosing",
    telegram_user_id: 42, telegram_message_id: 55, payload: {
      reader_intake: true, public_reference: "RM-ABCD-1234", chat_id: 42,
      text: "Полезное описание", attachments: [], reader_parts: { 55: { text: "Полезное описание", attachments: [] } },
      reader_notices: { menu: { status: "pending", submit_on_pick: true, received_at: "2026-01-01T00:00:00Z" } },
    },
  };
  const f = await fixture({ seed: seed({ cases: [draft] }) });
  await f.receive(message(1, "Предложить новость"));
  const event = (await f.state()).events[0];
  assert.equal(event.draft.id, 9);
  assert.equal(event.draft.status, "new");
  assert.equal(event.draft.payload.reader_notices.menu.status, "superseded");
  assert.equal(event.notice.status, "sent");
});

test("повтор update и повтор выбора темы не создают второй черновик", async () => {
  const f = await fixture();
  await f.receive(message(1, "Новость"));
  await f.receive(message(2, "Предложить новость"));
  await f.receive(message(2, "Предложить новость"));
  await f.receive(message(3, "Предложить новость"));
  const state = await f.state();
  assert.equal(state.cases.length, 1);
  assert.equal(state.cases[0].status, "new");
  assert.equal(state.events.length, 3);
  assert.equal(state.events.at(-1).draft, undefined);
  assert.equal(f.calls.length, 3);
  assert.match(f.calls.at(-1).params.text, /уже отправлено/);
  await assert.rejects(f.receive(message(2, "Другая нагрузка")), { status: 409 });
});

test("relay сохраняет исходные updates и подтверждает получение без выполнения действия", async () => {
  const f = await fixture({ seed: seed({ legacy_conversations: [99] }) });
  const callbacks = ["intake:10:submit", "owner:approve:12", "arbitrary"];
  const updates = callbacks.map((data, index) => ({ update_id: index + 1, callback_query: {
    id: String(index), data, from: { id: 42 }, message: { chat: { id: 42 }, message_id: 100 },
  } }));
  updates.push(message(8, "/tip факт"));
  updates.push(message(9, "Ответ", { reply_to_message: { message_id: 999 } }));
  updates.push(message(10, "Старый диалог", { from: { id: 99 }, chat: { id: 99, type: "private" } }));
  for (const update of updates) await f.receive(update);
  const state = await f.state();
  assert.equal(f.calls.length, 3);
  assert.equal(f.calls[0].method, "answerCallbackQuery");
  assert.equal(state.events[0].notice.status, "sent");
  assert.deepEqual(state.events.map((event) => event.update), updates);
  assert.ok(state.events.every((event) => !event.draft));
});

test("callback fence переживает более ранний ACK и снимается только после закрытия канонического черновика", async () => {
  const f = await fixture();
  await f.receive(message(1, "Описание"));
  await f.ingress.transfer({ operation: "peek" });
  const callback = { update_id: 2, callback_query: {
    data: "intake:114:pick:news_tip", from: { id: 42, is_bot: false },
    message: { chat: { type: "private", id: 42 }, message_id: 500 },
  } };
  await f.receive(callback);
  await f.receive(message(3, "Дополнение для канона"));
  await f.ingress.transfer({ operation: "ack", through: 1, snapshot_sha256: "a".repeat(64), open_drafts: [] });
  let state = await f.state();
  assert.equal(state.relay_actors["42:42:"], 3);
  assert.equal(state.cases.length, 0);
  assert.equal(state.events.at(-1).draft, undefined);
  await f.ingress.transfer({ operation: "peek" });
  await f.ingress.transfer({ operation: "ack", through: 3, snapshot_sha256: "b".repeat(64),
    open_drafts: [{ reference: "RM-AAAA-BBBB", telegram_user_id: 42, chat_id: 42, topic_id: null }] });
  assert.equal((await f.state()).relay_actors["42:42:"], 3);
  await f.receive(message(4, "Ещё дополнение"));
  await f.ingress.transfer({ operation: "peek" });
  await f.ingress.transfer({ operation: "ack", through: 4, snapshot_sha256: "c".repeat(64), open_drafts: [] });
  assert.equal((await f.state()).relay_actors["42:42:"], undefined);
  await f.receive(message(5, "Новое описание"));
  state = await f.state();
  assert.equal(state.cases.length, 1);
  assert.equal(state.events[0].draft.payload.text, "Новое описание");
});

test("ACK без open_drafts не снимает существующий fence", async () => {
  const f = await fixture();
  await f.receive({ update_id: 1, callback_query: { data: "intake:114:new", from: { id: 42 }, message: { chat: { id: 42 } } } });
  await f.ingress.transfer({ operation: "peek" });
  await f.ingress.transfer({ operation: "ack", through: 1, snapshot_sha256: "a".repeat(64) });
  assert.equal((await f.state()).relay_actors["42:42:"], 1);
});

test("seed non-draft не копирует личный текст; seed choosing продолжает быстрый приём", async () => {
  const item = {
    id: 113, reference: "RM-AAAA-AAAA", kind: "news_tip", status: "new", telegram_user_id: 42,
    telegram_message_id: 100, payload: { reader_intake: true, public_reference: "RM-AAAA-AAAA", chat_id: 42,
      text: "Уже канонический частный текст", attachments: [] },
  };
  const f = await fixture({ seed: seed({ cases: [item] }) });
  assert.ok(!JSON.stringify(await f.state()).includes("Уже канонический частный текст"));
  assert.equal((await f.state()).relay_actors["42:42:"], 0);
  const active = structuredClone(item);
  Object.assign(active, { id: 114, reference: "RM-BBBB-BBBB", status: "choosing" });
  active.payload.public_reference = active.reference;
  active.payload.text = "Новый открытый черновик";
  const mixed = await fixture({ seed: seed({ cases: [item, active] }) });
  assert.equal((await mixed.state()).relay_actors["42:42:"], undefined);
  await mixed.receive(message(1, "Добавление"));
  assert.equal((await mixed.state()).events[0].draft.id, 114);
});

function canonicalSeedCase(status = "new") {
  return {
    id: 114, reference: "RM-AAAA-BBBB", kind: "question", status, telegram_user_id: 42,
    telegram_message_id: 100, payload: {
      reader_intake: true, public_reference: "RM-AAAA-BBBB", chat_id: 42,
      text: "Историческая каноническая заявка", attachments: [],
    },
  };
}

test("новый старт после seed non-draft сразу получает меню и принятие через fake Bot", async () => {
  for (const status of ["new", "waiting_primary", "source_linked"]) {
    const f = await fixture({ seed: seed({ cases: [canonicalSeedCase(status)] }) });
    assert.equal((await f.state()).relay_actors["42:42:"], 0);
    await f.receive(message(1, "Привет"));
    let state = await f.state();
    assert.equal(state.relay_actors["42:42:"], undefined);
    assert.equal(state.cases.length, 1);
    assert.equal(state.cases[0].status, "choosing");
    assert.equal(state.cases[0].id, undefined);
    assert.notEqual(state.cases[0].reference, "RM-AAAA-BBBB");
    assert.equal(f.calls.length, 1);
    assert.equal(state.events[0].notice.status, "sent");
    await f.receive(message(2, "Новый вопрос читателя"));
    await f.receive(message(3, "Вопрос или идея"));
    state = await f.state();
    assert.equal(state.cases[0].status, "new");
    assert.equal(state.cases[0].kind, "question");
    assert.equal(state.cases[0].payload.text, "Новый вопрос читателя");
    assert.equal(state.events.at(-1).notice.status, "sent");
    assert.equal(f.calls.length, 2);
    assert.ok(!JSON.stringify(state).includes("Историческая каноническая заявка"));
  }
});

test("все формы нового старта снимают только нулевой seed fence", async () => {
  for (const start of ["Привет!", "Вы тут?", "Новое обращение", "/start", "/start@reader_bot payload"]) {
    const f = await fixture({ seed: seed({ cases: [canonicalSeedCase()] }) });
    await f.receive(message(1, start));
    assert.equal((await f.state()).relay_actors["42:42:"], undefined);
    assert.equal((await f.state()).cases[0].status, "choosing");
    assert.equal(f.calls.length, 1);
  }
});

test("ответ старой заявке и последующий старт остаются raw до канонического ACK", async () => {
  const f = await fixture({ seed: seed({ cases: [canonicalSeedCase()] }) });
  const reply = message(1, "Привет", { reply_to_message: { message_id: 100 } });
  await f.receive(reply);
  assert.equal((await f.state()).relay_actors["42:42:"], 1);
  await f.receive(message(2, "Привет"));
  const state = await f.state();
  assert.equal(state.relay_actors["42:42:"], 2);
  assert.equal(state.cases.length, 0);
  assert.deepEqual(state.events[0].update, reply);
  assert.ok(state.events.every((event) => !event.draft && event.notice.status === "sent"));
  assert.equal(f.calls.length, 2);
});

test("динамический inline callback fence не снимается новым приветствием", async () => {
  const f = await fixture({ seed: seed({ cases: [canonicalSeedCase()] }) });
  await f.receive({ update_id: 1, callback_query: {
    data: "intake:114:new", from: { id: 42 }, message: { chat: { id: 42 }, message_id: 100 },
  } });
  await f.receive(message(2, "Привет"));
  const state = await f.state();
  assert.equal(state.relay_actors["42:42:"], 2);
  assert.equal(state.cases.length, 0);
  assert.ok(state.events.every((event) => !event.draft));
  assert.equal(f.calls.length, 1);
});

test("конкурентный ответ старой заявке устанавливает fence до повторного планирования старта", async () => {
  const f = await fixture({ seed: seed({ cases: [canonicalSeedCase()] }) });
  const updates = [
    message(1, "Дополнение", { reply_to_message: { message_id: 100 } }), message(2, "Привет"),
  ];
  const attempts = await Promise.allSettled([
    f.receive(message(1, "Дополнение", { reply_to_message: { message_id: 100 } })),
    f.receive(message(2, "Привет")),
  ]);
  for (let i = 0; i < attempts.length; i++) {
    if (attempts[i].status === "rejected") {
      assert.equal(attempts[i].reason.code, "attempt_busy");
      await f.receive(updates[i]);
    }
  }
  const state = await f.state();
  assert.equal(state.relay_actors["42:42:"], 2);
  assert.equal(state.cases.length, 0);
  assert.ok(state.events.every((event) => !event.draft && event.notice.status === "sent"));
  assert.equal(f.calls.length, 2);
});

test("пока другой prepared update занимает очередь, нулевой seed fence не снимается", async () => {
  const f = await fixture({ seed: seed({ cases: [canonicalSeedCase()] }),
    hooks: { afterPrepared: () => { throw new Error("simulated_interruption"); } } });
  await assert.rejects(f.receive(message(1, "Привет", {
    from: { id: 43 }, chat: { id: 43, type: "private" },
  })), /simulated_interruption/u);
  await assert.rejects(f.receive(message(2, "Привет")), { status: 503, code: "attempt_busy" });
  const state = await f.state();
  assert.equal(state.relay_actors["42:42:"], 0);
  assert.equal(state.busy.event.update.update_id, 1);
  assert.equal(state.receipts[2], undefined);
  assert.equal(f.calls.length, 0);
});

test("правка и приветствие с вложением не снимают нулевой seed fence", async () => {
  for (const kind of ["edited", "media"]) {
    const f = await fixture({ seed: seed({ cases: [canonicalSeedCase()] }) });
    const update = message(1, "Привет");
    if (kind === "edited") {
      update.edited_message = update.message;
      delete update.message;
    } else update.message.photo = [{ file_id: "private-photo" }];
    await f.receive(update);
    const state = await f.state();
    assert.equal(state.relay_actors["42:42:"], 1);
    assert.equal(state.cases.length, 0);
    assert.deepEqual(state.events[0].update, update);
    assert.equal(state.events[0].draft, undefined);
    assert.equal(f.calls.length, kind === "edited" ? 0 : 1);
  }
});

test("нулевой fence при существующем локальном черновике не открывает второй черновик", async () => {
  const f = await fixture({ seed: seed({ cases: [canonicalSeedCase("collecting")] }) });
  const state = await f.state();
  state.relay_actors["42:42:"] = 0;
  const before = await f.store.read(f.ingress.paths.state);
  await f.store.write(f.ingress.paths.state, state, before.etag);
  await f.receive(message(1, "Привет"));
  const saved = await f.state();
  assert.equal(saved.relay_actors["42:42:"], 1);
  assert.equal(saved.cases.length, 1);
  assert.equal(saved.cases[0].id, 114);
  assert.equal(saved.events[0].draft, undefined);
  assert.equal(f.calls.length, 1);
});

test("проверка parent DM разрешает только родительский канал", async () => {
  const f = await fixture();
  const dm = message(1, "Новость", {
    chat: { id: -55, type: "supergroup", is_direct_messages: true }, direct_messages_topic: { topic_id: 12 },
  });
  await f.receive(dm);
  assert.deepEqual(f.calls.map((call) => call.method), ["getChat", "sendMessage"]);
  assert.equal(f.calls[1].params.direct_messages_topic_id, 12);
  assert.equal((await f.state()).cases[0].payload.channel_id, -1001234);
  const other = await fixture({ botFactory: () => ({ call: async () => ({ is_direct_messages: true, parent_chat: { id: -999 } }) }) });
  await other.receive(dm);
  assert.equal((await other.state()).events[0].draft, undefined);
});

test("preview namespace изолирован и никогда не вызывает BotAPI", async () => {
  const store = new FakeStore();
  const prod = await fixture({ store });
  await prod.receive(message(1, "Секретное prod сообщение"));
  const preview = await fixture({ store, env: { VERCEL_ENV: "preview", VERCEL_DEPLOYMENT_ID: "dpl_test" } });
  await preview.receive(message(1, "Проверка preview"));
  await preview.receive(message(2, "В DM", { chat: { id: -55, is_direct_messages: true }, direct_messages_topic: { topic_id: 12 } }));
  assert.equal(preview.calls.length, 0);
  assert.equal(preview.ingress.status(await preview.state()).can_send, false);
  assert.equal((await prod.state()).events.length, 1);
  assert.ok(!JSON.stringify(await preview.state()).includes("Секретное prod"));
  assert.throws(() => new Ingress({ store, env: { VERCEL_ENV: "preview" } }), /namespace/u);
});

test("CAS конфликт повторяет план, отправка имеет сохранённый sending claim", async () => {
  const f = await fixture();
  f.store.conflicts = 3;
  await f.receive(message(1, "Привет"));
  assert.equal(f.calls.length, 1);
  assert.equal((await f.state()).next_sequence, 2);
});

test("параллельные дубликаты не отправляют два сообщения", async () => {
  const f = await fixture();
  const results = await Promise.allSettled([f.receive(message(1, "Привет")), f.receive(message(1, "Привет"))]);
  assert.ok(results.some((result) => result.status === "fulfilled"));
  await f.receive(message(1, "Привет"));
  assert.equal(f.calls.length, 1);
  assert.equal((await f.state()).events.length, 1);
});

test("crash prepared сохраняет сырой update; повтор возможен только после runtime+grace", async () => {
  const f = await fixture({ hooks: { afterPrepared: () => { throw new Error("crash"); } } });
  await assert.rejects(f.receive(message(1, "Привет")), /crash/u);
  assert.equal((await f.state()).busy.phase, "prepared");
  assert.equal(f.calls.length, 0);
  await assert.rejects(f.receive(message(1, "Привет")), { status: 503 });
  assert.deepEqual((await f.ingress.transfer({ operation: "peek" })).events, []);
  f.advance(LIMITS.attemptMs + 1);
  f.deps.hooks.afterPrepared = undefined;
  assert.equal((await f.ingress.transfer({ operation: "status" })).busy, true);
  assert.equal(f.calls.length, 0);
  const result = await f.ingress.transfer({ operation: "peek" });
  assert.equal(result.events[0].notice.status, "sent");
  assert.equal(f.calls.length, 1);
  await f.receive(message(1, "Привет"));
  assert.equal(f.calls.length, 1);
});

for (const window of ["afterSending", "afterSend"]) {
  test(`crash ${window} завершается uncertain без автоматического повтора`, async () => {
    const f = await fixture({ hooks: { [window]: () => { throw new Error("crash"); } } });
    await assert.rejects(f.receive(message(1, "Привет")), /crash/u);
    const before = f.calls.length;
    f.advance(LIMITS.attemptMs + 1);
    const peek = await f.ingress.transfer({ operation: "peek" });
    assert.equal(peek.events[0].notice.status, "uncertain");
    await f.receive(message(1, "Привет"));
    assert.equal(f.calls.length, before);
    assert.equal((await f.state()).busy, null);
  });
}

test("ошибка BotAPI и плохой receipt считаются uncertain, без повторов", async () => {
  for (const reply of [() => { throw new Error("network"); }, () => ({ message_id: null })]) {
    let calls = 0;
    const f = await fixture({ botFactory: () => ({ call: async () => { calls++; return reply(); } }) });
    await f.receive(message(1, "Привет"));
    await f.receive(message(1, "Привет"));
    assert.equal(calls, 1);
    assert.equal((await f.state()).events[0].notice.status, "uncertain");
  }
});

test("ACK удаляет все переданные копии принятого текста, сохраняя открытый черновик и minimal receipts", async () => {
  const f = await fixture();
  await f.receive(message(1, "Уникальный текст удаляемой заявки"));
  await f.receive(message(2, "Предложить новость"));
  await f.receive(message(3, "Открытый черновик", { from: { id: 43 }, chat: { type: "private", id: 43 } }));
  await assert.rejects(f.ingress.transfer({ operation: "ack", through: 2, snapshot_sha256: "f".repeat(64) }), { status: 409 });
  await f.ingress.transfer({ operation: "peek", after: 0, limit: 30 });
  await assert.rejects(f.ingress.transfer({ operation: "ack", through: 2, snapshot_sha256: "invalid" }), { status: 400 });
  await f.ingress.transfer({ operation: "ack", through: 2, snapshot_sha256: "f".repeat(64) });
  let state = await f.state();
  assert.ok(!JSON.stringify(state).includes("Уникальный текст удаляемой заявки"));
  assert.ok(JSON.stringify(state).includes("Открытый черновик"));
  assert.equal(state.cases.length, 1);
  assert.equal(state.events.length, 1);
  assert.deepEqual(Object.keys(state.receipts[1]).sort(), ["at", "hash", "sequence"]);
  assert.ok(!JSON.stringify((await f.store.read(f.ingress.paths.config)).value).includes("Открытый черновик"));
  await f.receive(message(1, "Уникальный текст удаляемой заявки"));
  assert.equal((await f.state()).events.length, 1);
  const calls = f.calls.length;
  f.advance(LIMITS.receiptMs + 1);
  await f.receive(message(4, "Текст", { from: { id: 44 }, chat: { type: "private", id: 44 } }));
  state = await f.state();
  assert.equal(state.receipts[1], undefined);
  assert.equal(f.calls.length, calls + 1);
});

test("отмена удаляется после checkpoint; ACK идемпотентен с новым корректным SHA", async () => {
  const f = await fixture();
  await f.receive(message(1, "Непередаваемая заявка"));
  const reference = (await f.state()).cases[0].reference;
  await f.receive(message(2, `Отменить №${reference}`));
  await f.ingress.transfer({ operation: "peek" });
  const ack = { operation: "ack", through: 2, snapshot_sha256: "a".repeat(64) };
  await f.ingress.transfer(ack);
  await f.ingress.transfer(ack);
  const state = await f.state();
  await f.ingress.transfer({ ...ack, snapshot_sha256: "b".repeat(64) });
  assert.deepEqual(await f.state(), state);
  await assert.rejects(f.ingress.transfer({ ...ack, snapshot_sha256: "invalid" }), { status: 400 });
  assert.ok(!JSON.stringify(await f.state()).includes("Непередаваемая заявка"));
});

test("повтор ACK с другим SHA не меняет новые данные и просроченную prepared попытку", async () => {
  const f = await fixture();
  await f.receive(message(1, "Переданный текст"));
  await f.receive(message(2, "Предложить новость"));
  await f.ingress.transfer({ operation: "peek" });
  const ack = { operation: "ack", through: 2, snapshot_sha256: "a".repeat(64), open_drafts: [] };
  await f.ingress.transfer(ack);
  await f.receive(message(3, "Новый открытый текст"));
  await f.receive({ update_id: 4, callback_query: { data: "intake:114:new", from: { id: 99 }, message: { chat: { id: 99 } } } });
  f.deps.hooks.afterPrepared = () => { throw new Error("crash"); };
  await assert.rejects(f.receive(message(5, "Новейшая попытка", { from: { id: 44 }, chat: { type: "private", id: 44 } })), /crash/u);
  f.advance(LIMITS.attemptMs + 1);
  const state = await f.state();
  const revision = f.store.revision;
  const calls = f.calls.length;
  const result = await f.ingress.transfer({ ...ack, snapshot_sha256: "b".repeat(64) });
  assert.equal(result.status, "ready");
  assert.deepEqual(await f.state(), state);
  assert.equal(f.store.revision, revision);
  assert.equal(f.calls.length, calls);
  assert.equal((await f.state()).checkpoint_sha256, "a".repeat(64));
  assert.equal((await f.state()).relay_actors["99:99:"], 4);
  await assert.rejects(f.ingress.transfer({ ...ack, through: 1 }), { status: 409 });
  assert.deepEqual(await f.state(), state);
});

test("первый ACK0 применяет cleanup seed ограничений, повтор ACK0 ничего не пересинхронизирует", async () => {
  const item = { id: 113, reference: "RM-AAAA-AAAA", kind: "news_tip", status: "new",
    telegram_user_id: 42, telegram_message_id: 100, payload: {
      reader_intake: true, public_reference: "RM-AAAA-AAAA", chat_id: 42, text: "В каноне", attachments: [],
    } };
  const f = await fixture({ seed: seed({ cases: [item] }) });
  assert.equal((await f.state()).relay_actors["42:42:"], 0);
  await f.ingress.transfer({ operation: "ack", through: 0, snapshot_sha256: "a".repeat(64), open_drafts: [] });
  assert.equal((await f.state()).relay_actors["42:42:"], undefined);
  await f.receive(message(1, "Новый текст"));
  const state = await f.state();
  const revision = f.store.revision;
  const calls = f.calls.length;
  await f.ingress.transfer({ operation: "ack", through: 0, snapshot_sha256: "b".repeat(64), open_drafts: [] });
  assert.deepEqual(await f.state(), state);
  assert.equal(f.store.revision, revision);
  assert.equal(f.calls.length, calls);
});

test("bootstrap идемпотентен, не заменяет другой seed и не хранит seed в config", async () => {
  const f = await fixture();
  await f.receive(message(1, "Текст"));
  await f.ingress.bootstrap(f.input);
  assert.equal((await f.state()).events.length, 1);
  await assert.rejects(f.ingress.bootstrap(bootstrap(seed({ processed_updates: [987] }))), { status: 409 });
  await assert.rejects(f.ingress.bootstrap({ ...f.input, mode: "preview" }), { status: 409 });
  const config = (await f.store.read(f.ingress.paths.config)).value;
  assert.deepEqual(Object.keys(config).sort(), ["bot_id", "bot_token", "seed_sha256", "version", "webhook_secret"]);
});

test("seed receipts suppress updates; исчерпанная очередь отвечает 503 без потери", async () => {
  const f = await fixture({ seed: seed({ processed_updates: [1] }) });
  await f.receive(message(1, "Привет"));
  assert.equal(f.calls.length, 0);
  const raw = "x".repeat(400_000);
  let successful = 0;
  for (let i = 2; i < 14; i++) {
    try {
      await f.receive({ update_id: i, callback_query: { data: raw } });
      successful++;
    } catch (error) { assert.equal(error.status, 503); break; }
  }
  const state = await f.state();
  assert.equal(state.events.length, successful);
  assert.equal(state.next_sequence, successful + 1);
  assert.equal(Object.keys(state.receipts).length, successful + 1);
});

test("peek автоматически ограничивает JSON bytes без разрывов последовательности", async () => {
  const f = await fixture();
  for (let id = 1; id <= 30; id++) await f.receive(message(id, "я".repeat(600)));
  const first = await f.ingress.transfer({ operation: "peek", after: 0, limit: 30 });
  assert.ok(Buffer.byteLength(JSON.stringify(first)) <= LIMITS.exportBytes);
  assert.ok(first.events.length > 0 && first.events.length < 30);
  const second = await f.ingress.transfer({ operation: "peek", after: first.events.at(-1).sequence, limit: 30 });
  assert.ok(Buffer.byteLength(JSON.stringify(second)) <= LIMITS.exportBytes);
  const all = [...first.events, ...second.events].map((event) => event.sequence);
  assert.deepEqual(all, Array.from({ length: all.length }, (_, i) => i + 1));
  await assert.rejects(f.receive({ update_id: 31, callback_query: { data: "x".repeat(LIMITS.exportBytes) } }), { status: 413 });
  assert.equal((await f.state()).receipts[31], undefined);
});
