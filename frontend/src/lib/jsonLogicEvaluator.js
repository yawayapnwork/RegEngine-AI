// A dependency-free JSON-Logic evaluator for the Policy Playground's
// zero-latency in-browser evaluation path.
//
// This is a deliberate, faithful JS port of the exact grammar
// `app/backtest/jsonlogic_evaluator.py` implements server-side (var, and,
// ==, in, >=, >, <=, <) -- NOT a general-purpose json-logic-js
// integration. The whole point is bit-for-bit fidelity with what
// `app.compiler.jsonlogic_compiler` actually emits and what OPA would
// actually evaluate for the same rule, so a playground "ALLOW"/"DENY"
// here means the same thing a real backend evaluation would. A node
// shape outside that set raises, exactly like the Python original,
// rather than silently guessing at semantics a general JSON-Logic
// library might interpret differently.
//
// See useOpaWasm.js for why this is the DEFAULT evaluation path (real
// OPA Wasm needs a compiled .wasm bundle, which requires the `opa` CLI
// -- not runnable in a browser -- so this is what gives live-edited
// JSON-Logic its "instant feedback as you type" property Requirement 2
// asks for).

export class UnsupportedJsonLogicNodeError extends Error {}
export class MissingFactError extends Error {
  constructor(path) {
    super(`Missing fact: ${path}`);
    this.path = path;
  }
}

const COMPARISON_OPS = {
  "==": (a, b) => a === b,
  ">=": (a, b) => a >= b,
  ">": (a, b) => a > b,
  "<=": (a, b) => a <= b,
  "<": (a, b) => a < b,
};

function resolveVar(path, data) {
  let node = data;
  for (const part of path.split(".")) {
    if (typeof node !== "object" || node === null || !(part in node)) {
      throw new MissingFactError(path);
    }
    node = node[part];
  }
  return node;
}

/** `data` is the same `{ entity_type, facts }` shape OPA's `input`
 * document uses -- a JsonLogicRule and a compiled Rego module are
 * interchangeable given identical input. */
export function evaluateJsonLogic(node, data) {
  if (node === null || ["boolean", "number", "string"].includes(typeof node)) {
    return node;
  }
  if (Array.isArray(node)) {
    return node.map((item) => evaluateJsonLogic(item, data));
  }
  if (typeof node !== "object") {
    throw new UnsupportedJsonLogicNodeError(`Unrecognized JSON-Logic node: ${JSON.stringify(node)}`);
  }

  const keys = Object.keys(node);
  if (keys.length !== 1) {
    throw new UnsupportedJsonLogicNodeError(`Expected exactly one operator key, got: ${JSON.stringify(keys)}`);
  }
  const [operator] = keys;
  const operands = node[operator];

  if (operator === "var") {
    return resolveVar(operands, data);
  }

  if (operator === "and") {
    if (!Array.isArray(operands)) {
      throw new UnsupportedJsonLogicNodeError("'and' operands must be a list.");
    }
    return operands.every((child) => evaluateJsonLogic(child, data));
  }

  if (operator === "in") {
    if (!Array.isArray(operands) || operands.length !== 2) {
      throw new UnsupportedJsonLogicNodeError("'in' requires exactly two operands: [needle, haystack].");
    }
    const needle = evaluateJsonLogic(operands[0], data);
    const haystack = evaluateJsonLogic(operands[1], data);
    return haystack.includes(needle);
  }

  if (operator in COMPARISON_OPS) {
    if (!Array.isArray(operands) || operands.length !== 2) {
      throw new UnsupportedJsonLogicNodeError(`'${operator}' requires exactly two operands.`);
    }
    const left = evaluateJsonLogic(operands[0], data);
    const right = evaluateJsonLogic(operands[1], data);
    return COMPARISON_OPS[operator](left, right);
  }

  throw new UnsupportedJsonLogicNodeError(`Unsupported JSON-Logic operator: '${operator}'`);
}
