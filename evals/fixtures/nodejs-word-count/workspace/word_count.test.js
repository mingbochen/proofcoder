"use strict";

const test = require("node:test");
const assert = require("node:assert");
const { countWords } = require("./word_count.js");

test("counts every repeated word", () => {
  assert.deepStrictEqual([...countWords("a b a b a")], [["a", 3], ["b", 2]]);
});

test("treats differing case as the same word", () => {
  assert.deepStrictEqual([...countWords("Alpha alpha beta")], [["alpha", 2], ["beta", 1]]);
});

test("ignores surrounding whitespace", () => {
  assert.deepStrictEqual([...countWords("  solo  ")], [["solo", 1]]);
});
