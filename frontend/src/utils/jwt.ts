/**
 * Shared JWT utility functions.
 * Used by both the router guard and auth store.
 */

export interface JwtPayload {
  sub?: string
  username?: string
  role?: string
  permissions?: string[]
  device_scope?: string
  device_ids?: number[]
  pve_guests?: string[]
  exp?: number
  iat?: number
  type?: string
  jti?: string
}

/** Decode JWT payload without signature verification (for client-side use only). */
export function decodeJwtPayload(token: string): Record<string, unknown> {
  try {
    const base64 = token.split('.')[1]
    const bytes = Uint8Array.from(atob(base64.replace(/-/g, '+').replace(/_/g, '/')), (c) => c.charCodeAt(0))
    // atob yields a latin1 string; JWT payloads are UTF-8 bytes. Decode explicitly
    // so non-ASCII claims (e.g. Chinese usernames) don't get mangled.
    const json = new TextDecoder().decode(bytes)
    return JSON.parse(json) as Record<string, unknown>
  } catch {
    return {}
  }
}

/** Check if a JWT is expired based on its `exp` claim. */
export function isJwtExpired(token: string): boolean {
  const payload = decodeJwtPayload(token)
  // Without an `exp` claim, treat as expired for safety.
  if (!payload.exp) return true
  return Date.now() >= (payload.exp as number) * 1000
}
