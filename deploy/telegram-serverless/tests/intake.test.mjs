import assert from "node:assert/strict";
import test from "node:test";

import { planDraft, readerIdentity } from "../tgcloud/lib/intake.js";

const form = {
  menu: "Напишите, что хотите сообщить.",
  new_case: "Новое обращение",
  routes: { news_tip: ["Предложить новость", "Что произошло?"] },
};
const at = "2026-11-17T12:00:00.000Z";
const reference = "RM-1234-5678";

for (const direct of [false, true]) {
  test(`draft and fast receipt via ${direct ? "channel inbox" : "bot chat"}`, () => {
    const fields = {
      from: { id: 42 },
      chat: direct ? { id: -100, is_direct_messages: true } : { id: 42, type: "private" },
      ...(direct && { direct_messages_topic: { topic_id: 73 } }),
    };
    const first = { ...fields, message_id: 1, text: "Здравствуйте!" };
    const actor = { ...readerIdentity(first), channel: -220 };
    const plan = (draft, id, content, edited = false) =>
      planDraft(form, draft, { ...fields, message_id: id, ...content }, edited,
        () => reference, actor, at);
    const hello = plan(null, 1, { text: "Здравствуйте!" });
    assert.equal(hello.notice.text, form.menu);
    assert.equal(hello.draft.payload.text, "");
    hello.draft.payload.reader_notices = {
      menu: { ...hello.notice, status: "sent", received_at: at, telegram_message_id: 104 },
    };
    const empty = plan(hello.draft, 2, { text: "Предложить новость" });
    assert.equal(empty.draft.status, "choosing");
    assert.match(empty.notice.text, /Что произошло/);
    const description = plan(empty.draft, 3, { text: "Старый текст" });
    const photo = plan(description.draft, 4, {
      caption: "Фото", photo: [{ file_id: "small" }, { file_id: "large" }],
    });
    const edit = plan(photo.draft, 3, { text: "Исправленный текст" }, true);
    const accepted = plan(edit.draft, 5, { text: "Предложить новость" });
    assert.equal(accepted.draft.status, "new");
    assert.equal(accepted.draft.payload.text, "Исправленный текст\nФото");
    assert.equal(accepted.draft.payload.attachments[0].file_id, "large");
    assert.match(accepted.notice.text, /^Ваше обращение №RM-1234-5678 отправлено в работу\./);
    assert.equal(accepted.draft.payload.topic_id, direct ? 73 : undefined);
  });
}

test("legacy draft keeps its number, attachments and confirmation step", () => {
  const old = {
    id: 111, reference: "111", kind: "news_tip", status: "choosing", telegram_user_id: 42,
    payload: {
      reader_intake: true, chat_id: 42, text: "Черновик", attachments: [],
      reader_notices: { menu: { status: "sent", received_at: at, telegram_message_id: 90 } },
    },
  };
  const plan = (draft, message_id, text) => planDraft(form, draft, { message_id, text },
    false, () => { throw new Error("existing draft"); }, { user: 42, chat: 42, topic: null }, at);
  const chosen = plan(old, 120, "Предложить новость");
  assert.equal(chosen.draft.status, "collecting");
  assert.match(chosen.notice.text, /Отправить №111/);
  const saved = plan(chosen.draft, 121, "Отправить №111");
  assert.equal(saved.draft.id, 111);
  assert.equal(saved.draft.payload.text, "Черновик");
  assert.match(saved.notice.text, /обращение №111 отправлено/);
});
