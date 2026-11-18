import test from "node:test";
import assert from "node:assert/strict";
import { fixture, message, seed } from "./helpers.js";

async function checkpoint(f) {
  const batch = await f.ingress.transfer({ operation: "peek" });
  await f.ingress.transfer({ operation: "ack", through: batch.events.at(-1).sequence,
    snapshot_sha256: "a".repeat(64), open_drafts: [] });
}

for (const media of ["text", "photo", "video", "document"]) {
  test(`досылка ${media} после ACK сразу подтверждена; исходный update не изменён`, async () => {
    const f = await fixture();
    await f.receive(message(1, "Описание"));
    await f.receive(message(2, "Предложить новость"));
    const acceptedId = f.calls.at(-1).params ? (await f.state()).events.at(-1).notice.telegram_message_id : null;
    await checkpoint(f);
    const extra = media === "text" ? {} : { [media]: media === "photo"
      ? [{ file_id: "fake", file_unique_id: "unique" }] : { file_id: "fake", file_unique_id: "unique" } };
    const update = message(3, "Дополнение", { ...extra, reply_to_message: { message_id: acceptedId } });
    await f.receive(update);
    await f.receive(update);
    const state = await f.state();
    assert.equal(state.cases.length, 0);
    assert.equal(state.events.length, 1);
    assert.deepEqual(state.events[0].update, update);
    assert.equal(state.events[0].draft, undefined);
    assert.equal(state.events[0].notice.status, "sent");
    assert.equal(state.events[0].notice.key, "received:3");
    assert.equal(f.calls.length, 3);
    assert.match(f.calls.at(-1).params.text, /получены и сохранены/);
    await checkpoint(f);
    assert.equal((await f.state()).events.length, 0);
    assert.ok(!JSON.stringify(await f.state()).includes("Дополнение"));
  });
}

test("старая inline кнопка немедленно снимает ожидание; решение остаётся в Actions", async () => {
  const f = await fixture();
  const update = { update_id: 1, callback_query: { id: "callback-1", data: "intake:111:cancel",
    from: { id: 42 }, message: { message_id: 500, chat: { id: 42, type: "private" } } } };
  await f.receive(update);
  await f.receive(update);
  const state = await f.state();
  assert.equal(state.cases.length, 0);
  assert.deepEqual(state.events[0].update, update);
  assert.equal(state.events[0].notice.status, "sent");
  assert.equal(f.calls.length, 1);
  assert.equal(f.calls[0].method, "answerCallbackQuery");
  assert.doesNotMatch(f.calls[0].params.text, /отменён|отправлено в работу/);
});

test("повтор отмены после ACK отвечает по метаданным без копии текста заявки", async () => {
  const f = await fixture();
  await f.receive(message(1, "Частный текст"));
  const reference = (await f.state()).cases[0].reference;
  await f.receive(message(2, `Отменить №${reference}`));
  await checkpoint(f);
  assert.ok(!JSON.stringify(await f.state()).includes("Частный текст"));
  await f.receive(message(3, `Отменить №${reference}`));
  assert.match(f.calls.at(-1).params.text, /уже отменён/);
  assert.deepEqual(f.calls.at(-1).params.reply_markup.keyboard, [[{ text: "Новое обращение" }]]);
  await f.receive(message(4, "Новое обращение"));
  const active = (await f.state()).cases[0].reference;
  await f.receive(message(5, `Отменить №${reference}`));
  assert.equal((await f.state()).cases[0].reference, active);
  assert.equal((await f.state()).cases[0].status, "choosing");
});

test("неизвестная старая кнопка подтверждает получение и не создаёт черновик", async () => {
  const f = await fixture();
  await f.receive(message(1, "Отменить №111"));
  assert.equal((await f.state()).cases.length, 0);
  assert.match(f.calls[0].params.text, /Проверяем состояние/);
  assert.doesNotMatch(f.calls[0].params.text, /отменён/);
});

test("неопределённая отправка подтверждения досылки не повторяется", async () => {
  let calls = 0;
  const f = await fixture({ seed: seed({ legacy_conversations: [42] }),
    botFactory: () => ({ call: async () => { calls++; throw new Error("lost_response"); } }) });
  const update = message(1, "Дополнение", { reply_to_message: { message_id: 500 } });
  await f.receive(update);
  await f.receive(update);
  assert.equal(calls, 1);
  assert.equal((await f.state()).events[0].notice.status, "uncertain");
});

test("досылка native DM проверяет parent и адресует ответ в тот же topic", async () => {
  const f = await fixture();
  await f.receive(message(1, "Дополнение", { chat: { id: -55, type: "supergroup", is_direct_messages: true },
    direct_messages_topic: { topic_id: 12 }, reply_to_message: { message_id: 500 } }));
  assert.equal(f.calls[0].method, "getChat");
  assert.equal(f.calls[1].params.direct_messages_topic_id, 12);
});
