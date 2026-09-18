#!/usr/bin/env node
// Copy named top-level definitions from a donor file into a target file, by AST range.
//
//   node tools/harness/restore-defs.mjs <donor.js> <target.js> <anchorFunctionName> name...
//
// Brace counting cannot do this safely: these files are full of template literals that
// contain braces. The parser knows where a declaration really ends.

import { readFileSync, writeFileSync } from "node:fs";

import * as espree from "espree";

// index.html keeps the whole UI in one inline <script>. Operate on that script and put
// it back where it came from, so the same tool repairs both files.
const INLINE = /<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/;
function readCode(path) {
  const source = readFileSync(path, "utf8");
  if (!path.endsWith(".html")) return { code: source, wrap: (next) => next };
  const match = INLINE.exec(source);
  if (!match) throw new Error(`no inline script in ${path}`);
  const before = source.slice(0, match.index + match[0].indexOf(match[1]));
  const after = source.slice(match.index + match[0].indexOf(match[1]) + match[1].length);
  return { code: match[1], wrap: (next) => before + next + after };
}

const [donorPath, targetPath, anchorName, ...names] = process.argv.slice(2);
const parse = (code) => espree.parse(code, {
  ecmaVersion: 2024, sourceType: "script", range: true, loc: true,
});

function topLevelDeclarations(code) {
  const found = new Map();
  const walk = (nodes) => {
    for (const node of nodes) {
      if (node.type === "FunctionDeclaration" && node.id) {
        found.set(node.id.name, node.range);
      } else if (node.type === "VariableDeclaration") {
        for (const declarator of node.declarations) {
          if (declarator.id.type === "Identifier") {
            found.set(declarator.id.name, node.range);
          }
        }
      } else if (node.type === "ExpressionStatement"
          && node.expression.type === "CallExpression"
          && ["FunctionExpression", "ArrowFunctionExpression"]
            .includes(node.expression.callee.type)) {
        // The app body lives inside one IIFE; its declarations are the ones we want.
        walk(node.expression.callee.body.body || []);
      }
    }
  };
  walk(parse(code).body);
  return found;
}

const donor = readCode(donorPath).code;
const targetFile = readCode(targetPath);
const target = targetFile.code;
const donorDeclarations = topLevelDeclarations(donor);
const targetDeclarations = topLevelDeclarations(target);

const pieces = [];
for (const name of names) {
  if (targetDeclarations.has(name)) {
    console.log(`skip     ${name} (already defined)`);
    continue;
  }
  const range = donorDeclarations.get(name);
  if (!range) {
    console.log(`MISSING  ${name} (not in donor)`);
    continue;
  }
  pieces.push(donor.slice(range[0], range[1]));
  console.log(`restore  ${name}`);
}

if (!pieces.length) {
  console.log("nothing to restore");
  process.exit(0);
}

const anchorRange = targetDeclarations.get(anchorName);
if (!anchorRange) {
  console.error(`anchor ${anchorName} not found in target`);
  process.exit(2);
}
const lineStart = target.lastIndexOf("\n", anchorRange[0]) + 1;
const banner = "  // Restored from the last coherent production file: the v1.38 merge dropped these\n"
  + "  // definitions while their call sites stayed, so these paths threw on first use.\n";
const insert = `${banner}  ${pieces.join("\n\n  ")}\n\n`;
writeFileSync(targetPath,
  targetFile.wrap(target.slice(0, lineStart) + insert + target.slice(lineStart)));
console.log(`inserted ${pieces.length} definition(s) before ${anchorName}`);
