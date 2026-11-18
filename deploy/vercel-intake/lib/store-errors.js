export class Conflict extends Error {}

export class RedisStoreError extends Error {
  constructor(code) { super(code); this.code = code; }
}
