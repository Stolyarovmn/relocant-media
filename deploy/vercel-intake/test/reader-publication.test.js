import test from "node:test";
import assert from "node:assert/strict";
import { fixture, message, seed } from "./helpers.js";
import { materialFingerprint, PUBLICATION_POLICY as P, PUBLICATION_LABELS as L } from "../lib/reader-publication.js";

async function reader(origin = "bot", username = "reader_alias") {
  const settings = seed({ channel_id: P.channel_id });
  const f = await fixture({ seed: settings, botFactory: (config, enabled) => ({
    call: async (method, params) => {
      assert.equal(enabled,true);
      f.calls.push({method,params});
      if (method === "getChat") return { is_direct_messages:true, parent_chat:{id:P.channel_id} };
      return { message_id:9000+f.calls.length };
    },
  }) });
  let id = 0;
  f.send = async (text, extra={}) => f.receive(message(++id,text,{
    from:{id:42,is_bot:false, ...(username ? {username} : {})},
    ...(origin === "channel_dm" ? {chat:{id:-200001,type:"supergroup",is_direct_messages:true},
      direct_messages_topic:{topic_id:17}} : {}), ...extra,
  }));
  f.case = async () => (await f.state()).cases[0];
  await f.send("/start");
  await f.send("Тест, не публиковать", {photo:[{file_id:"photo",file_unique_id:"photo-unique"}]});
  await f.send("Предложить новость");
  return f;
}

for (const origin of ["bot","channel_dm"]) for (const signature of ["anonymous","username","name"]) {
  test(origin + ": разрешение и подпись " + signature + " сохраняются с одним номером", async () => {
    const f = await reader(origin);
    const ref = (await f.case()).reference;
    assert.equal((await f.case()).status,"collecting");
    await f.send(L.own);
    await f.send(L.allow);
    await f.send(L[signature]);
    if (signature === "name") await f.send("Софийко");
    if (signature !== "anonymous") await f.send(L.credit);
    const before = await f.case();
    assert.equal(before.payload.reader_publication.stage,"review");
    assert.equal(before.payload.text,"Тест, не публиковать");
    await f.send("Отправить №" + ref);
    const item = await f.case();
    const r = item.payload.reader_publication;
    assert.equal(item.reference,ref);
    assert.equal(item.status,"new");
    assert.equal(r.material_sha256,materialFingerprint(item.payload));
    assert.equal(r.signature.mode,signature);
    assert.ok(r.confirmed.at);
    assert.equal(r.scope.channel_id,P.channel_id);
    assert.equal((await f.state()).cases.length,1);
    assert.equal(f.calls.at(-1).params.direct_messages_topic_id, origin === "channel_dm" ? 17 : undefined);
  });
}

test("без username не предлагается публичный ник и имя профиля не копируется", async () => {
  const f = await reader("bot",null);
  await f.send(L.own); await f.send(L.allow);
  const labels = f.calls.at(-1).params.reply_markup.keyboard.flat().map(x=>x.text);
  assert.ok(!labels.includes(L.username));
  assert.equal((await f.case()).payload.reader_publication.available_username,null);
  await f.send(L.username);
  assert.equal((await f.case()).payload.reader_publication.stage,"signature");
});

test("новый файл и исправление сбрасывают разрешение, а не расширяют его", async () => {
  const f = await reader();
  await f.send(L.own); await f.send(L.allow); await f.send(L.anonymous);
  await f.send("Дополнение", {video:{file_id:"video",file_unique_id:"video-unique"}});
  let r = (await f.case()).payload.reader_publication;
  assert.equal(r.stage,"ownership"); assert.equal(r.permission,undefined);
  await f.send(L.own); await f.send(L.allow); await f.send(L.anonymous);
  const update = message(99,"Исправленный текст",{message_id:102});
  update.edited_message=update.message; delete update.message;
  await f.receive(update);
  r=(await f.case()).payload.reader_publication;
  assert.equal(r.stage,"ownership"); assert.equal(r.confirmed,undefined);
  assert.equal((await f.case()).payload.text,"Исправленный текст\nДополнение");
});

test("чужой источник и только для редакции не дают разрешения на публикацию", async () => {
  for (const source of [true,false]) {
    const f=await reader();
    await f.send(source ? L.source : L.own);
    if (!source) await f.send(L.private);
    await f.send("Отправить №"+(await f.case()).reference);
    assert.equal((await f.case()).payload.reader_publication.permission,"private");
    assert.equal((await f.case()).payload.reader_publication.signature.mode,"anonymous");
  }
});

test("подпись нельзя подтвердить пропуском отдельного шага", async () => {
  const f=await reader();
  await f.send(L.own); await f.send(L.allow); await f.send(L.username);
  await f.send("Отправить №"+(await f.case()).reference);
  assert.equal((await f.case()).status,"collecting");
  assert.equal((await f.case()).payload.reader_publication.stage,"credit_confirm");
  await f.send(L.back); await f.send(L.anonymous);
  assert.equal((await f.case()).payload.reader_publication.signature.disclosure,undefined);
});

test("почта и телефон не становятся подписью, отмена сохраняет один номер", async () => {
  const f=await reader();
  await f.send(L.own); await f.send(L.allow); await f.send(L.name);
  await f.send("author@example.com"); await f.send("+79990000000");
  assert.equal((await f.case()).payload.reader_publication.stage,"name");
  assert.equal((await f.case()).payload.text,"Тест, не публиковать");
  await f.send("Отменить №"+(await f.case()).reference);
  assert.equal((await f.case()).status,"cancelled");
  assert.equal((await f.state()).cases.length,1);
});

test("жалоба не проходит публикационные экраны", async () => {
  const f=await fixture({seed:seed({channel_id:P.channel_id})});
  await f.receive(message(1,"Жалоба, не публиковать"));
  await f.receive(message(2,"Пожаловаться"));
  const item=(await f.state()).cases[0];
  assert.equal(item.status,"new");
  assert.equal(item.payload.reader_publication,undefined);
});

test("кнопка прежнего шага после отправки не создаёт новый черновик", async () => {
  const f=await reader();
  await f.send(L.own); await f.send(L.private);
  await f.send("Отправить №"+(await f.case()).reference);
  await f.send(L.allow);
  assert.equal((await f.state()).cases.length,1);
  assert.equal((await f.state()).events.at(-1).receipt_only,true);
  assert.match(f.calls.at(-1).params.text,/прежнего шага/u);
});
