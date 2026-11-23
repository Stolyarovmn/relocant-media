const usernamePattern = /^[A-Za-z][A-Za-z0-9_]{4,31}$/u;
const postPattern = /^https:\/\/t\.me\/[a-z][a-z0-9_]{4,31}\/([1-9][0-9]{0,15})$/u;
const origins = new Set(["channel", "chat", "user", "hidden_user"]);

export function isForwardedMessage(message) {
  return origins.has(message.forward_origin?.type);
}

export function forwardedPublicPost(message) {
  const origin = message.forward_origin;
  if (origin?.type !== "channel" || origin.chat?.type !== "channel") return null;
  const { username } = origin.chat;
  const { message_id: messageId } = origin;
  if (typeof username !== "string" || usernamePattern.exec(username)?.[0] !== username ||
      !Number.isSafeInteger(messageId) || messageId <= 0) return null;
  return `https://t.me/${username.toLowerCase()}/${messageId}`;
}

export function publicForwardUrls(payload) {
  const keys = Object.keys(payload.reader_parts ?? {})
    .filter(key => /^[1-9][0-9]{0,15}$/u.exec(key)?.[0] === key)
    .sort((a, b) => Number(a) - Number(b));
  const urls = new Set();
  for (const key of keys) {
    const url = payload.reader_parts[key]?.public_forward_url;
    const match = typeof url === "string" ? postPattern.exec(url) : null;
    if (match && match[0] === url && Number.isSafeInteger(Number(match[1]))) urls.add(url);
  }
  return [...urls];
}

export function hasForwardedParts(payload) {
  return Object.values(payload.reader_parts ?? {}).some(part => part?.forwarded === true);
}
