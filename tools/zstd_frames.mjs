#!/usr/bin/env node
/**
 * Decode a multi-frame zstd file to stdout.
 *
 * DSH appends one zstd frame per session event, so a session log is thousands
 * of concatenated frames rather than one stream. Node's zstdDecompressSync
 * decodes only the first frame and silently returns 223 bytes of a 17 MB file,
 * which looks like an empty session rather than an error — so the frames are
 * walked by hand here.
 *
 * Node's built-in zstd is used deliberately: the project's runtime dependency
 * budget is exactly one package (cryptography), and this must not add a second.
 *
 *   node tools/zstd_frames.mjs session.v3.jsonl.zstd > session.jsonl
 *   node tools/zstd_frames.mjs --count session.v3.jsonl.zstd
 */
import { readFileSync } from 'node:fs';
import { zstdDecompressSync } from 'node:zlib';

const MAGIC = Buffer.from([0x28, 0xb5, 0x2f, 0xfd]);

const argv = process.argv.slice(2);
const countOnly = argv.includes('--count');
const target = argv.find((item) => !item.startsWith('--'));

if (!target) {
  process.stderr.write('usage: zstd_frames.mjs [--count] <file.zstd>\n');
  process.exit(2);
}

const input = readFileSync(target);
const frames = [];
let offset = 0;
let trailing = 0;

while (offset < input.length) {
  // Search for the next frame header *after* this frame's own header.
  const found = input.indexOf(MAGIC, offset + 4);
  const end = found < 0 ? input.length : found;
  try {
    frames.push(zstdDecompressSync(input.subarray(offset, end)));
  } catch (error) {
    // A truncated final frame is normal if the session is still being written;
    // anything else is worth reporting so a silent partial read is impossible.
    trailing = input.length - offset;
    process.stderr.write(`stopped after ${frames.length} frames: ${error.message} (${trailing} bytes left)\n`);
    break;
  }
  offset = end;
}

if (countOnly) {
  const total = frames.reduce((sum, frame) => sum + frame.length, 0);
  process.stdout.write(`${frames.length} frames, ${total} bytes\n`);
} else {
  process.stdout.write(Buffer.concat(frames));
}
