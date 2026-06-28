/**
 * Input sanitization utilities.
 *
 * - DOMPurify for HTML context (user-provided content rendered in the DOM)
 * - xmlEscape for SVG/XML context
 * - sanitizeText for plain-text context (strips all HTML)
 */

import DOMPurify from "dompurify";

// ── HTML sanitization ──────────────────────────────────────────

/**
 * Sanitize an HTML string for safe insertion via v-html or innerHTML.
 * Strips all scripts, event handlers, and dangerous attributes.
 * Returns clean HTML safe for DOM insertion.
 *
 * Use this BEFORE passing user-provided content to v-html.
 */
export function sanitizeHtml(dirty: string): string {
  return DOMPurify.sanitize(dirty, {
    ALLOWED_TAGS: [
      "b", "i", "em", "strong", "a",
      "p", "br", "ul", "ol", "li",
      "code", "pre", "span", "div",
      "h1", "h2", "h3", "h4", "h5", "h6",
      "table", "thead", "tbody", "tr", "td", "th",
    ],
    ALLOWED_ATTR: ["href", "title", "class", "id", "target"],
  });
}

/**
 * Strip ALL HTML tags from a string. Returns plain text only.
 * Use for display contexts where no markup is expected (e.g., display names, device names).
 */
export function sanitizeText(dirty: string): string {
  return DOMPurify.sanitize(dirty, { ALLOWED_TAGS: [], ALLOWED_ATTR: [] });
}

// ── SVG/XML escaping ───────────────────────────────────────────

/** Escape user-controlled text for safe embedding in SVG/XML. */
export function xmlEscape(text: string): string {
  return text
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&apos;");
}

/** Generate a safe SVG data URI for use as a CSS background-image watermark. */
export function buildWatermarkSvg(displayName: string, dateStr?: string): string {
  const safeName = xmlEscape(displayName);
  const text = dateStr ? `${safeName}  ${dateStr}` : safeName;
  const svg = `<svg xmlns="http://www.w3.org/2000/svg" width="260" height="100">
    <text x="130" y="50" text-anchor="middle" font-size="14" font-family="Arial,sans-serif" fill="rgba(255,255,255,0.13)" font-weight="bold">${text}</text>
  </svg>`;
  return encodeURIComponent(svg);
}
