import { receive } from "../lib/receive.js";

export default async function (_message, ctx) {
  await receive(ctx.update);
}
