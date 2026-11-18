export function namespace(env) {
  if (env.VERCEL_ENV === "production") return "production";
  const id = env.VERCEL_DEPLOYMENT_ID;
  if (typeof id !== "string" || !/^[A-Za-z0-9_-]{1,100}$/u.test(id)) {
    throw new Error("deployment_namespace_missing");
  }
  return `preview/${id}`;
}

export function paths(env) {
  const prefix = namespace(env);
  return { state: `${prefix}/state.json`, config: `${prefix}/config.json` };
}
