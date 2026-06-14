/**
 * Tiny runtime type-validators for API response shapes.
 *
 * The frontend currently trusts backend responses implicitly (typed
 * interfaces only). A schema mismatch — a renamed field, a typo, a
 * dropped optional key — silently propagates as ``undefined`` deep in a
 * template and the user sees a blank card with no error.
 *
 * ``defineValidator`` lets us declare a validator for any shape in a
 * few lines. The validator returns either the original (already-narrowed)
 * object on success, or a ``ValidationError`` describing every
 * discrepancy so the caller can log/inspect. We intentionally do NOT
 * import a full library (zod / valibot) to keep the bundle small and
 * avoid a new dependency for ~30 lines of code.
 */

export class ValidationError extends Error {
  readonly path: string
  readonly reason: string
  constructor(path: string, reason: string) {
    super(`${path}: ${reason}`)
    this.path = path
    this.reason = reason
    this.name = 'ValidationError'
  }
}

export type Validator<T> = (value: unknown, path?: string) => T

export function object<T extends Record<string, Validator<unknown>>>(
  shape: T,
): Validator<{ [K in keyof T]: ReturnType<T[K]> }> {
  return (value, path = '$') => {
    if (value === null || typeof value !== 'object') {
      throw new ValidationError(path, `expected object, got ${typeof value}`)
    }
    const obj = value as Record<string, unknown>
    const out: Record<string, unknown> = {}
    for (const key in shape) {
      const v = shape[key] as Validator<unknown>
      try {
        out[key] = v(obj[key], `${path}.${key}`)
      } catch (err) {
        if (err instanceof ValidationError) throw err
        throw new ValidationError(`${path}.${key}`, (err as Error).message)
      }
    }
    return out as { [K in keyof T]: ReturnType<T[K]> }
  }
}

export const string: Validator<string> = (value, path = '$') => {
  if (typeof value !== 'string') {
    throw new ValidationError(path, `expected string, got ${typeof value}`)
  }
  return value
}

export const optionalString: Validator<string | null | undefined> = (value, path = '$') => {
  if (value === undefined || value === null) return value
  if (typeof value !== 'string') {
    throw new ValidationError(path, `expected string|null, got ${typeof value}`)
  }
  return value
}

export const number: Validator<number> = (value, path = '$') => {
  if (typeof value !== 'number' || Number.isNaN(value)) {
    throw new ValidationError(path, `expected number, got ${typeof value}`)
  }
  return value
}

export const optionalNumber: Validator<number | null | undefined> = (value, path = '$') => {
  if (value === undefined || value === null) return value
  if (typeof value !== 'number' || Number.isNaN(value)) {
    throw new ValidationError(path, `expected number|null, got ${typeof value}`)
  }
  return value
}

export const boolean: Validator<boolean> = (value, path = '$') => {
  if (typeof value !== 'boolean') {
    throw new ValidationError(path, `expected boolean, got ${typeof value}`)
  }
  return value
}

export const optionalBoolean: Validator<boolean | null | undefined> = (value, path = '$') => {
  if (value === undefined || value === null) return value
  if (typeof value !== 'boolean') {
    throw new ValidationError(path, `expected boolean|null, got ${typeof value}`)
  }
  return value
}

/**
 * Permissive "any object" validator. Accepts ``undefined``/``null`` and any
 * non-array object. Use this for nested payloads where the inner shape is
 * not pinned to a specific schema (e.g. a heterogeneous ``risks`` bag) and
 * the caller is going to do its own narrowing via ``as`` on the values it
 * actually reads. Avoids the "frame dropped because deep type didn't match"
 * class of bug that hits schemas that nest arbitrary server data.
 */
export const optionalObject: Validator<Record<string, unknown> | null | undefined> = (
  value,
  path = '$',
) => {
  if (value === undefined || value === null) return value
  if (typeof value !== 'object' || Array.isArray(value)) {
    throw new ValidationError(path, `expected object|null, got ${Array.isArray(value) ? 'array' : typeof value}`)
  }
  return value as Record<string, unknown>
}

export function enumOf<T extends string>(allowed: readonly T[]): Validator<T> {
  return (value, path = '$') => {
    if (typeof value !== 'string' || !allowed.includes(value as T)) {
      throw new ValidationError(
        path,
        `expected one of [${allowed.join(', ')}], got ${JSON.stringify(value)}`,
      )
    }
    return value as T
  }
}

/** Try the validator, returning null on failure (for use in non-critical paths). */
export function safeValidate<T>(v: Validator<T>, value: unknown): T | null {
  try {
    return v(value)
  } catch (err) {
    if (import.meta.env.DEV) {
      // eslint-disable-next-line no-console
      console.warn('[validator] validation failed', err)
    }
    return null
  }
}
