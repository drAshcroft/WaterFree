/**
 * String case-conversion utilities.
 */

export function toSnakeCase(s: string): string {
  // "UserAuthService.login" → "user_auth_service_login"
  return s
    .replace(/\./g, "_")
    .replace(/([A-Z])/g, "_$1")
    .toLowerCase()
    .replace(/^_/, "")
    .replace(/__+/g, "_");
}

export function toPascalCase(s: string): string {
  // "UserAuthService.login" → "UserAuthServiceLogin"
  return s
    .replace(/[._\s]+(.)/g, (_, c: string) => c.toUpperCase())
    .replace(/^(.)/, (c) => c.toUpperCase());
}
