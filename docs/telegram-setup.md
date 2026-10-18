# Telegram setup checklist

Owner-only/manual setup is intentionally deferred until runtime tests pass.

Required objects for the first city:

- public news channel;
- linked discussion supergroup;
- separate forum supergroup;
- admin/publisher bot;
- optional analytics user account for MTProto-only channel statistics.

Bot permissions should be minimal and mapped to implemented features. Do not grant unrelated admin rights.

Before production:

1. create resources;
2. record numeric ids as secrets/config, not source code;
3. configure webhook with a secret token;
4. explicitly choose allowed update types;
5. run staging publication/moderation tests;
6. only then enable production publishing.
