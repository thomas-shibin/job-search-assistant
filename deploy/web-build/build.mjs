// The browser files as the release image serves them (Dockerfile, web stage):
//   *.js   comments and whitespace removed, names shortened (terser), then
//          obfuscated: strings moved into an encoded table, names made
//          meaningless, some control flow scrambled (javascript-obfuscator)
//   *.css  comments and whitespace removed (clean-css)
//   *.svg  comments and whitespace between tags removed
// No source maps. This makes the files hard to read, not impossible: anything
// a browser runs, a visitor can download. Keep secrets and business rules on
// the server.
//
//   node build.mjs <static folder in> <static folder out>
import { readFileSync, writeFileSync, mkdirSync, readdirSync, statSync, copyFileSync } from "node:fs";
import { join, relative, dirname, extname } from "node:path";
import { minify } from "terser";
import JavaScriptObfuscator from "javascript-obfuscator";
import CleanCSS from "clean-css";

const [src, out] = process.argv.slice(2);
if (!src || !out) {
  console.error("usage: node build.mjs <in> <out>");
  process.exit(2);
}

// Safe for these files: they're each one self-running function, and the only
// names other code relies on are properties (window.openAuth, window.hirlumo),
// which neither tool renames. No eval, no self-defending code or debugger
// traps: those break on reformatting and slow every visitor down.
const OBFUSCATE = {
  target: "browser",
  compact: true,
  simplify: true,
  identifierNamesGenerator: "hexadecimal",
  renameGlobals: false,
  transformObjectKeys: false,
  stringArray: true,
  stringArrayEncoding: ["base64"],
  stringArrayThreshold: 0.75,
  stringArrayRotate: true,
  stringArrayShuffle: true,
  stringArrayWrappersCount: 1,
  splitStrings: false,
  controlFlowFlattening: true,
  controlFlowFlatteningThreshold: 0.25,
  deadCodeInjection: false,
  selfDefending: false,
  debugProtection: false,
  disableConsoleOutput: false,
  numbersToExpressions: false,
  unicodeEscapeSequence: false,
  sourceMap: false,
};

function files(dir) {
  return readdirSync(dir).flatMap((name) => {
    const path = join(dir, name);
    return statSync(path).isDirectory() ? files(path) : [path];
  });
}

let before = 0, after = 0;
for (const path of files(src)) {
  const target = join(out, relative(src, path));
  mkdirSync(dirname(target), { recursive: true });
  const ext = extname(path);
  if (![".js", ".css", ".svg"].includes(ext)) {
    copyFileSync(path, target);
    continue;
  }
  const text = readFileSync(path, "utf8");
  let result;
  if (ext === ".js") {
    const small = await minify(text, { compress: { passes: 2 }, mangle: true, format: { comments: false } });
    result = JavaScriptObfuscator.obfuscate(small.code, OBFUSCATE).getObfuscatedCode();
  } else if (ext === ".css") {
    const css = new CleanCSS({ level: 1, inline: false }).minify(text);
    if (css.errors.length) throw new Error(`${path}: ${css.errors.join("; ")}`);
    result = css.styles;
  } else {
    result = text.replace(/<!--[\s\S]*?-->/g, "").replace(/>\s+</g, "><").trim();
  }
  writeFileSync(target, result + "\n");
  before += text.length;
  after += result.length;
  console.log(`${relative(src, path)}: ${text.length} -> ${result.length} bytes`);
}
console.log(`total: ${before} -> ${after} bytes`);
