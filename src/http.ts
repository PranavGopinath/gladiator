import type { IncomingMessage, ServerResponse } from 'node:http';
export async function body(req: IncomingMessage, limit = 2 * 1024 * 1024): Promise<Buffer> {
  const chunks: Buffer[] = []; let length = 0;
  for await (const chunk of req) { length += chunk.length; if (length > limit) throw new Error('Request too large'); chunks.push(chunk); }
  return Buffer.concat(chunks);
}
export function json(res: ServerResponse, status: number, value: unknown) {
  if (res.headersSent) { res.end(); return; }
  res.writeHead(status, { 'Content-Type': 'application/json', 'Cache-Control': 'no-store' }); res.end(JSON.stringify(value));
}
export function scrub(value: unknown, secrets: string[]): any {
  if (typeof value === 'string') { let result = value; for (const s of secrets) if (s) result = result.replaceAll(s, '[redacted]'); return result; }
  if (Array.isArray(value)) return value.map(x => scrub(x, secrets));
  if (value && typeof value === 'object') return Object.fromEntries(Object.entries(value).map(([k, v]) => [k, scrub(v, secrets)]));
  return value;
}
